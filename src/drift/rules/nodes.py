"""Node-pool rules: NODE_SKEW_EXCEEDED, NODE_NEWER_THAN_CP, NODE_POOL_MIXED, NODE_EOL,
and UNPARSEABLE_VERSION for node pools.

Per pool (D4, D15, D17):
- unparseable kubelet versions -> one UNPARSEABLE_VERSION; those nodes are excluded from other rules
- skew and EOL look at the pool's oldest version; "newer" looks at its newest
- skew/newer need a parseable control plane; mixed/EOL do not
"""

from __future__ import annotations

from drift.domain.enums import Rule
from drift.domain.models import ClusterSnapshot, PoolSnapshot
from drift.domain.versions import Version
from drift.rules.base import Finding, RuleContext


def node_findings(snap: ClusterSnapshot, ctx: RuleContext) -> list[Finding]:
    out: list[Finding] = []
    for pool in snap.pools:
        out.extend(_pool(snap, pool, ctx))
    return out


def _pool(snap: ClusterSnapshot, pool: PoolSnapshot, ctx: RuleContext) -> list[Finding]:
    cluster = snap.cluster
    out: list[Finding] = []
    parsed: list[Version] = [r.parsed for _, r in pool.nodes if r.parsed is not None]
    unparsed = sorted({r.raw for _, r in pool.nodes if r.parsed is None})
    if unparsed:
        n = len(pool.nodes) - len(parsed)
        out.append(ctx.make(cluster, Rule.UNPARSEABLE_VERSION, pool.name, ", ".join(unparsed), nodes=n))
    if not parsed:
        return out

    newest = max(parsed)
    minors = sorted({(v.major, v.minor) for v in parsed})
    if len(minors) > 1:
        lagging = sum(1 for v in parsed if (v.major, v.minor) != (newest.major, newest.minor))
        observed = ", ".join(f"{a}.{b}" for a, b in minors)
        out.append(ctx.make(cluster, Rule.NODE_POOL_MIXED, pool.name, observed, nodes=lagging))

    cp = snap.control_plane.parsed
    if cp is not None:
        skew = ctx.policy.skew_max_minor
        same_major = [v for v in parsed if v.major == cp.major]
        too_old = [v for v in same_major if cp.minor - v.minor > skew]
        if too_old:
            limit = f"{cp.major}.{cp.minor - skew}"
            out.append(
                ctx.make(
                    cluster, Rule.NODE_SKEW_EXCEEDED, pool.name, str(min(too_old)), limit=limit, nodes=len(too_old)
                )
            )
        too_new = [v for v in same_major if v.minor > cp.minor]
        if too_new:
            out.append(
                ctx.make(
                    cluster,
                    Rule.NODE_NEWER_THAN_CP,
                    pool.name,
                    str(max(too_new)),
                    limit=cp.minor_key,
                    nodes=len(too_new),
                )
            )

    eol_nodes = []
    for v in parsed:
        entry = ctx.matrix.entry(cluster.provider, v.minor_key)
        if entry is not None and ctx.days_until(entry.end_of_life) <= 0:
            eol_nodes.append(v)
    if eol_nodes:
        worst = min(eol_nodes)
        entry = ctx.matrix.entry(cluster.provider, worst.minor_key)
        assert entry is not None
        eol = entry.end_of_life
        out.append(
            ctx.make(cluster, Rule.NODE_EOL, pool.name, str(worst), limit=eol.isoformat(), on=eol, nodes=len(eol_nodes))
        )
    return out
