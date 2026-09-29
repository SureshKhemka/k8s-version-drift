"""EV-1 (hard gate): every seeded night produces exactly the expected findings.

Runs the real collector against the mock cluster API, then the rule engine. Expectations are
hand-written in scripts/build_seed.py and never derived from the engine.
"""

import pytest

from evals.support import NIGHTS, expected_findings, finding_row, run_night


def _key(row):
    return (row["cluster"], row["rule"], row["subject"])


@pytest.mark.parametrize("night", NIGHTS)
async def test_ev01_night(tmp_path, night):
    run = await run_night(tmp_path, night)
    actual = {_key(r): r for r in map(finding_row, run.detection.findings)}
    expected = {_key(r): r for r in expected_findings(night)}

    problems = []
    for k in sorted(expected.keys() - actual.keys()):
        problems.append(f"MISSING  {k}: expected {expected[k]}")
    for k in sorted(actual.keys() - expected.keys()):
        problems.append(f"EXTRA    {k}: got {actual[k]}")
    for k in sorted(expected.keys() & actual.keys()):
        if expected[k] != actual[k]:
            diff = {f: (expected[k][f], actual[k][f]) for f in expected[k] if expected[k][f] != actual[k][f]}
            problems.append(f"MISMATCH {k}: (expected, got) {diff}")
    assert not problems, f"night {night}: {len(problems)} problem(s)\n" + "\n".join(problems)

    # FR-5 / FR-14: nobody dropped, compliant set and checked count exact
    inv_ids = {c.id for c in run.inventory.clusters}
    with_findings = {r["cluster"] for r in expected_findings(night)}
    assert run.detection.total == len(inv_ids)
    assert set(run.detection.compliant) == inv_ids - with_findings
    unreachable = {f.cluster_id for f in run.detection.findings if f.rule.value == "UNREACHABLE"}
    assert run.detection.checked == len(inv_ids) - len(unreachable)
