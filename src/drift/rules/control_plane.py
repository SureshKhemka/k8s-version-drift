"""Control-plane rules: CP_EOL, CP_UNSUPPORTED, CP_EOL_SOON, CP_UNKNOWN_VERSION, CP_PATCH_BEHIND,
and UNPARSEABLE_VERSION for the control plane.

Lifecycle rules are exclusive (D3): only the most severe of CP_EOL > CP_UNSUPPORTED > CP_EOL_SOON.
A date is reached on the day itself (D14).
"""

from __future__ import annotations

from drift.domain.enums import Rule
from drift.domain.models import ClusterSnapshot
from drift.rules.base import CONTROL_PLANE, Finding, RuleContext


def control_plane_findings(snap: ClusterSnapshot, ctx: RuleContext) -> list[Finding]:
    cluster, reading = snap.cluster, snap.control_plane
    if reading.parsed is None:
        return [ctx.make(cluster, Rule.UNPARSEABLE_VERSION, CONTROL_PLANE, reading.raw)]

    version = reading.parsed
    observed = str(version)
    entry = ctx.matrix.entry(cluster.provider, version.minor_key)
    if entry is None:
        known = ctx.matrix.known_range(cluster.provider)
        return [ctx.make(cluster, Rule.CP_UNKNOWN_VERSION, CONTROL_PLANE, observed, limit=known)]

    out: list[Finding] = []
    eos, eol = entry.end_of_standard_support, entry.end_of_life
    if ctx.days_until(eol) <= 0:
        out.append(ctx.make(cluster, Rule.CP_EOL, CONTROL_PLANE, observed, limit=eol.isoformat(), on=eol))
    elif ctx.days_until(eos) <= 0:
        out.append(ctx.make(cluster, Rule.CP_UNSUPPORTED, CONTROL_PLANE, observed, limit=eos.isoformat(), on=eol))
    elif ctx.days_until(eos) <= ctx.policy.warning_window_days:
        # EOS is in the future and EOS <= EOL, so EOS is the earliest upcoming date.
        out.append(ctx.make(cluster, Rule.CP_EOL_SOON, CONTROL_PLANE, observed, limit=eos.isoformat(), on=eos))

    if entry.latest - version.patch >= ctx.policy.patch_behind_threshold:
        out.append(ctx.make(cluster, Rule.CP_PATCH_BEHIND, CONTROL_PLANE, observed, limit=entry.latest_patch))
    return out
