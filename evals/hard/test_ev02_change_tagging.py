"""EV-2 (hard gate): every multi-night sequence produces exactly the expected change tags.

Nights 0..4 run in order through the real collector, rule engine, change tagging and SQLite store.
Expected tags are hand-written in scripts/build_seed.py.
"""

from evals.support import expected_tags, run_sequence


async def test_ev02_tags_across_all_nights(tmp_path):
    failures = []
    async for run, changes, _store in run_sequence(tmp_path):
        exp = expected_tags(run.night)
        if changes.first_run != exp["first_run"]:
            failures.append(f"night {run.night}: first_run={changes.first_run}, expected {exp['first_run']}")
        actual = {(t.finding.cluster_id, t.finding.rule.value, t.finding.subject): t.tag.value for t in changes.tagged}
        for f in changes.resolved:
            key = (f.cluster_id, f.rule.value, f.subject)
            assert key not in actual, f"night {run.night}: {key} both current and resolved"
            actual[key] = "RESOLVED"
        expected = {(r["cluster"], r["rule"], r["subject"]): r["tag"] for r in exp["tags"]}
        for k in sorted(expected.keys() | actual.keys()):
            if expected.get(k) != actual.get(k):
                failures.append(f"night {run.night}: {k} expected {expected.get(k)}, got {actual.get(k)}")
    assert not failures, "\n".join(failures)


async def test_ev02_stale_findings_carried_during_outage(tmp_path):
    """D19: search-prod-3 (c014) is unreachable on nights 2-3; its patch finding is carried, not resolved."""
    stale_by_night = {}
    async for run, changes, _ in run_sequence(tmp_path):
        stale_by_night[run.night] = {(f.cluster_id, f.rule.value) for f in changes.stale}
    assert stale_by_night == {
        0: set(),
        1: set(),
        2: {("c014", "CP_PATCH_BEHIND")},
        3: {("c014", "CP_PATCH_BEHIND")},
        4: set(),
    }


async def test_ev02_new_cluster_and_worsened_reasons(tmp_path):
    async for run, changes, _ in run_sequence(tmp_path, nights=(0, 1, 2)):
        if run.night == 1:
            reasons = {t.finding.cluster_id: t.reason for t in changes.tagged if t.tag.value == "WORSENED"}
            assert reasons == {
                "c005": "escalated from CP_UNSUPPORTED",
                "c035": "escalated from CP_EOL_SOON",
                "c063": "days remaining crossed 30",
                "c064": "days remaining crossed 30",
            }
        if run.night == 2:
            assert changes.new_clusters == ("c081",)
