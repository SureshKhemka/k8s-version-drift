"""One run of the workflow: collect -> detect -> diff -> summarise -> compose -> publish -> record.

Step IDs (stable, FR-31): config, inventory, collect, detect, diff, summarise[.<chain step>], compose, publish, finish.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

import httpx

from drift.collect.collector import Collector
from drift.collect.sources import SourceError, load_inventory, load_matrix, select
from drift.config import AppConfig, masked_dump
from drift.diff.tagging import Baseline, ChangeSet, tag_changes
from drift.domain.clock import Clock
from drift.domain.enums import ExitCode, OutputMode, Rule
from drift.domain.models import snapshot
from drift.message.compose import (
    Fmt,
    RunFacts,
    SummarySource,
    all_clear_body,
    appendix,
    assemble,
    body_budget,
    fallback_body,
    footer,
    is_all_clear,
    mentions_line,
)
from drift.publish.channel import ChannelPublisher, PostResult, write_outbox
from drift.rules.engine import Detection, detect
from drift.store.recorder import Recorder, Redactor, new_run_id
from drift.store.sqlite import Store

log = logging.getLogger("drift.pipeline")

TransportFactory = Callable[[], httpx.AsyncBaseTransport]


@dataclass(frozen=True)
class SummaryRequest:
    facts: RunFacts
    detection: Detection
    changes: ChangeSet
    fmt: Fmt
    body_budget: int  # characters the body may use (CH-4 length check)


@dataclass(frozen=True)
class SummaryResult:
    body: str
    model: str | None = None


class Summarizer(Protocol):
    """The LLM chain (M6). Returns None when it cannot produce a body that passed every gate."""

    async def summarize(self, req: SummaryRequest, recorder: Recorder) -> SummaryResult | None: ...


@dataclass
class RunOutcome:
    run_id: str | None
    run_date: date
    exit_code: ExitCode
    status: str
    message: str | None = None
    summary_source: SummarySource | None = None
    post: PostResult | None = None
    detection: Detection | None = None
    changes: ChangeSet | None = None
    errors: list[str] = field(default_factory=list)


def exit_code_for(unreachable: bool, source: SummarySource | None, post: PostResult | None) -> ExitCode:
    """FR-29, most severe wins: post failed > partial > fallback > success."""
    if post is not None and post.status == "POST_FAILED":
        return ExitCode.POST_FAILED
    if unreachable:
        return ExitCode.PARTIAL
    if source == "fallback":
        return ExitCode.SUCCESS_FALLBACK
    return ExitCode.SUCCESS


def scrub(text: str, patterns: Sequence[str]) -> tuple[str, list[str]]:
    """Last line of defence (CH-4, EV-6): remove deny-listed strings from a code-built message."""
    hits: list[str] = []
    for p in patterns:
        rx = re.compile(p)
        if rx.search(text):
            hits.append(p)
            text = rx.sub("[removed]", text)
    return text, hits


async def run_once(
    cfg: AppConfig,
    *,
    clock: Clock,
    run_date: date | None = None,
    environ: Mapping[str, str],
    clusters: Sequence[str] | None = None,
    force: bool = False,
    mode: OutputMode | None = None,
    summarizer: Summarizer | None = None,
    k8s_transport: TransportFactory | None = None,
    channel_transport: TransportFactory | None = None,
) -> RunOutcome:
    run_date = run_date or clock.now().date()
    mode = OutputMode(mode or cfg.output.mode)
    subset = bool(clusters)
    if subset and mode is OutputMode.POST:
        log.warning("subset runs are always DRY_RUN (D30); ignoring POST")
        mode = OutputMode.DRY_RUN

    # --- sources: a failure here is a configuration error, before any cluster is contacted (CF-4)
    try:
        inventory = load_inventory(cfg.sources.inventory)
        matrix = load_matrix(cfg.sources.matrix)
        selected = select(inventory, clusters)
    except SourceError as exc:
        return RunOutcome(None, run_date, ExitCode.CONFIG_ERROR, "CONFIG_ERROR", errors=[str(exc)])

    store = Store(cfg.storage.db_path)
    run_id = new_run_id(run_date)
    redactor = Redactor.from_env(
        [cfg.clusters.token_env, cfg.channel.webhook_url_env, *(p.api_key_env for p in cfg.llm.providers.values())],
        environ,
        cfg.deny_list,
    )
    rec = Recorder(store, run_id, cfg.storage.runs_dir, clock, redactor)

    def now() -> str:
        return clock.now().isoformat(timespec="seconds")

    store.create_run(run_id, run_date, now())
    try:
        rec.event(
            "config",
            "effective_config",
            {"config": masked_dump(cfg), "mode": mode.value, "force": force, "subset": list(clusters or [])},
        )
        rec.event(
            "inventory",
            "inventory_snapshot",
            {
                "clusters": [c.model_dump(mode="json", exclude={"api_server"}) for c in selected],
                "matrix_version": matrix.version,
            },
        )

        # --- collect
        t0 = time.perf_counter()
        results = await Collector(cfg.clusters, environ, k8s_transport).collect(selected)
        for r in results:
            rec.event("collect", "fetch_result", r)
        log.info(
            "collected %d/%d clusters in %.1fs",
            sum(r.reachable for r in results),
            len(results),
            time.perf_counter() - t0,
        )

        # --- detect
        by_id = {c.id: c for c in selected}
        for r in results:
            if r.reachable:
                s = snapshot(by_id[r.cluster_id], r)
                rec.event(
                    "detect",
                    "normalised",
                    {
                        "cluster_id": r.cluster_id,
                        "control_plane": {"raw": s.control_plane.raw, "normalised": _v(s.control_plane.parsed)},
                        "pools": {p.name: sorted({f"{v.raw} -> {_v(v.parsed)}" for _, v in p.nodes}) for p in s.pools},
                    },
                )
        det = detect(selected, results, matrix, cfg.policy, run_date)
        rec.event(
            "detect",
            "detection",
            {"checked": det.checked, "total": det.total, "compliant": list(det.compliant), "findings": det.findings},
        )

        # --- diff
        baseline = store.load_baseline(run_date)
        if subset and baseline is not None:
            keep = {c.id for c in selected}
            baseline = Baseline(
                baseline.run_id, baseline.clusters & keep, tuple(f for f in baseline.findings if f.cluster_id in keep)
            )
        changes = tag_changes(det.findings, [c.id for c in selected], baseline, cfg.policy.worsened_days_thresholds)
        rec.event("diff", "changes", changes)
        if not subset:  # subset runs never become a baseline (D30)
            store.save_detection(
                run_id, run_date, matrix.version, {r.cluster_id: r.reachable for r in results}, changes
            )

        # --- summarise + compose
        fmt = Fmt(cfg.channel.format)
        facts = RunFacts.build(run_id, run_date, det, changes, matrix.version)
        mentions = mentions_line(det.findings, cfg.channel.mentions.on_critical)
        appx = appendix(det.findings, fmt)
        source: SummarySource
        model: str | None = None
        if is_all_clear(det.findings):
            source, body = "all_clear", all_clear_body(facts, fmt)
        else:
            source, body = "fallback", fallback_body(facts, fmt)
            if summarizer is not None and cfg.llm.enabled:
                foot_probe = footer(facts, "llm", fmt, cfg.llm.for_step("draft").model)
                req = SummaryRequest(
                    facts, det, changes, fmt, body_budget(cfg.channel.max_length, mentions, appx, foot_probe)
                )
                try:
                    result = await summarizer.summarize(req, rec)
                except Exception as exc:
                    rec.event("summarise", "chain_error", {"error_type": type(exc).__name__})
                    result = None
                if result is not None:
                    source, body, model = "llm", result.body, result.model
        foot = footer(facts, source, fmt, model)
        message, hits = scrub(assemble(mentions, body, appx, foot), cfg.deny_list)
        rec.event(
            "compose", "message", {"source": source, "length": len(message), "deny_list_hits": hits, "text": message}
        )

        # --- publish
        post = await _publish(
            cfg, store, rec, run_id, run_date, message, source, mode, force, environ, channel_transport
        )
        code = exit_code_for(any(f.rule is Rule.UNREACHABLE for f in det.findings), source, post)
        status = code.name
        store.prune(run_date, cfg.storage.retention_days)
        rec.event("finish", "outcome", {"exit_code": int(code), "status": status, "post": post.status})
        store.finish_run(
            run_id, now(), status, int(code), {"source": source, "post": post.status, "length": len(message)}
        )
        return RunOutcome(run_id, run_date, code, status, message, source, post, det, changes)
    except BaseException as exc:
        rec.event("finish", "crash", {"error_type": type(exc).__name__})
        store.finish_run(run_id, now(), "CRASHED", 1)
        raise
    finally:
        store.close()


async def _publish(
    cfg: AppConfig,
    store: Store,
    rec: Recorder,
    run_id: str,
    run_date: date,
    message: str,
    source: SummarySource,
    mode: OutputMode,
    force: bool,
    environ: Mapping[str, str],
    channel_transport: TransportFactory | None,
) -> PostResult:
    meta: dict[str, object] = {"run_id": run_id, "run_date": run_date.isoformat(), "source": source, "mode": mode.value}
    outbox = write_outbox(cfg.output.outbox_dir, run_date.isoformat(), run_id, message, meta)
    if source == "all_clear" and not cfg.channel.post_all_clear:
        result = PostResult("SILENT", str(outbox))
    elif mode is OutputMode.DRY_RUN:
        result = PostResult("DRY_RUN", str(outbox))
    elif (prior := store.post_for(run_date)) is not None and not force:
        # FR-26: at most one post per run date unless forced; the local record is still updated.
        result = PostResult("ALREADY_POSTED", str(outbox), ({"previous_run_id": prior["run_id"]},))
    else:
        ok, attempts = await ChannelPublisher(cfg.channel, cfg.output, environ, channel_transport).post(message)
        if ok:
            store.record_post(run_date, run_id, rec.clock.now().isoformat(timespec="seconds"), force)
        result = PostResult("POSTED" if ok else "POST_FAILED", str(outbox), attempts)
    rec.event(
        "publish", "post_result", {"status": result.status, "attempts": result.attempts, "outbox": result.outbox_path}
    )
    write_outbox(cfg.output.outbox_dir, run_date.isoformat(), run_id, message, meta | {"post": result.status})
    return result


def _v(parsed: object) -> str | None:
    return str(parsed) if parsed is not None else None
