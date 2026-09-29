"""The fact sheet: the only facts the LLM sees and the only facts a message may state (CH-4).

Built by code from the findings, change tags and run statistics. Prompts render it; Gate 2 checks drafts
against it. Findings are exposed to the model under short per-run aliases (``f1``, ``f2``...) so the model
never has to copy long IDs.
"""

from __future__ import annotations

import contextlib
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from drift.diff.tagging import ChangeSet
from drift.domain.enums import ChangeTag, Rule, Severity
from drift.message.compose import RunFacts
from drift.rules.base import Finding

RULE_TEXT = {
    Rule.CP_EOL: "control plane is past end of life",
    Rule.CP_UNSUPPORTED: "control plane is past end of standard support (extended support)",
    Rule.CP_EOL_SOON: "control plane reaches end of support soon",
    Rule.CP_UNKNOWN_VERSION: "control plane version is not in the supported-version matrix",
    Rule.CP_PATCH_BEHIND: "control plane is several patches behind the latest patch",
    Rule.NODE_SKEW_EXCEEDED: "node pool is too many minor versions behind the control plane",
    Rule.NODE_NEWER_THAN_CP: "node pool is newer than the control plane",
    Rule.NODE_POOL_MIXED: "node pool runs mixed minor versions (upgrade in progress or stuck)",
    Rule.NODE_EOL: "node pool runs a minor version past end of life",
    Rule.UNREACHABLE: "cluster could not be checked",
    Rule.UNPARSEABLE_VERSION: "a reported version could not be parsed",
}

VERSION_RX = re.compile(r"(?<![\w.])v?(\d+\.\d+(?:\.\d+)?)(?!\w|\.\d)")


def versions_in(text: str | None) -> set[str]:
    """Every version-looking token in ``text`` plus the minor of each (``1.28.15`` -> ``1.28.15``, ``1.28``)."""
    out: set[str] = set()
    for m in VERSION_RX.finditer(text or ""):
        v = m.group(1)
        out.add(v)
        parts = v.split(".")
        out.add(f"{parts[0]}.{parts[1]}")
    return out


def dates_in(text: str | None) -> set[date]:
    out = set()
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text or ""):
        with contextlib.suppress(ValueError):
            out.add(date(int(m[1]), int(m[2]), int(m[3])))
    return out


@dataclass(frozen=True)
class FactFinding:
    alias: str
    finding: Finding
    tag: ChangeTag

    def as_prompt_dict(self) -> dict[str, Any]:
        f = self.finding
        d: dict[str, Any] = {
            "id": self.alias,
            "cluster": f.cluster_name,
            "environment": f.environment.value,
            "rule": f.rule.value,
            "severity": f.severity.name,
            "subject": f.subject,
            "observed": f.observed,
            "change": self.tag.value,
        }
        if f.limit:
            d["limit"] = f.limit
        if f.date:
            d["date"] = f.date.isoformat()
        if f.days_remaining is not None:
            d["days_remaining"] = f.days_remaining
        if f.nodes_affected is not None:
            d["nodes_affected"] = f.nodes_affected
        return d


@dataclass(frozen=True)
class ClusterFacts:
    versions: frozenset[str]
    dates: frozenset[date]
    rules: frozenset[Rule]
    severities: frozenset[Severity]
    nodes: frozenset[int] = frozenset()  # each nodes_affected, and their sum
    days: frozenset[int] = frozenset()  # |days_remaining| of each finding


