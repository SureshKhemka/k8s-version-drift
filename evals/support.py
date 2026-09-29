"""Shared helpers for evals: run the real collector + rule engine against the mock world (EV-17)."""

from __future__ import annotations

from dataclasses import dataclass
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
