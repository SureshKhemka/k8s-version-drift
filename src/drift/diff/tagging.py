"""Change since last run (FR-16..FR-18). Pure function: baseline + tonight -> tags.

Semantics (decisions D18, D19, D26, D27):
- No baseline -> every finding NEW, ``first_run`` set.
- Cluster not in the baseline inventory -> each of its findings NEW_CLUSTER.
- Same key (cluster, rule, subject) as the baseline -> WORSENED if severity rose or days_remaining
  crossed a configured threshold (``prev > t >= now``), else ONGOING.
- Lifecycle escalation on the same subject (CP_EOL_SOON -> CP_UNSUPPORTED -> CP_EOL) -> WORSENED;
  the superseded baseline finding is *not* reported RESOLVED.
- Cluster UNREACHABLE tonight: its UNREACHABLE finding is NEW if it was reachable last run (FR-17),
  ONGOING otherwise; its other baseline findings are carried forward as *stale* (not RESOLVED).
- Baseline findings of a reachable cluster with no match tonight -> RESOLVED.
- Clusters removed from the inventory: their findings are dropped (listed in ``removed_clusters``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from drift.domain.enums import ChangeTag, Rule
from drift.rules.base import Finding

LIFECYCLE_RANK = {Rule.CP_EOL_SOON: 1, Rule.CP_UNSUPPORTED: 2, Rule.CP_EOL: 3}


@dataclass(frozen=True)
class Baseline:
    run_id: str
    clusters: frozenset[str]  # inventory at that run
    findings: tuple[Finding, ...]  # current + stale findings at that run (never RESOLVED ones)


@dataclass(frozen=True)
class TaggedFinding:
    finding: Finding
    tag: ChangeTag
    previous: Finding | None = None  # matched or escalated-from baseline finding
    reason: str | None = None  # why WORSENED, for audit


@dataclass(frozen=True)
class ChangeSet:
    tagged: tuple[TaggedFinding, ...]  # tonight's findings, one tag each
    resolved: tuple[Finding, ...]  # baseline findings gone tonight
    stale: tuple[Finding, ...]  # carried because their cluster could not be checked
    first_run: bool
    baseline_run_id: str | None
    new_clusters: tuple[str, ...]
    removed_clusters: tuple[str, ...]
    counts: dict[ChangeTag, int] = field(default_factory=dict)


def tag_changes(
    findings: Sequence[Finding],
    inventory_ids: Iterable[str],
    baseline: Baseline | None,
    worsened_days_thresholds: Sequence[int],
) -> ChangeSet:
    inventory = list(dict.fromkeys(inventory_ids))
    if baseline is None:
        first = tuple(TaggedFinding(f, ChangeTag.NEW) for f in findings)
        return _done(first, (), (), True, None, (), ())

    prev_by_cluster: dict[str, list[Finding]] = defaultdict(list)
    for p in baseline.findings:
        prev_by_cluster[p.cluster_id].append(p)
    cur_by_cluster: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        cur_by_cluster[f.cluster_id].append(f)

    new_clusters = tuple(c for c in inventory if c not in baseline.clusters)
    removed = tuple(sorted(baseline.clusters - set(inventory)))
    tagged: list[TaggedFinding] = []
    resolved: list[Finding] = []
    stale: list[Finding] = []

    for cid in inventory:
        current, previous = cur_by_cluster.get(cid, []), prev_by_cluster.get(cid, [])
        if cid in new_clusters:
            tagged += [TaggedFinding(f, ChangeTag.NEW_CLUSTER) for f in current]
            continue
        unreachable_now = any(f.rule is Rule.UNREACHABLE for f in current)
        if unreachable_now:
            prev_unreach = next((p for p in previous if p.rule is Rule.UNREACHABLE), None)
            for f in current:
                tag = ChangeTag.ONGOING if prev_unreach else ChangeTag.NEW
                tagged.append(TaggedFinding(f, tag, prev_unreach))
            stale += [p for p in previous if p.rule is not Rule.UNREACHABLE]
            continue
        t, r = _reachable(current, previous, worsened_days_thresholds)
        tagged += t
        resolved += r

    return _done(tuple(tagged), tuple(resolved), tuple(stale), False, baseline.run_id, new_clusters, removed)


def _reachable(
    current: list[Finding], previous: list[Finding], thresholds: Sequence[int]
) -> tuple[list[TaggedFinding], list[Finding]]:
    prev_by_key = {p.key: p for p in previous}
    consumed: set[tuple[str, Rule, str]] = set()
    out: list[TaggedFinding] = []
    for f in current:
        p = prev_by_key.get(f.key)
        if p is not None:
            consumed.add(p.key)
            why = _worsened(p, f, thresholds)
            out.append(TaggedFinding(f, ChangeTag.WORSENED if why else ChangeTag.ONGOING, p, why))
            continue
        esc = _escalated_from(f, previous, consumed)
        if esc is not None:
            consumed.add(esc.key)
            out.append(TaggedFinding(f, ChangeTag.WORSENED, esc, f"escalated from {esc.rule.value}"))
            continue
        out.append(TaggedFinding(f, ChangeTag.NEW))
    # Keys present tonight are matched above; lifecycle findings superseded by escalation are consumed.
    resolved = [p for p in previous if p.key not in consumed]
    return out, resolved


def _worsened(prev: Finding, cur: Finding, thresholds: Sequence[int]) -> str | None:
    if cur.severity > prev.severity:
        return f"severity {prev.severity.name} -> {cur.severity.name}"
    if prev.days_remaining is not None and cur.days_remaining is not None:
        for t in sorted(thresholds, reverse=True):
            if prev.days_remaining > t >= cur.days_remaining:
                return f"days remaining crossed {t}"
    return None


def _escalated_from(f: Finding, previous: list[Finding], consumed: set[tuple[str, Rule, str]]) -> Finding | None:
    rank = LIFECYCLE_RANK.get(f.rule)
    if rank is None:
        return None
    candidates = [
        p
        for p in previous
        if p.key not in consumed
        and p.subject == f.subject
        and p.rule in LIFECYCLE_RANK
        and LIFECYCLE_RANK[p.rule] < rank
    ]
    return max(candidates, key=lambda p: LIFECYCLE_RANK[p.rule], default=None)


def _done(
    tagged: tuple[TaggedFinding, ...],
    resolved: tuple[Finding, ...],
    stale: tuple[Finding, ...],
    first_run: bool,
    baseline_run_id: str | None,
    new_clusters: tuple[str, ...],
    removed: tuple[str, ...],
) -> ChangeSet:
    counts = {tag: 0 for tag in ChangeTag}
    for t in tagged:
        counts[t.tag] += 1
    counts[ChangeTag.RESOLVED] = len(resolved)
    return ChangeSet(tagged, resolved, stale, first_run, baseline_run_id, new_clusters, removed, counts)