@dataclass(frozen=True)
class Theme:
    title: str
    priority: int
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class FactSheet:
    facts: RunFacts
    findings: tuple[FactFinding, ...]
    resolved: tuple[Finding, ...]
    inventory_names: frozenset[str]
    inventory_ids: frozenset[str]
    literals: tuple[str, ...]  # strings masked before checking (run ID, matrix version, mentions, reasons)
    themes: tuple[Theme, ...] = ()
    # derived
    clusters: dict[str, ClusterFacts] = field(default_factory=dict)
    versions: frozenset[str] = frozenset()
    dates: frozenset[date] = frozenset()
    pools: frozenset[str] = frozenset()
    counts: dict[str, frozenset[int]] = field(default_factory=dict)

    # ------------------------------------------------------------------ build

    @classmethod
    def build(
        cls,
        facts: RunFacts,
        changes: ChangeSet,
        inventory_names: Iterable[str],
        inventory_ids: Iterable[str],
        literals: Sequence[str] = (),
    ) -> FactSheet:
        tagged = sorted(
            changes.tagged,
            key=lambda t: (-int(t.finding.severity), t.finding.cluster_name, t.finding.rule.value, t.finding.subject),
        )
        findings = tuple(FactFinding(f"f{i}", t.finding, t.tag) for i, t in enumerate(tagged, start=1))
        reasons = tuple(
            f.observed
            for f in (*(x.finding for x in findings), *changes.resolved)
            if f.rule in (Rule.UNREACHABLE, Rule.UNPARSEABLE_VERSION)
        )
        sheet = cls(
            facts=facts,
            findings=findings,
            resolved=tuple(changes.resolved),
            inventory_names=frozenset(inventory_names),
            inventory_ids=frozenset(inventory_ids),
            literals=tuple(sorted({*literals, facts.run_id, facts.matrix_version, *reasons}, key=len, reverse=True)),
        )
        return sheet._derive()

    def with_themes(self, themes: Sequence[Theme]) -> FactSheet:
        return replace(self, themes=tuple(sorted(themes, key=lambda t: t.priority)))._derive()

    def _derive(self) -> FactSheet:
        per: dict[str, dict[str, set[Any]]] = defaultdict(lambda: defaultdict(set))
        all_f = [ff.finding for ff in self.findings] + list(self.resolved)
        for f in all_f:
            c = per[f.cluster_name]
            c["versions"] |= versions_in(f.observed) | versions_in(f.limit)
            c["dates"] |= ({f.date} if f.date else set()) | dates_in(f.limit)
            c["rules"].add(f.rule)
            c["severities"].add(f.severity)
            if f.nodes_affected is not None:
                c["nodes"].add(f.nodes_affected)
                c["node_items"].add((f.id, f.nodes_affected))
            if f.days_remaining is not None:
                c["days"].add(abs(f.days_remaining))
        clusters = {
            name: ClusterFacts(
                frozenset(v["versions"]),
                frozenset(v["dates"]),
                frozenset(v["rules"]),
                frozenset(v["severities"]),
                frozenset(v["nodes"] | ({sum(n for _, n in v["node_items"])} if v["node_items"] else set())),
                frozenset(v["days"]),
            )
            for name, v in per.items()
        }
        versions = frozenset().union(*(c.versions for c in clusters.values())) if clusters else frozenset()
        dates = frozenset({self.facts.run_date}).union(*(c.dates for c in clusters.values()))
        pools = frozenset(f.subject for f in all_f if f.subject not in ("control-plane", "cluster"))
        return replace(self, clusters=clusters, versions=versions, dates=dates, pools=pools, counts=self._counts())

    def _counts(self) -> dict[str, frozenset[int]]:
        fx = self.facts
        cur = [ff.finding for ff in self.findings]
        tags = {ff.finding.id: ff.tag for ff in self.findings}
        by_alias = {ff.alias: ff for ff in self.findings}

        cluster: set[int] = {
            fx.total,
            fx.checked,
            fx.compliant,
            fx.unreachable,
            fx.clusters_with_findings,
            fx.new_clusters,
            len({f.cluster_id for f in self.resolved}),
        }
        finding: set[int] = {len(cur), len(self.resolved), *fx.severity_counts.values(), *fx.change_counts.values()}
        node: set[int] = {f.nodes_affected for f in cur + list(self.resolved) if f.nodes_affected is not None}
        day: set[int] = {abs(f.days_remaining) for f in cur if f.days_remaining is not None}
        sev: dict[Severity, set[int]] = defaultdict(set)
        chg: dict[str, set[int]] = defaultdict(set)

        def group(fs: list[Finding]) -> None:
            if not fs:
                return
            cluster.add(len({f.cluster_id for f in fs}))
            finding.add(len(fs))
            nodes = [f.nodes_affected for f in fs if f.nodes_affected is not None]
            if nodes:
                node.add(sum(nodes))

        for key in ("rule", "severity", "environment"):
            buckets: dict[Any, list[Finding]] = defaultdict(list)
            for f in cur:
                buckets[getattr(f, key)].append(f)
            for fs in buckets.values():
                group(fs)
        minors: dict[str, list[Finding]] = defaultdict(list)
        for f in cur:
            for v in versions_in(f.observed):
                minors[v].append(f)
        for fs in minors.values():
            group(fs)
        for s in Severity:
            fs = [f for f in cur if f.severity is s]
            sev[s] |= {len(fs), len({f.cluster_id for f in fs})}
        for t in ChangeTag:
            fs = [f for f in cur if tags[f.id] is t] if t is not ChangeTag.RESOLVED else list(self.resolved)
            chg[t.value] |= {len(fs), len({f.cluster_id for f in fs})}
        chg[ChangeTag.NEW_CLUSTER.value].add(fx.new_clusters)
        for th in self.themes:
            fs = [by_alias[a].finding for a in th.aliases if a in by_alias]
            group(fs)
            for s in Severity:
                n = sum(1 for f in fs if f.severity is s)
                if n:
                    sev[s].add(n)
            for t in ChangeTag:
                n = sum(1 for f in fs if tags[f.id] is t)
                if n:
                    chg[t.value].add(n)

        unreachable = [f for f in cur if f.rule is Rule.UNREACHABLE]
        qual: dict[str, set[int]] = {
            "cluster:unreachable": {fx.unreachable},
            "cluster:compliant": {fx.compliant},
            "cluster:checked": {fx.checked, fx.total},
        }
        for env in ("prod", "staging", "dev"):
            in_env = [f for f in cur if f.environment.value == env]
            qual["cluster:unreachable"].add(len({f.cluster_id for f in unreachable if f.environment.value == env}))
            qual[f"cluster:{env}"] = {len({f.cluster_id for f in in_env})}
            for key in ("rule", "severity"):
                buckets2: dict[Any, set[str]] = defaultdict(set)
                for f in in_env:
                    buckets2[getattr(f, key)].add(f.cluster_id)
                qual[f"cluster:{env}"] |= {len(v) for v in buckets2.values()}
        for th in self.themes:
            fs = [by_alias[a].finding for a in th.aliases if a in by_alias]
            n_unr = len({f.cluster_id for f in fs if f.rule is Rule.UNREACHABLE})
            if n_unr:
                qual["cluster:unreachable"].add(n_unr)

        out = {"cluster": cluster, "finding": finding, "node": node, "day": day, **qual}
        out |= {f"severity:{s.name}": v for s, v in sev.items()}
        out |= {f"change:{k}": v for k, v in chg.items()}
        allv: set[int] = set().union(*out.values())
        out["any"] = allv
        return {k: frozenset(v) for k, v in out.items()}

    # ------------------------------------------------------------------ lookups

    def by_alias(self) -> dict[str, FactFinding]:
        return {ff.alias: ff for ff in self.findings}

    def required_clusters(self) -> set[str]:
        """CH-4: clusters a message must name: any CRITICAL finding, or UNREACHABLE in prod."""
        return {
            ff.finding.cluster_name
            for ff in self.findings
            if ff.finding.severity is Severity.CRITICAL
            or (ff.finding.rule is Rule.UNREACHABLE and ff.finding.environment.value == "prod")
        }

    def prompt_stats(self) -> dict[str, Any]:
        fx = self.facts
        return {
            "run_date": fx.run_date.isoformat(),
            "first_run": fx.first_run,
            "clusters_total": fx.total,
            "clusters_checked": fx.checked,
            "clusters_compliant": fx.compliant,
            "clusters_unreachable": fx.unreachable,
            "clusters_with_findings": fx.clusters_with_findings,
            "new_clusters": fx.new_clusters,
            "findings_by_severity": {s.name: n for s, n in sorted(fx.severity_counts.items(), reverse=True) if n},
            "changes": {t.value: n for t, n in fx.change_counts.items() if n},
        }
