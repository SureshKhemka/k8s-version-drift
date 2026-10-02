"""Shared helpers for evals: run the real collector + rule engine against the mock world (EV-17)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import yaml

from drift.collect.collector import Collector
from drift.collect.sources import load_inventory, load_matrix
from drift.config import AppConfig, load_config
from drift.config.models import SourceConfig
from drift.domain.models import FetchResult, Inventory, Matrix
from drift.rules.base import Finding
from drift.rules.engine import Detection, detect
from drift_mocks.k8s_api import K8sMockState, create_app

REPO = Path(__file__).resolve().parents[1]
SEED = REPO / "seed"
EXPECTED = REPO / "evals" / "expected"
NIGHTS = tuple(range(5))


def eval_config() -> AppConfig:
    """Fixed config for evals: defaults + config/eval.yaml, no env vars."""
    return load_config(REPO / "config", env="eval", environ={})


def night_date(night: int) -> date:
    scenario = yaml.safe_load((SEED / "scenario.yaml").read_text())
    return date.fromisoformat(scenario["nights"][night]["date"])


@dataclass
class NightRun:
    night: int
    run_date: date
    inventory: Inventory
    matrix: Matrix
    results: list[FetchResult]
    detection: Detection
    mock: K8sMockState


async def run_night(state_dir: Path, night: int, cfg: AppConfig | None = None) -> NightRun:
    cfg = cfg or eval_config()
    mock = K8sMockState(SEED, state_dir, None, timeout_sleep_s=30)
    mock.set_night(night)
    app = create_app(mock)
    inventory = load_inventory(SourceConfig(type="file", path=mock.inventory_path))
    matrix = load_matrix(cfg.sources.matrix)
    collector = Collector(cfg.clusters, environ={}, transport_factory=lambda: httpx.ASGITransport(app=app))
    results = await collector.collect(inventory.clusters)
    run_date = night_date(night)
    detection = detect(inventory.clusters, results, matrix, cfg.policy, run_date)
    return NightRun(night, run_date, inventory, matrix, results, detection, mock)


_CACHE: dict[int, NightRun] = {}


async def night_run(night: int) -> NightRun:
    """Cached run_night with the eval config. Collection is deterministic per night (EV-17), so each night
    is collected once per test session. Use ``run_night`` directly when a test needs a fresh mock call log."""
    if night not in _CACHE:
        import tempfile

        _CACHE[night] = await run_night(Path(tempfile.mkdtemp(prefix=f"drift-eval-n{night}-")), night)
    return _CACHE[night]


def expected_findings(night: int) -> list[dict[str, Any]]:
    return yaml.safe_load((EXPECTED / f"findings_night_{night}.yaml").read_text())["findings"]


def finding_row(f: Finding) -> dict[str, Any]:
    """Project a Finding onto the columns the hand-written expectations use."""
    return {
        "cluster": f.cluster_id,
        "rule": f.rule.value,
        "subject": f.subject,
        "severity": f.severity.name,
        "observed": f.observed,
        "limit": f.limit,
        "date": f.date.isoformat() if f.date else None,
        "days_remaining": f.days_remaining,
        "nodes_affected": f.nodes_affected,
    }


def expected_tags(night: int) -> dict[str, Any]:
    return yaml.safe_load((EXPECTED / f"tags_night_{night}.yaml").read_text())


async def run_sequence(tmp: Path, nights: tuple[int, ...] = NIGHTS):  # type: ignore[no-untyped-def]
    """Run nights in order through collection, detection, change tagging and the store. Yields per night."""
    from drift.diff.tagging import tag_changes
    from drift.store.sqlite import Store

    cfg = eval_config()
    store = Store(tmp / "drift.db")
    try:
        for night in nights:
            run = await night_run(night)
            baseline = store.load_baseline(run.run_date)
            changes = tag_changes(
                run.detection.findings,
                [c.id for c in run.inventory.clusters],
                baseline,
                cfg.policy.worsened_days_thresholds,
            )
            run_id = f"eval-night-{night}"
            store.create_run(run_id, run.run_date, f"{run.run_date}T02:00:00+05:30")
            store.save_detection(
                run_id, run.run_date, run.matrix.version, {r.cluster_id: r.reachable for r in run.results}, changes
            )
            yield run, changes, store
    finally:
        store.close()


# ------------------------------------------------------------------------------ full pipeline runs

EVAL_SECRETS = {
    "K8S_TOKEN": "k8s-token-DO-NOT-LEAK-5f1e",
    "DRIFT_CHANNEL_WEBHOOK_URL": "http://channel.mock/webhook/T-SECRET-HOOK-9a7c",
    "OPENROUTER_API_KEY": "sk-or-DO-NOT-LEAK-77aa",
}


@dataclass
class PipelineEnv:
    """A fully wired, in-process world: mock k8s + mock channel + store paths under ``root``."""

    root: Path
    cfg: AppConfig
    k8s: K8sMockState
    channel: Any  # ChannelMockState
    environ: dict[str, str]
    seq: list[int] = field(default_factory=lambda: [0])  # shared run counter -> deterministic run IDs

    def __post_init__(self) -> None:
        from drift_mocks.channel import create_app as channel_app

        self._k8s_app = create_app(self.k8s)
        self._channel_app = channel_app(self.channel)

    def k8s_transport(self) -> httpx.AsyncBaseTransport:
        return httpx.ASGITransport(app=self._k8s_app)

    def channel_transport(self) -> httpx.AsyncBaseTransport:
        return httpx.ASGITransport(app=self._channel_app)

    def messages(self) -> list[str]:
        return [m["text"] for m in self.channel.messages()]

    def with_cfg(self, **updates: Any) -> PipelineEnv:
        """Same world, config sections replaced (e.g. ``llm=...``). Shares mocks, store and outbox."""
        env = PipelineEnv(
            self.root, self.cfg.model_copy(update=updates), self.k8s, self.channel, self.environ, self.seq
        )
        return env


def pipeline_env(root: Path) -> PipelineEnv:
    from drift.domain.enums import OutputMode
    from drift_mocks.channel import ChannelMockState

    base = eval_config()
    k8s = K8sMockState(SEED, root / "mocks", None, timeout_sleep_s=30)
    channel = ChannelMockState(root / "mocks", None, timeout_sleep_s=30)
    cfg = base.model_copy(
        update={
            "sources": base.sources.model_copy(
                update={"inventory": SourceConfig(type="file", path=k8s.inventory_path)}
            ),
            "storage": base.storage.model_copy(update={"db_path": root / "drift.db", "runs_dir": root / "runs"}),
            "output": base.output.model_copy(update={"outbox_dir": root / "outbox", "mode": OutputMode.POST}),
            "clusters": base.clusters.model_copy(
                update={"token_env": "K8S_TOKEN", "endpoint_template": "http://k8s.mock/clusters/{id}"}
            ),
        }
    )
    return PipelineEnv(root, cfg, k8s, channel, dict(EVAL_SECRETS))


async def run_pipeline_night(env: PipelineEnv, night: int, **kwargs: Any):  # type: ignore[no-untyped-def]
    """One full ``run_once`` for a scenario night, with a fixed clock (EV-17)."""
    from drift.domain.clock import FixedClock
    from drift.pipeline import run_once

    if env.k8s.world.night != night:
        env.k8s.set_night(night)
    day = night_date(night)
    env.seq[0] += 1
    kwargs.setdefault("run_id", f"{day.isoformat()}-ev{env.seq[0]:04d}")
    return await run_once(
        env.cfg,
        clock=FixedClock.on(day),
        run_date=day,
        environ=env.environ,
        k8s_transport=env.k8s_transport,
        channel_transport=env.channel_transport,
        **kwargs,
    )


# ------------------------------------------------------------------------------------ fact sheets

_SHEETS: dict[int, Any] = {}


async def fact_sheet(night: int):  # type: ignore[no-untyped-def]
    """The Gate 2 fact sheet for a scenario night (nights before it are replayed for change tags)."""
    import tempfile

    from drift.chain.facts import FactSheet
    from drift.message.compose import RunFacts

    if night not in _SHEETS:
        cfg = eval_config()
        async for run, changes, _ in run_sequence(Path(tempfile.mkdtemp()), nights=tuple(range(night + 1))):
            facts = RunFacts.build(f"eval-{night}", run.run_date, run.detection, changes, run.matrix.version)
            _SHEETS[run.night] = FactSheet.build(
                facts,
                changes,
                [c.name for c in run.inventory.clusters],
                [c.id for c in run.inventory.clusters],
                cfg.channel.mentions.on_critical,
            )
    return _SHEETS[night]


def sheet_for_outcome(env: PipelineEnv, out: Any):  # type: ignore[no-untyped-def]
    """Rebuild the fact sheet for a finished pipeline run (actual run ID, inventory at that night)."""
    from drift.chain.facts import FactSheet
    from drift.collect.sources import load_matrix
    from drift.message.compose import RunFacts

    matrix_version = load_matrix(env.cfg.sources.matrix).version
    facts = RunFacts.build(out.run_id, out.run_date, out.detection, out.changes, matrix_version)
    clusters = list(env.k8s.world.clusters.values())
    return FactSheet.build(
        facts,
        out.changes,
        [c["name"] for c in clusters],
        [c["id"] for c in clusters],
        env.cfg.channel.mentions.on_critical,
    )
