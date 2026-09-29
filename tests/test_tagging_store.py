"""Change tagging edge cases, the SQLite store and the recorder's redaction."""

import json
import sqlite3
from datetime import date

import pytest

from drift.diff.tagging import Baseline, tag_changes
from drift.domain.clock import FixedClock
from drift.domain.enums import ChangeTag, Rule, Severity
from drift.rules.base import Finding, finding_id
from drift.store.recorder import REDACTED, Recorder, Redactor, new_run_id
from drift.store.sqlite import Store

D = date(2026, 10, 1)
TH = (30, 14, 7)


def F(cid, rule, subject="control-plane", sev=Severity.HIGH, days=None, observed="1.31.9"):
    return Finding(
        id=finding_id(cid, rule, subject),
        cluster_id=cid,
        cluster_name=f"n-{cid}",
        environment="prod",
        provider="eks",
        rule=rule,
        severity=sev,
        subject=subject,
        observed=observed,
        days_remaining=days,
    )


def tags(cs):
    out = {(t.finding.cluster_id, t.finding.rule, t.finding.subject): t.tag for t in cs.tagged}
    out |= {(f.cluster_id, f.rule, f.subject): ChangeTag.RESOLVED for f in cs.resolved}
    return out


def base(findings, clusters=("a", "b")):
    return Baseline(run_id="prev", clusters=frozenset(clusters), findings=tuple(findings))


# ------------------------------------------------------------------------------ tagging


def test_first_run_all_new():
    cs = tag_changes([F("a", Rule.CP_EOL)], ["a"], None, TH)
    assert cs.first_run and tags(cs) == {("a", Rule.CP_EOL, "control-plane"): ChangeTag.NEW}


def test_severity_increase_is_worsened():
    cs = tag_changes(
        [F("a", Rule.NODE_POOL_MIXED, "p", Severity.HIGH)],
        ["a", "b"],
        base([F("a", Rule.NODE_POOL_MIXED, "p", Severity.MEDIUM)]),
        TH,
    )
    (t,) = cs.tagged
    assert t.tag is ChangeTag.WORSENED and t.reason == "severity MEDIUM -> HIGH"


@pytest.mark.parametrize(
    "prev,now,tag",
    [
        (31, 30, ChangeTag.WORSENED),
        (30, 29, ChangeTag.ONGOING),
        (15, 14, ChangeTag.WORSENED),
        (20, 5, ChangeTag.WORSENED),
        (7, 7, ChangeTag.ONGOING),
        (5, 8, ChangeTag.ONGOING),
    ],
)
def test_days_threshold_crossing(prev, now, tag):
    cs = tag_changes([F("a", Rule.CP_EOL_SOON, days=now)], ["a", "b"], base([F("a", Rule.CP_EOL_SOON, days=prev)]), TH)
    assert cs.tagged[0].tag is tag


def test_escalation_not_resolved_and_deescalation_is():
    cs = tag_changes([F("a", Rule.CP_EOL)], ["a", "b"], base([F("a", Rule.CP_UNSUPPORTED)]), TH)
    assert tags(cs) == {("a", Rule.CP_EOL, "control-plane"): ChangeTag.WORSENED}
    # upgrade from an EOL minor to one that is merely EOL_SOON: old resolved, new is NEW
    cs = tag_changes([F("a", Rule.CP_EOL_SOON)], ["a", "b"], base([F("a", Rule.CP_EOL)]), TH)
    assert tags(cs) == {
        ("a", Rule.CP_EOL_SOON, "control-plane"): ChangeTag.NEW,
        ("a", Rule.CP_EOL, "control-plane"): ChangeTag.RESOLVED,
    }


def test_unreachable_new_vs_ongoing_and_stale_carry():
    prev = base([F("a", Rule.CP_PATCH_BEHIND), F("b", Rule.UNREACHABLE, "cluster")])
    cs = tag_changes([F("a", Rule.UNREACHABLE, "cluster"), F("b", Rule.UNREACHABLE, "cluster")], ["a", "b"], prev, TH)
    assert tags(cs) == {
        ("a", Rule.UNREACHABLE, "cluster"): ChangeTag.NEW,
        ("b", Rule.UNREACHABLE, "cluster"): ChangeTag.ONGOING,
    }
    assert [f.rule for f in cs.stale] == [Rule.CP_PATCH_BEHIND] and cs.resolved == ()


def test_new_cluster_and_removed_cluster():
    cs = tag_changes([F("c", Rule.CP_EOL)], ["a", "c"], base([F("b", Rule.CP_EOL)]), TH)
    assert tags(cs) == {("c", Rule.CP_EOL, "control-plane"): ChangeTag.NEW_CLUSTER}
    assert cs.new_clusters == ("c",) and cs.removed_clusters == ("b",)
    assert cs.resolved == ()  # removed clusters are not "resolved"


