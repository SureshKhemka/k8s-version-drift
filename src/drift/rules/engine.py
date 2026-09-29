"""Deterministic drift detection (FR-9..FR-14). No LLM anywhere in here."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from drift.config.models import PolicyConfig
from drift.domain.enums import Rule, Severity
from drift.domain.models import ClusterRef, FetchResult, Matrix, snapshot
from drift.rules.base import CLUSTER, Finding, RuleContext
from drift.rules.control_plane import control_plane_findings
from drift.rules.nodes import node_findings


@dataclass(frozen=True)
class Detection:
    findings: tuple[Finding, ...]
    compliant: tuple[str, ...]  # cluster IDs with no findings (FR-14)
    checked: int  # reachable clusters
    total: int
    nodes_total: int
    nodes_not_ready: int  # D10: recorded, not a rule

    def by_severity(self) -> dict[Severity, int]:
        counts = {s: 0 for s in Severity}
        for f in self.findings:
            counts[f.severity] += 1
        return counts


def sort_key(f: Finding) -> tuple[int, str, str, str]:
    return (-int(f.severity), f.cluster_name, f.rule.value, f.subject)


def detect(
    clusters: Sequence[ClusterRef],
    results: Sequence[FetchResult],
    matrix: Matrix,
    policy: PolicyConfig,
    run_date: date,
) -> Detection:
    ctx = RuleContext(matrix=matrix, policy=policy, run_date=run_date)
    by_id = {r.cluster_id: r for r in results}
    missing = [c.id for c in clusters if c.id not in by_id]
    if missing:
        raise ValueError(f"no fetch result for clusters: {missing}")  # FR-5: never silently dropped

    findings: list[Finding] = []
    compliant: list[str] = []
    checked = nodes_total = not_ready = 0
    for cluster in clusters:
        result = by_id[cluster.id]
        if not result.reachable:
            findings.append(ctx.make(cluster, Rule.UNREACHABLE, CLUSTER, result.reason or "unknown"))
            continue
        checked += 1
        snap = snapshot(cluster, result)
        nodes_total += snap.node_count
        not_ready += sum(p.not_ready for p in snap.pools)
        found = control_plane_findings(snap, ctx) + node_findings(snap, ctx)
        if not found:
            compliant.append(cluster.id)
        findings.extend(found)

    return Detection(
        findings=tuple(sorted(findings, key=sort_key)),
        compliant=tuple(compliant),
        checked=checked,
        total=len(clusters),
        nodes_total=nodes_total,
        nodes_not_ready=not_ready,
    )
