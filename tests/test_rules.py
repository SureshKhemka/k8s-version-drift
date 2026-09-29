"""Rule engine edge cases beyond the seed scenario (FR-9..FR-15)."""

from datetime import date
from pathlib import Path

import pytest

from drift.collect.sources import load_matrix
from drift.config import load_config
from drift.config.models import SourceConfig
from drift.domain.enums import Rule, Severity
from drift.domain.models import ClusterRef, FetchResult, NodeRecord
from drift.rules.base import finding_id
from drift.rules.engine import detect

REPO = Path(__file__).resolve().parents[1]
CFG = load_config(REPO / "config", environ={})
MATRIX = load_matrix(SourceConfig(type="file", path=REPO / "seed" / "matrix.yaml"))
N0 = date(2026, 10, 1)


def _cluster(provider="eks", env="prod", cid="x1"):
    return ClusterRef(id=cid, name=f"name-{cid}", provider=provider, environment=env, region="r", team="t")


def _run(cp, pools=None, provider="eks", env="prod", on=N0, policy=None):
    c = _cluster(provider, env)
    nodes = [
        NodeRecord(name=f"{pool}-{i}", pool=pool, kubelet_version=v, ready=True)
        for pool, versions in (pools or {}).items()
        for i, v in enumerate(versions)
    ]
    res = FetchResult(cluster_id=c.id, reachable=True, control_plane_version=cp, nodes=tuple(nodes))
    return detect([c], [res], MATRIX, policy or CFG.policy, on).findings


def _rules(findings):
    return sorted((f.rule.value, f.subject) for f in findings)


def test_compliant_cluster_has_no_findings():
    assert _run("v1.35.4", {"a": ["v1.35.4", "v1.35.3"]}) == ()


def test_patch_newer_than_matrix_latest_is_not_behind():
    assert _run("v1.35.9") == ()


def test_eol_date_boundaries_move_with_injected_date():
    # EKS 1.30: EOS 2026-07-23, EOL 2026-10-02
    assert _rules(_run("v1.30.12", on=date(2026, 10, 1))) == [("CP_UNSUPPORTED", "control-plane")]
    assert _rules(_run("v1.30.12", on=date(2026, 10, 2))) == [("CP_EOL", "control-plane")]
    # EKS 1.32: EOS 2026-11-29 -> EOL_SOON appears exactly 60 days before
    assert _run("v1.32.7", on=date(2026, 9, 29)) == ()
    (f,) = _run("v1.32.7", on=date(2026, 9, 30))
    assert (f.rule, f.days_remaining) == (Rule.CP_EOL_SOON, 60)


def test_self_managed_eol_soon_uses_eol_date():
    (f,) = _run("v1.31.9", provider="self-managed")
    assert (f.rule, f.limit, f.days_remaining) == (Rule.CP_EOL_SOON, "2026-11-01", 31)


def test_patch_behind_is_independent_of_lifecycle():
    assert _rules(_run("v1.31.1")) == [("CP_PATCH_BEHIND", "control-plane"), ("CP_UNSUPPORTED", "control-plane")]


def test_pool_with_two_eol_minors_reports_oldest_and_counts_all():
    (f,) = [x for x in _run("v1.31.9", {"p": ["v1.28.15", "v1.29.14", "v1.31.9"]}) if x.rule is Rule.NODE_EOL]
    assert (f.observed, f.limit, f.nodes_affected) == ("1.28.15", "2026-09-30", 2)


def test_skew_counts_only_offending_nodes():
    fs = _run("v1.35.4", {"p": ["v1.31.9", "v1.31.8", "v1.32.7", "v1.35.4"]})
    skew = next(f for f in fs if f.rule is Rule.NODE_SKEW_EXCEEDED)
    assert (skew.observed, skew.limit, skew.nodes_affected) == ("1.31.8", "1.32", 2)
    mixed = next(f for f in fs if f.rule is Rule.NODE_POOL_MIXED)
    assert (mixed.observed, mixed.nodes_affected) == ("1.31, 1.32, 1.35", 3)


def test_skew_policy_is_configurable():
    strict = CFG.policy.model_copy(update={"skew_max_minor": 1})
    assert _rules(_run("v1.35.4", {"p": ["v1.33.5"]}, policy=strict)) == [("NODE_SKEW_EXCEEDED", "p")]


def test_unparseable_cp_still_checks_node_eol_and_mixed():
    fs = _run("garbage", {"p": ["v1.28.15", "v1.35.4"]})
    assert _rules(fs) == [("NODE_EOL", "p"), ("NODE_POOL_MIXED", "p"), ("UNPARSEABLE_VERSION", "control-plane")]


def test_unparseable_nodes_excluded_from_other_rules():
    fs = _run("v1.35.4", {"p": ["weird", "weird", "v1.35.4"]})
    (f,) = fs
    assert (f.rule, f.observed, f.nodes_affected) == (Rule.UNPARSEABLE_VERSION, "weird", 2)


def test_unlabelled_pool_and_zero_nodes():
    assert _run("v1.35.4", {}) == ()
    assert _rules(_run("v1.35.4", {"(unlabelled)": ["v1.36.0"]})) == [("NODE_NEWER_THAN_CP", "(unlabelled)")]


def test_unknown_minor_for_provider_skips_lifecycle():
    (f,) = _run("v1.28.15", provider="self-managed")  # SM matrix starts at 1.29
    assert (f.rule, f.limit) == (Rule.CP_UNKNOWN_VERSION, "1.29-1.35")


@pytest.mark.parametrize("env,expected", [("prod", Severity.HIGH), ("staging", Severity.MEDIUM), ("dev", Severity.LOW)])
def test_severity_by_environment(env, expected):
    (f,) = _run("v1.32.7", env=env)
    assert f.severity is expected


def test_unreachable_finding_and_counts():
    c1, c2 = _cluster(cid="a"), _cluster(cid="b", env="dev")
    det = detect(
        [c1, c2],
        [
            FetchResult(cluster_id="a", reachable=False, reason="timeout"),
            FetchResult(cluster_id="b", reachable=True, control_plane_version="v1.35.4"),
        ],
        MATRIX,
        CFG.policy,
        N0,
    )
    (f,) = det.findings
    assert (f.rule, f.subject, f.observed, f.severity) == (Rule.UNREACHABLE, "cluster", "timeout", Severity.HIGH)
    assert det.compliant == ("b",) and det.checked == 1 and det.total == 2


def test_missing_result_is_an_error_not_a_silent_drop():
    with pytest.raises(ValueError, match="no fetch result"):
        detect([_cluster()], [], MATRIX, CFG.policy, N0)


def test_finding_ids_are_stable_and_distinct():
    a = finding_id("c1", Rule.CP_EOL, "control-plane")
    assert a == finding_id("c1", Rule.CP_EOL, "control-plane")
    assert a != finding_id("c11", Rule.CP_EOL, "control-plane")
    assert a != finding_id("c1", Rule.NODE_EOL, "control-plane")


def test_findings_sorted_most_severe_first():
    fs = _run("v1.28.15", {"p": ["v1.28.15"]})
    assert [f.severity for f in fs] == sorted((f.severity for f in fs), reverse=True)
