"""Eval tooling: cassette record/replay (EV-15), judge parsing (EV-12), metrics (EV-11/14), agreement (EV-13),
report comparison and label export."""

from __future__ import annotations

import json

import pytest
import yaml

from drift.config.models import StepModelConfig
from drift.llm.cassette import CassetteClient, CassetteMiss, request_key
from drift.llm.client import LLMResponse, Message
from evals import harness

STEP = StepModelConfig(provider="p", model="m", temperature=0.2, max_tokens=10, timeout_s=1, retries=0)


class Fake:
    step = STEP

    def __init__(self):
        self.calls = 0

    async def complete(self, messages, schema=None):
        self.calls += 1
        return LLMResponse(f"answer {self.calls}", f"answer {self.calls}", "m", 1, 2, 0, 42, 0.0)


async def test_cassette_record_then_replay(tmp_path):
    msgs = [Message("user", "hello")]
    rec = CassetteClient(Fake(), tmp_path, "record")
    assert (await rec.complete(msgs)).text == "answer 1"
    replay_inner = Fake()
    rep = CassetteClient(replay_inner, tmp_path, "replay")
    r = await rep.complete(msgs)
    assert r.text == "answer 1" and r.latency_ms == 42 and replay_inner.calls == 0 and rep.hits == 1
    with pytest.raises(CassetteMiss):
        await rep.complete([Message("user", "a changed prompt")])
    assert rep.misses == 1


async def test_cassette_auto_records_only_misses(tmp_path):
    inner = Fake()
    auto = CassetteClient(inner, tmp_path, "auto")
    await auto.complete([Message("user", "x")])
    await auto.complete([Message("user", "x")])
    assert inner.calls == 1 and auto.hits == 1


def test_request_key_covers_everything_that_changes_the_answer():
    base = request_key(STEP, [Message("user", "a")], None)
    assert base != request_key(STEP.model_copy(update={"model": "m2"}), [Message("user", "a")], None)
    assert base != request_key(STEP.model_copy(update={"temperature": 0.3}), [Message("user", "a")], None)
    assert base != request_key(STEP, [Message("user", "b")], None)
    assert base != request_key(STEP, [Message("user", "a")], {"type": "object"})
    assert base == request_key(STEP.model_copy(update={"timeout_s": 99}), [Message("user", "a")], None)


GOOD = {"scores": dict.fromkeys(harness.CRITERIA, 4), "overall": 4, "rationale": "fine"}


@pytest.mark.parametrize(
    "text,ok",
    [
        (json.dumps(GOOD), True),
        ("```json\n" + json.dumps(GOOD) + "\n```", True),
        (json.dumps(GOOD | {"overall": 6}), False),
        (json.dumps(GOOD | {"overall": "4"}), False),
        (json.dumps({"scores": {"tone_neutral": 3}, "overall": 3}), False),
        ("Score: 4/5", False),
    ],
)
def test_judge_parsing_is_strict(text, ok):
    parsed, err = harness._parse_judge(text)
    assert (parsed is not None) is ok
    assert bool(err) is not ok


def _row(source, length, steps=(), judge=None, faithful=True, reason=None):
    r = {
        "source": source,
        "length": length,
        "limit": 3000,
        "faithful": faithful,
        "fallback_reason": reason,
        "steps": list(steps),
    }
    if judge is not None:
        r["judge"] = {"scores": dict.fromkeys(harness.CRITERIA, judge), "overall": judge}
    return r


def _step(name, attempts, passed=True):
    return {
        "step": name,
        "attempts": attempts,
        "passed": passed,
        "first_attempt_passed": passed and attempts == 1,
        "latency_ms": 1000 * attempts,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "errors": [],
    }


def test_model_metrics():
    rows = [
        _row("llm", 2000, [_step("group", 2), _step("draft", 1)], judge=4),
        _row("llm", 2600, [_step("group", 1), _step("draft", 1)], judge=2),
        _row("fallback", 1200, [_step("group", 3, passed=False)], reason="chain_failed"),
        _row("all_clear", 300),
    ]
    m = harness.model_metrics(rows)
    assert m["llm_pct"] == pytest.approx(66.7) and m["fallback_pct"] == pytest.approx(33.3)
    assert m["steps"]["group"] == {
        "invocations": 3,
        "first_attempt_pass_pct": pytest.approx(33.3),
        "pass_after_retries_pct": pytest.approx(66.7),
        "mean_attempts": 2.0,
        "latency_ms_mean": 2000,
        "latency_ms_max": 3000,
        "prompt_tokens_total": 30,
        "completion_tokens_total": 15,
    }
    assert m["length"]["median"] == 1600 and m["length"]["over_limit"] == 0
    assert m["judge"]["overall"] == 3 and m["judge"]["n"] == 2
    assert m["fallback_reasons"] == ["chain_failed"]


def test_spearman():
    assert harness._spearman([1, 2, 3, 4], [2, 3, 4, 5]) == 1.0
    assert harness._spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert harness._spearman([3, 3, 3], [1, 2, 3]) is None


def _report(rid, faithful_failures=0, judge=4.0, misses=0):
    metrics = {
        "faithfulness_failures": faithful_failures,
        "llm_pct": 100.0,
        "fallback_pct": 0.0,
        "steps": {"draft": {"first_attempt_pass_pct": 80.0, "mean_attempts": 1.2}},
        "length": {"median": 2000, "over_limit": 0},
        "judge": {"overall": judge},
    }
    return {
        "id": rid,
        "git": "abc",
        "prompts": {"draft": "draft-v1"},
        "mode": "replay",
        "verdict": {"status": "PASS", "reasons": []},
        "models": {"qwen": {"metrics": metrics, "cassette": {"hits": 3, "misses": misses}}},
    }


def test_compare_and_verdict(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(_report("A", judge=3.5)))
    b.write_text(json.dumps(_report("B", judge=4.0)))
    out = harness.compare(a, b)
    assert "| qwen | judge.overall | 3.5 | 4 | 0.5 |" in out
    assert harness.verdict(_report("x"))["status"] == "PASS"
    assert harness.verdict(_report("x", faithful_failures=1))["status"] == "FAIL"
    assert harness.verdict(_report("x", misses=2))["status"] == "FAIL"
    hard_fail = _report("x") | {"hard_gates": {"passed": False, "failures": ["test_ev01_night[2]"]}}
    assert "test_ev01_night[2]" in harness.verdict(hard_fail)["reasons"][0]


def test_export_labels_keeps_existing_scores(tmp_path):
    report = {
        "id": "R1",
        "models": {
            "qwen": {
                "nights": [
                    {
                        "night": 0,
                        "source": "llm",
                        "message": "msg A",
                        "fallback_message": "fb A",
                        "judge_context": "ctx",
                    },
                    {
                        "night": 1,
                        "source": "fallback",
                        "message": "fb B",
                        "fallback_message": "fb B",
                        "judge_context": "ctx",
                    },
                ]
            }
        },
    }
    rp = tmp_path / "report.json"
    rp.write_text(json.dumps(report))
    labels = tmp_path / "labels.yaml"
    labels.write_text(yaml.safe_dump({"items": [{"id": "H01", "message": "msg A", "human_score": 4}]}))
    assert harness.export_labels(rp, labels) == 3
    items = yaml.safe_load(labels.read_text())["items"]
    assert items[0]["human_score"] == 4 and [i["message"] for i in items] == ["msg A", "fb A", "fb B"]
    assert all(i["human_score"] is None for i in items[1:])
