"""Gate effectiveness (EV-9, EV-10): hand-written good and bad outputs against the real gates.

Reports false rejects (good output rejected) and false accepts (bad output accepted). Both must be zero.
"""

from __future__ import annotations

import json

import yaml

from drift.chain.gates import gate1, gate2
from drift.domain.enums import ChangeTag, Severity
from evals.support import REPO, eval_config, fact_sheet

SETS = REPO / "evals" / "gate_sets"


# ------------------------------------------------------------------------------------------ EV-9


def _gate2_cases() -> list[dict]:
    data = yaml.safe_load((SETS / "gate2_drafts.yaml").read_text())
    good = {g["id"]: g for g in data["good"]}
    cases = []
    for g in data["good"]:
        cases.append(
            {
                "id": g["id"],
                "night": g.get("night", 1),
                "text": g["text"],
                "expect": None,
                "category": "good",
                "budget": data["budget"],
            }
        )
    for b in data["bad"]:
        text = b.get("text")
        night = b.get("night", 1)
        if "base" in b:
            base = good[b["base"]]
            night = b.get("night", base.get("night", 1))
            text = base["text"]
            for old, new in b.get("replace", []):
                assert old in text, f"{b['id']}: '{old}' not found in {b['base']} (gate set is stale)"
                text = text.replace(old, new, 1)
            if "append_repeat" in b:
                chunk, n = b["append_repeat"]
                text = text.rstrip("\n") + chunk * n
        expect = b.get("expect_any") or [b["expect"]]
        cases.append(
            {
                "id": b["id"],
                "night": night,
                "text": text,
                "expect": expect,
                "category": b["category"],
                "budget": data["budget"],
            }
        )
    return cases


def test_ev09_gate_set_size():
    cases = _gate2_cases()
    assert sum(c["expect"] is None for c in cases) >= 20
    assert sum(c["expect"] is not None for c in cases) >= 40


async def test_ev09_gate2_rejects_every_bad_draft_and_passes_every_good_one():
    cfg = eval_config()
    false_rejects, false_accepts, wrong_reason = [], [], []
    for c in _gate2_cases():
        sheet = await fact_sheet(c["night"])
        res = gate2.check(c["text"], sheet, max_length=c["budget"], deny_list=cfg.deny_list)
        codes = {e.code for e in res.errors}
        if c["expect"] is None and not res.passed:
            false_rejects.append(f"{c['id']}: {[str(e) for e in res.errors]}")
        elif c["expect"] is not None and res.passed:
            false_accepts.append(f"{c['id']} ({c['category']})")
        elif c["expect"] is not None and not codes & set(c["expect"]):
            wrong_reason.append(f"{c['id']} ({c['category']}): expected {c['expect']}, got {sorted(codes)}")
    report = {"false_rejects": false_rejects, "false_accepts": false_accepts, "wrong_reason": wrong_reason}
    assert not any(report.values()), json.dumps(report, indent=2)


# ----------------------------------------------------------------------------------------- EV-10


def _step1_output(case: dict, sheet) -> str:
    if "text" in case:
        return case["text"]
    required = sorted(gate1.must_cover(sheet), key=lambda a: int(a[1:]))
    tpl = case["template"]
    if tpl == "one_theme_per_finding":
        themes = [{"title": f"t{i}", "priority": i, "finding_ids": [a]} for i, a in enumerate(required, 1)]
        return json.dumps({"themes": themes})
    if case.get("drop_required"):
        required = required[case["drop_required"] :]
    if case.get("drop_new_medium"):
        nm = next(
            ff.alias for ff in sheet.findings if ff.finding.severity is Severity.MEDIUM and ff.tag is ChangeTag.NEW
        )
        required = [a for a in required if a != nm]
    thirds = [required[i::3] for i in range(3)]
    themes = [{"title": f"Theme {i + 1}", "priority": i + 1, "finding_ids": ids} for i, ids in enumerate(thirds)]
    if tpl == "fenced_with_low":
        low = [ff.alias for ff in sheet.findings if ff.finding.severity is Severity.LOW]
        themes[-1]["finding_ids"] += low
        return "```json\n" + json.dumps({"themes": themes}, indent=2) + "\n```"
    if case.get("add_to_first"):
        themes[0]["finding_ids"] += case["add_to_first"]
    if case.get("duplicate_first_id"):
        themes[1]["finding_ids"].append(themes[0]["finding_ids"][0])
    if case.get("same_priority"):
        themes[1]["priority"] = 1
    if case.get("add_empty_theme"):
        themes.append({"title": "Empty", "priority": 4, "finding_ids": []})
    return json.dumps({"themes": themes})


async def test_ev10_gate1_rejects_bad_step1_outputs():
    data = yaml.safe_load((SETS / "gate1_outputs.yaml").read_text())
    sheet = await fact_sheet(1)
    max_themes = eval_config().chain.max_themes
    problems = []
    for case in data["good"]:
        res, themes = gate1.parse_and_check(_step1_output(case, sheet), sheet, max_themes)
        if not res.passed or not themes:
            problems.append(f"false reject {case['id']}: {[str(e) for e in res.errors]}")
    for case in data["bad"]:
        res, themes = gate1.parse_and_check(_step1_output(case, sheet), sheet, max_themes)
        codes = {e.code for e in res.errors}
        if res.passed:
            problems.append(f"false accept {case['id']}")
        elif case["expect"] not in codes:
            problems.append(f"{case['id']}: expected {case['expect']}, got {sorted(codes)}")
        assert themes == [], "a failed gate must not hand themes to the next step"
    assert len(data["bad"]) >= 10
    assert not problems, "\n".join(problems)