def test_counts():
    cs = tag_changes([F("a", Rule.CP_EOL)], ["a", "b"], base([F("b", Rule.CP_EOL_SOON)]), TH)
    assert cs.counts[ChangeTag.NEW] == 1 and cs.counts[ChangeTag.RESOLVED] == 1


# -------------------------------------------------------------------------------- store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def _save(store, run_id, d, findings, clusters=("a", "b"), complete=True):
    store.create_run(run_id, d, f"{d}T02:00:00")
    if complete:
        cs = tag_changes(findings, clusters, store.load_baseline(d), TH)
        store.save_detection(run_id, d, "m1", dict.fromkeys(clusters, True), cs)
        return cs
    return None


def test_baseline_is_latest_completed_run_before_date(store):
    _save(store, "r1", date(2026, 10, 1), [F("a", Rule.CP_EOL)])
    _save(store, "r2", date(2026, 10, 2), [F("a", Rule.CP_EOL), F("b", Rule.CP_EOL)])
    _save(store, "r3", date(2026, 10, 3), [], complete=False)  # never finished detection
    assert store.load_baseline(date(2026, 10, 4)).run_id == "r2"
    # re-running 10-02 compares against 10-01, not against the earlier 10-02 run (D27)
    assert store.load_baseline(date(2026, 10, 2)).run_id == "r1"
    assert store.load_baseline(date(2026, 10, 1)) is None


def test_baseline_roundtrip_keeps_stale_and_drops_resolved(store):
    _save(store, "r1", date(2026, 10, 1), [F("a", Rule.CP_PATCH_BEHIND), F("b", Rule.CP_EOL)])
    _save(store, "r2", date(2026, 10, 2), [F("a", Rule.UNREACHABLE, "cluster")])
    b = store.load_baseline(date(2026, 10, 3))
    assert sorted((f.cluster_id, f.rule) for f in b.findings) == [("a", Rule.CP_PATCH_BEHIND), ("a", Rule.UNREACHABLE)]
    assert b.clusters == frozenset({"a", "b"})


def test_history_per_cluster(store):
    _save(store, "r1", date(2026, 10, 1), [F("a", Rule.CP_EOL)])
    _save(store, "r2", date(2026, 10, 2), [])
    h = store.history("a")
    assert [(r["run_date"], r["tag"]) for r in h] == [("2026-10-01", "NEW"), ("2026-10-02", "RESOLVED")]
    assert store.history("a", since=date(2026, 10, 2))[0]["tag"] == "RESOLVED"


def test_events_are_append_only(store):
    store.create_run("r1", D, "t")
    store.append_event("r1", "collect", "fetch", "t", "{}")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("UPDATE run_events SET payload='x'")


def test_prune_respects_retention(store):
    _save(store, "old", date(2026, 6, 1), [F("a", Rule.CP_EOL)])
    _save(store, "new", date(2026, 10, 1), [F("a", Rule.CP_EOL)])
    store.append_event("old", "s", "k", "t", "{}")
    assert store.prune(date(2026, 10, 1), retention_days=90) == 1
    assert store.run("old") is None and store.run("new") is not None
    assert store.events("old") == [] and [r["run_id"] for r in store.history("a")] == ["new"]


def test_posts_idempotency_record(store):
    assert store.post_for(D) is None
    store.record_post(D, "r1", "t1", forced=False)
    store.record_post(D, "r2", "t2", forced=True)
    assert store.post_for(D)["run_id"] == "r2"


# ----------------------------------------------------------------------------- recorder


def test_redactor_removes_secrets_endpoints_and_tokens():
    r = Redactor.from_env(["TOK", "MISSING", None], {"TOK": "hunter2-secret"}, [r"\b\d{12}\b"])
    s = r.text(
        "url http://127.0.0.1:8081/clusters/c1 tok hunter2-secret acct 123456789012 "
        "Bearer abc.def ip 10.0.0.1 version 1.31.4 jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIx"
    )
    for leaked in ("127.0.0.1", "hunter2", "123456789012", "abc.def", "10.0.0.1", "eyJhbGci"):
        assert leaked not in s
    assert "1.31.4" in s and s.count(REDACTED) == 6


def test_recorder_writes_sqlite_and_jsonl(store, tmp_path):
    clock = FixedClock.on(D)
    rec = Recorder(store, new_run_id(D), tmp_path / "runs", clock, Redactor(("s3cr3t",)))
    store.create_run(rec.run_id, D, "t")
    rec.event("collect", "fetch_result", {"cluster": "c1", "note": "token s3cr3t", "f": F("a", Rule.CP_EOL)})
    (ev,) = store.events(rec.run_id)
    assert ev["payload"]["note"] == f"token {REDACTED}" and ev["payload"]["f"]["rule"] == "CP_EOL"
    line = json.loads(rec.path.read_text())
    assert line["step_id"] == "collect" and line["payload"]["cluster"] == "c1"
    assert rec.run_id.startswith("2026-10-01-")
