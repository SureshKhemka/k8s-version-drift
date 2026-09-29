"""Prompt chain behaviour with a scripted LLM (CH-1..CH-9, LL-4), plus the provider clients over mock HTTP."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from drift.chain.gates import gate1
from drift.chain.runner import PromptChain
from drift.config.models import ProviderConfig, StepModelConfig
from drift.domain.clock import FixedClock
from drift.domain.enums import ExitCode
from drift.llm.client import LLMClient, LLMError, LLMResponse, LLMUnavailable, Message, strip_thinking
from drift.message.compose import Fmt
from drift.pipeline import SummaryRequest
from drift.store.recorder import Recorder, Redactor
from drift.store.sqlite import Store
from drift_mocks.faults import FaultSpec
from evals.support import REPO, eval_config, fact_sheet, pipeline_env, run_pipeline_night

GOOD_DRAFT = next(
    g["text"]
    for g in yaml.safe_load((REPO / "evals/gate_sets/gate2_drafts.yaml").read_text())["good"]
    if g["id"] == "G01"
)


def good_themes(sheet) -> str:
    req = sorted(gate1.must_cover(sheet), key=lambda a: int(a[1:]))
    return json.dumps(
        {
            "themes": [
                {"title": "Most urgent", "priority": 1, "finding_ids": req[::2]},
                {"title": "Also important", "priority": 2, "finding_ids": req[1::2]},
            ]
        }
    )


class Scripted:
    """Stands in for LLMClient: returns queued outputs per step and records what it was sent."""

    def __init__(self, outputs: dict[str, list[str | Exception]]):
        self.outputs = {k: list(v) for k, v in outputs.items()}
        self.calls: list[tuple[str, list[Message]]] = []

    def factory(self, step):
        scripted = self

        class _C:
            def __init__(self):
                self.step = StepModelConfig(
                    provider="lmstudio", model=f"scripted-{step}", temperature=0, max_tokens=100, timeout_s=1, retries=0
                )

            async def complete(self, messages, schema=None):
                scripted.calls.append((step, list(messages)))
                out = scripted.outputs[step].pop(0)
                if isinstance(out, Exception):
                    raise out
                return LLMResponse(strip_thinking(out), out, self.step.model, 10, 5, 0, 1, 0.0)

        return _C()


@pytest.fixture
async def req_and_rec(tmp_path):
    sheet = await fact_sheet(1)
    store = Store(tmp_path / "t.db")
    store.create_run("r1", sheet.facts.run_date, "t")
    rec = Recorder(store, "r1", tmp_path / "runs", FixedClock.on(sheet.facts.run_date), Redactor())
    req = SummaryRequest(sheet.facts, None, None, Fmt("slack_mrkdwn"), 2000, sheet)  # type: ignore[arg-type]
    yield req, rec, store
    store.close()


def _chain(scripted: Scripted) -> PromptChain:
    return PromptChain(eval_config(), {}, client_factory=scripted.factory)


async def test_happy_path(req_and_rec):
    req, rec, store = req_and_rec
    s = Scripted({"group": [good_themes(req.sheet)], "draft": [GOOD_DRAFT]})
    chain = _chain(s)
    result = await chain.summarize(req, rec)
    assert result is not None and result.body == GOOD_DRAFT.strip()
    assert [(t.step, t.attempts, t.first_attempt_passed) for t in chain.traces] == [
        ("group", 1, True),
        ("draft", 1, True),
    ]
    calls = [e for e in store.events("r1") if e["kind"] == "llm_call"]
    assert [c["payload"]["prompt_version"] for c in calls] == ["group-v1", "draft-v1"]  # CH-8
    # the draft prompt never contains internal finding IDs
    draft_prompt = s.calls[1][1][1].content
    assert '"id": "f1"' not in draft_prompt and "payments-prod-11" in draft_prompt


async def test_retry_feeds_gate_errors_back(req_and_rec):
    req, rec, _ = req_and_rec
    incomplete = json.dumps({"themes": [{"title": "x", "priority": 1, "finding_ids": ["f1"]}]})
    s = Scripted({"group": ["not json", incomplete, good_themes(req.sheet)], "draft": [GOOD_DRAFT]})
    chain = _chain(s)
    assert await chain.summarize(req, rec) is not None
    group_calls = [m for step, m in s.calls if step == "group"]
    assert len(group_calls) == 3
    assert "PARSE" in group_calls[1][-1].content and group_calls[1][-2].content == "not json"
    assert "MISSING_COVERAGE" in group_calls[2][-1].content
    assert chain.traces[0].attempts == 3 and not chain.traces[0].first_attempt_passed


async def test_length_only_failure_goes_to_shorten(req_and_rec):
    req, rec, _ = req_and_rec
    long = GOOD_DRAFT + "\n" + ("Please upgrade soon. " * 120)
    s = Scripted({"group": [good_themes(req.sheet)], "draft": [long], "shorten": [GOOD_DRAFT]})
    chain = _chain(s)
    result = await chain.summarize(req, rec)
    assert result is not None and result.body == GOOD_DRAFT.strip()
    assert [t.step for t in chain.traces] == ["group", "draft", "shorten"]
    assert chain.traces[1].attempts == 1  # no draft retry: went straight to shorten (CH-5)
    shorten_prompt = s.calls[-1][1][1].content
    assert "Limit: 2000 characters" in shorten_prompt


async def test_unfaithful_draft_exhausts_retries_and_returns_none(req_and_rec):
    req, rec, store = req_and_rec
    bad = GOOD_DRAFT.replace("(1.28.15, end", "(1.28.14, end")
    s = Scripted({"group": [good_themes(req.sheet)], "draft": [bad, bad, bad]})
    chain = _chain(s)
    assert await chain.summarize(req, rec) is None
    gates = [e["payload"] for e in store.events("r1") if e["kind"] == "gate_result" and e["payload"]["step"] == "draft"]
    assert len(gates) == 3 and all("VERSION_NOT_IN_FACTS" in " ".join(g["errors"]) for g in gates)
    assert any(e["kind"] == "chain_failed" for e in store.events("r1"))


async def test_provider_unreachable_skips_chain(req_and_rec):
    req, rec, store = req_and_rec
    s = Scripted({"group": [LLMUnavailable("ConnectError")]})
    assert await _chain(s).summarize(req, rec) is None
    kinds = [e["kind"] for e in store.events("r1")]
    assert "llm_unavailable" in kinds and "llm_call" in kinds  # the failed call is still recorded (FR-30)


async def test_thinking_is_stripped_before_gates(req_and_rec):
    req, rec, _ = req_and_rec
    s = Scripted(
        {
            "group": ["<think>let me group f1 and f999</think>\n" + good_themes(req.sheet)],
            "draft": ["<think>I will mention payments-prod-99 and 1.99.9</think>" + GOOD_DRAFT],
        }
    )
    result = await _chain(s).summarize(req, rec)
    assert result is not None and "<think>" not in result.body and "payments-prod-99" not in result.body


async def test_pipeline_posts_llm_message_that_passes_final_gate(tmp_path):
    env = pipeline_env(tmp_path)
    env.k8s.overrides = {"c021": FaultSpec()}
    env = env.with_cfg(llm=env.cfg.llm.model_copy(update={"enabled": True}))
    await run_pipeline_night(env, 0)
    sheet = await fact_sheet(1)
    s = Scripted({"group": [good_themes(sheet)], "draft": [GOOD_DRAFT]})
    out = await run_pipeline_night(env, 1, summarizer=PromptChain(env.cfg, {}, client_factory=s.factory))
    assert out.summary_source == "llm" and out.post.status == "POSTED"
    assert GOOD_DRAFT.strip() in out.message
    assert "summary: LLM-written" in out.message and "Critical and unreachable" in out.message
    assert out.exit_code is ExitCode.PARTIAL


# ----------------------------------------------------------------------------- LLM client


def test_strip_thinking_variants():
    assert strip_thinking("<think>a</think>\nanswer") == "answer"
    assert strip_thinking("reasoning...</think>answer") == "answer"  # opening tag eaten by the template
    assert strip_thinking("answer<thinking>truncated") == "answer"
    assert strip_thinking("<THINK>x</THINK><reasoning>y</reasoning>ok") == "ok"
    assert strip_thinking("plain") == "plain"


def _client(handler, ptype="openai_compat", json_mode="off", extra=None, retries=1, key_env=None, env=None):
    prov = ProviderConfig(
        type=ptype, base_url="http://llm.test/v1", api_key_env=key_env, json_mode=json_mode, extra_body=extra or {}
    )
    step = StepModelConfig(
        provider="p",
        model="m",
        temperature=0.1,
        max_tokens=50,
        timeout_s=5,
        retries=retries,
        price_in_per_mtok=1.0,
        price_out_per_mtok=2.0,
    )
    return LLMClient({"p": prov}, step, env or {}, lambda: httpx.MockTransport(handler))


async def test_openai_compat_request_and_response():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"], seen["auth"] = (
            str(req.url),
            json.loads(req.content),
            req.headers.get("authorization"),
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '<think>x</think>{"a":1}', "reasoning_content": "secret plan"}}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                    "completion_tokens_details": {"reasoning_tokens": 7},
                },
            },
        )

    c = _client(handler, json_mode="json_object", extra={"reasoning_effort": "none"}, key_env="K", env={"K": "sk-1"})
    r = await c.complete([Message("system", "s"), Message("user", "u")], schema={"type": "object"})
    assert seen["url"] == "http://llm.test/v1/chat/completions" and seen["auth"] == "Bearer sk-1"
    assert seen["body"]["response_format"] == {"type": "json_object"} and seen["body"]["reasoning_effort"] == "none"
    assert r.text == '{"a":1}' and "secret plan" not in r.text + r.raw_text
    assert (r.prompt_tokens, r.completion_tokens, r.reasoning_tokens) == (1000, 500, 7)
    assert r.cost_usd == pytest.approx((1000 * 1.0 + 500 * 2.0) / 1e6)


async def test_json_mode_off_sends_no_response_format():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {}})

    await _client(handler).complete([Message("user", "u")], schema={"type": "object"})
    assert "response_format" not in seen["body"]


async def test_retry_then_success_and_error_classes():
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    assert (await _client(flaky).complete([Message("user", "u")])).text == "ok"

    def refused(req):
        raise httpx.ConnectError("refused http://10.0.0.5")

    with pytest.raises(LLMUnavailable) as exc:
        await _client(refused).complete([Message("user", "u")])
    assert "10.0.0.5" not in str(exc.value)
    with pytest.raises(LLMError):
        await _client(lambda r: httpx.Response(400)).complete([Message("user", "u")])
    with pytest.raises(LLMError) as exc:
        await _client(lambda r: httpx.Response(500), retries=0).complete([Message("user", "u")])
    assert not isinstance(exc.value, LLMUnavailable)


async def test_anthropic_request_shape():
    seen = {}

    def handler(req):
        seen["url"], seen["body"], seen["key"] = str(req.url), json.loads(req.content), req.headers.get("x-api-key")
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 3, "output_tokens": 4}}
        )

    c = _client(handler, ptype="anthropic", key_env="AK", env={"AK": "ak-1"})
    r = await c.complete([Message("system", "sys"), Message("user", "u")])
    assert seen["url"] == "http://llm.test/v1/v1/messages" and seen["key"] == "ak-1"
    assert seen["body"]["system"] == "sys" and seen["body"]["messages"] == [{"role": "user", "content": "u"}]
    assert r.text == "hi" and r.prompt_tokens == 3


def test_prompt_files_load_with_versions():
    from drift.chain.prompts import load_prompt

    cfg = eval_config()
    for step in ("group", "draft", "shorten"):
        p = load_prompt(Path(cfg.prompts.dir) / getattr(cfg.prompts, step))
        assert p.id == step and p.version == f"{step}-v1"
