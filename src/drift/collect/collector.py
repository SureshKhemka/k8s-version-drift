"""Concurrent collection with per-cluster retries and a global time budget (FR-4, FR-5, FR-6)."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Mapping, Sequence

import httpx

from drift.collect.k8s_client import K8sClient, MalformedResponse, ReadOnlyTransport, ReadOnlyViolation
from drift.config.models import ClustersConfig
from drift.domain.models import AttemptRecord, ClusterRef, FetchResult

TIME_BUDGET_EXCEEDED = "time budget exceeded"

TransportFactory = Callable[[], httpx.AsyncBaseTransport]


class _Unreachable(Exception):
    def __init__(self, reason: str, status: int | None, error_type: str, retryable: bool = True) -> None:
        super().__init__(reason)
        self.reason, self.status, self.error_type, self.retryable = reason, status, error_type, retryable


def endpoint_for(cluster: ClusterRef, cfg: ClustersConfig) -> str:
    if cluster.api_server:
        return cluster.api_server
    if cfg.endpoint_template:
        return cfg.endpoint_template.replace("{id}", cluster.id)
    raise ValueError(f"no API server for cluster {cluster.id}: set api_server or clusters.endpoint_template")


def _classify(exc: BaseException) -> _Unreachable:
    """Map an exception to a stable UNREACHABLE reason. Messages are dropped: they can contain URLs (FR-32)."""
    kind = type(exc).__name__
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return _Unreachable("timeout", None, kind)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return _Unreachable(f"auth error (HTTP {code})", code, kind, retryable=False)
        return _Unreachable(f"HTTP {code}", code, kind)
    if isinstance(exc, MalformedResponse):
        return _Unreachable("malformed response", None, kind)
    if isinstance(exc, httpx.TransportError):
        return _Unreachable("connection error", None, kind)
    raise exc  # programming errors (incl. ReadOnlyViolation) must surface


class Collector:
    def __init__(
        self,
        cfg: ClustersConfig,
        environ: Mapping[str, str] | None = None,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self._cfg = cfg
        environ = os.environ if environ is None else environ
        self._token = environ.get(cfg.token_env) if cfg.token_env else None
        self._transport_factory = transport_factory or (
            lambda: httpx.AsyncHTTPTransport(verify=cfg.verify_tls, retries=0)
        )

    async def collect(self, clusters: Sequence[ClusterRef]) -> list[FetchResult]:
        """Fetch every cluster. Always returns exactly one result per input cluster, in input order."""
        cfg = self._cfg
        sem = asyncio.Semaphore(cfg.concurrency)
        timeout = httpx.Timeout(cfg.timeout_s)
        async with httpx.AsyncClient(
            transport=ReadOnlyTransport(self._transport_factory()), timeout=timeout, follow_redirects=False
        ) as http:
            logs: dict[str, list[AttemptRecord]] = {c.id: [] for c in clusters}

            async def guarded(c: ClusterRef) -> FetchResult:
                async with sem:
                    return await self._fetch(http, c, logs[c.id])

            tasks = {c.id: asyncio.create_task(guarded(c), name=f"fetch:{c.id}") for c in clusters}
            if tasks:
                await asyncio.wait(tasks.values(), timeout=cfg.time_budget_s)
            results = []
            for c in clusters:
                task = tasks[c.id]
                if task.done() and not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        _cancel_all(tasks.values())
                        raise exc
                    results.append(task.result())
                else:
                    task.cancel()
                    results.append(
                        FetchResult(
                            cluster_id=c.id, reachable=False, reason=TIME_BUDGET_EXCEEDED, attempts=tuple(logs[c.id])
                        )
                    )
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            return results

    async def _fetch(self, http: httpx.AsyncClient, cluster: ClusterRef, attempts: list[AttemptRecord]) -> FetchResult:
        cfg = self._cfg
        client = K8sClient(http, endpoint_for(cluster, cfg), self._token, cfg.page_size)
        started = time.perf_counter()
        last: _Unreachable | None = None
        for attempt in range(1, cfg.retries + 2):
            t0 = time.perf_counter()
            try:
                async with asyncio.timeout(cfg.timeout_s):
                    cp = await client.control_plane_version()
                    nodes = await client.nodes(cfg.node_pool_labels)
            except ReadOnlyViolation:
                raise
            except Exception as exc:
                last = _classify(exc)
                attempts.append(
                    AttemptRecord(
                        attempt=attempt,
                        outcome=last.reason,
                        status_code=last.status,
                        error_type=last.error_type,
                        duration_ms=_ms(t0),
                    )
                )
                if not last.retryable:
                    break
                if attempt <= cfg.retries and cfg.retry_backoff_s:
                    await asyncio.sleep(cfg.retry_backoff_s * attempt)
                continue
            attempts.append(AttemptRecord(attempt=attempt, outcome="ok", duration_ms=_ms(t0)))
            return FetchResult(
                cluster_id=cluster.id,
                reachable=True,
                control_plane_version=cp,
                nodes=tuple(nodes),
                attempts=tuple(attempts),
                duration_ms=_ms(started),
                requests=client.requests,
            )
        assert last is not None
        return FetchResult(
            cluster_id=cluster.id,
            reachable=False,
            reason=last.reason,
            attempts=tuple(attempts),
            duration_ms=_ms(started),
            requests=client.requests,
        )


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _cancel_all(tasks: object) -> None:
    for t in tasks:  # type: ignore[attr-defined]
        t.cancel()
