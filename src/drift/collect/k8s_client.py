"""Read-only Kubernetes API client (FR-2, FR-3).

Read-only is enforced twice, independently of configuration:
  1. ``K8sClient`` only has methods that issue GET requests.
  2. ``ReadOnlyTransport`` refuses to send anything but GET/HEAD, whatever code builds the request.
"""

from __future__ import annotations

from typing import Any

import httpx

from drift.domain.models import NodeRecord

READ_METHODS = frozenset({"GET", "HEAD"})
UNLABELLED_POOL = "(unlabelled)"


class ReadOnlyViolation(RuntimeError):
    """A non-read request was about to be sent to a cluster. This is a bug, never a runtime condition."""


class MalformedResponse(Exception):
    pass


class ReadOnlyTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method.upper() not in READ_METHODS:
            raise ReadOnlyViolation(f"refusing {request.method} to a cluster: access is read-only")
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class K8sClient:
    """GET-only access to one cluster's API server. The base URL is never logged (FR-32)."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, token: str | None, page_size: int) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._headers = {"Accept": "application/json"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._page_size = page_size
        self.requests = 0

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.requests += 1
        resp = await self._http.get(f"{self._base}{path}", params=params, headers=self._headers)
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError as exc:
            raise MalformedResponse("response is not JSON") from exc
        if not isinstance(body, dict):
            raise MalformedResponse("response is not a JSON object")
        return body

    async def control_plane_version(self) -> str:
        body = await self._get_json("/version")
        git = body.get("gitVersion")
        if not isinstance(git, str):
            raise MalformedResponse("version response has no gitVersion")
        return git

    async def nodes(self, pool_labels: tuple[str, ...]) -> list[NodeRecord]:
        records: list[NodeRecord] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"limit": self._page_size}
            if token:
                params["continue"] = token
            body = await self._get_json("/api/v1/nodes", params)
            if body.get("kind") != "NodeList" or not isinstance(body.get("items"), list):
                raise MalformedResponse("nodes response is not a NodeList")
            records.extend(_node_record(item, pool_labels) for item in body["items"])
            token = (body.get("metadata") or {}).get("continue") or None
            if not token:
                return records


def _node_record(item: Any, pool_labels: tuple[str, ...]) -> NodeRecord:
    try:
        meta, status = item["metadata"], item["status"]
        labels = meta.get("labels") or {}
        pool = next((labels[k] for k in pool_labels if labels.get(k)), UNLABELLED_POOL)
        kubelet = status["nodeInfo"]["kubeletVersion"]
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])
        if not isinstance(meta["name"], str) or not isinstance(kubelet, str):
            raise TypeError
        return NodeRecord(name=meta["name"], pool=str(pool), kubelet_version=kubelet, ready=ready)
    except (KeyError, TypeError, AttributeError) as exc:
        raise MalformedResponse("node item is missing required fields") from exc
