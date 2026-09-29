"""The fixed prompt chain (CH-1..CH-9).

    Step 1 group  -> Gate 1 -(retry with errors)-> ...
    Step 2 draft  -> Gate 2 -(retry with errors)-> ...
                     └ failed on length only -> Step 3 shorten -> Gate 2 (full) -(retry)-> ...
    Any step out of retries, or the provider unreachable -> None (the pipeline posts the fallback)

A step never proceeds on output that failed its gate (CH-6).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from drift.chain.facts import RULE_TEXT, FactSheet, Theme
from drift.chain.gates import gate1, gate2
from drift.chain.gates.gate2 import GateResult
from drift.chain.prompts import Prompt, load_prompt
from drift.config import AppConfig
from drift.config.models import StepName
from drift.domain.enums import Severity
from drift.llm.client import LLMClient, LLMError, LLMResponse, LLMUnavailable, Message, TransportFactory
from drift.pipeline import SummaryRequest, SummaryResult
from drift.store.recorder import Recorder

log = logging.getLogger("drift.chain")


class Completer(Protocol):
    step: Any

    async def complete(self, messages: list[Message], schema: dict[str, Any] | None = None) -> LLMResponse: ...


ClientFactory = Callable[[StepName], Completer]


@dataclass
class StepTrace:
    """Per-step outcome for EV-11 (also recorded as events)."""

    step: str
    attempts: int = 0
    passed: bool = False
    first_attempt_passed: bool = False
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: list[list[str]] = field(default_factory=list)


class _StepFailed(Exception):
    pass


def _legend(sheet: FactSheet) -> dict[str, str]:
    """Plain-English meaning of each rule that occurs tonight (sent once instead of per finding)."""
    rules = sorted({ff.finding.rule for ff in sheet.findings} | {f.rule for f in sheet.resolved}, key=lambda r: r.value)
    return {r.value: RULE_TEXT[r] for r in rules}


class PromptChain:
    def __init__(
        self,
        cfg: AppConfig,
        environ: Mapping[str, str],
        transport_factory: TransportFactory | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.cfg = cfg
        d = cfg.prompts.dir
        self.prompts: dict[str, Prompt] = {
            "group": load_prompt(d / cfg.prompts.group),
            "draft": load_prompt(d / cfg.prompts.draft),
            "shorten": load_prompt(d / cfg.prompts.shorten),
        }
        self._client_factory: ClientFactory = client_factory or (
            lambda step: LLMClient(cfg.llm.providers, cfg.llm.for_step(step), environ, transport_factory)
        )
        self.traces: list[StepTrace] = []

    # ---------------------------------------------------------------------- entry

    async def summarize(self, req: SummaryRequest, rec: Recorder) -> SummaryResult | None:
        self.traces = []
        sheet = req.sheet
        try:
            themes = await self._group(sheet, rec)
            sheet = sheet.with_themes(themes)
            text = await self._draft(sheet, req, rec)
        except LLMUnavailable as exc:
            rec.event("summarise", "llm_unavailable", {"error_type": str(exc)})  # CH-9
            log.warning("LLM provider unreachable (%s); using fallback", exc)
            return None
        except (_StepFailed, LLMError) as exc:
            rec.event("summarise", "chain_failed", {"reason": str(exc)})
            log.warning("prompt chain failed (%s); using fallback", exc)
            return None
        finally:
            rec.event("summarise", "chain_trace", {"steps": [t.__dict__ for t in self.traces]})
        return SummaryResult(body=text, model=self.cfg.llm.for_step("draft").model)

    # ---------------------------------------------------------------------- steps

    async def _group(self, sheet: FactSheet, rec: Recorder) -> list[Theme]:
        must = gate1.must_cover(sheet)
        ctx = {
            "legend": _legend(sheet),
            "max_themes": self.cfg.chain.max_themes,
            "stats": sheet.prompt_stats(),
            "findings": [ff.as_prompt_dict() for ff in sheet.findings],
            "must_include": sorted(must, key=lambda a: int(a[1:])),
        }
        themes: list[Theme] = []

        def gate(text: str) -> GateResult:
            nonlocal themes
            result, themes = gate1.parse_and_check(text, sheet, self.cfg.chain.max_themes)
            return result

        await self._run_step("group", self.prompts["group"].messages(**ctx), gate, rec, gate1.GROUP_SCHEMA)
        return themes

    async def _draft(self, sheet: FactSheet, req: SummaryRequest, rec: Recorder) -> str:
        budget = req.body_budget
        by_alias = sheet.by_alias()
        themes_ctx = []
        for t in sheet.themes:
            fs = [by_alias[a] for a in t.aliases]
            nodes = [ff.finding.nodes_affected for ff in fs if ff.finding.nodes_affected is not None]
            counts: dict[str, Any] = {
                "clusters": len({ff.finding.cluster_id for ff in fs}),
                "findings": len(fs),
                **({"nodes": sum(nodes)} if nodes else {}),
                "by_severity": {
                    s.name: n
                    for s in sorted(Severity, reverse=True)
                    if (n := sum(1 for ff in fs if ff.finding.severity is s))
                },
            }
            themes_ctx.append(
                {
                    "priority": t.priority,
                    "title": t.title,
                    "counts": counts,
                    "findings": [{k: v for k, v in ff.as_prompt_dict().items() if k != "id"} for ff in fs],
                }
            )
        resolved = [
            {"cluster": f.cluster_name, "rule": f.rule.value, "subject": f.subject, "observed": f.observed}
            for f in sheet.resolved
        ]
        ctx = {
            "legend": _legend(sheet),
            "fmt": req.fmt.kind,
            "budget": budget,
            "stats": sheet.prompt_stats(),
            "themes": themes_ctx,
            "resolved": resolved,
            "must_name": sorted(sheet.required_clusters()),
        }
        deny = self.cfg.deny_list
        last: dict[str, Any] = {}

        def gate(text: str) -> GateResult:
            result = gate2.check(text, sheet, max_length=budget, deny_list=deny)
            last["text"], last["result"] = text, result
            return result

        try:
            return await self._run_step(
                "draft", self.prompts["draft"].messages(**ctx), gate, rec, stop_early=lambda r: r.only_length
            )
        except _StepFailed:
            result: GateResult | None = last.get("result")
            if result is None or not result.only_length:
                raise
        # CH-5: the draft passed everything except length -> shorten, then re-run Gate 2 in full.
        draft = last["text"]
        sctx = {"limit": budget, "length": len(draft), "draft": draft, "must_name": ctx["must_name"]}
        return await self._run_step(
            "shorten",
            self.prompts["shorten"].messages(**sctx),
            lambda text: gate2.check(text, sheet, max_length=budget, deny_list=deny),
            rec,
        )

    async def _run_step(
        self,
        step: StepName,
        messages: list[Message],
        gate: Callable[[str], GateResult],
        rec: Recorder,
        schema: dict[str, Any] | None = None,
        stop_early: Callable[[GateResult], bool] | None = None,
    ) -> str:
        prompt = self.prompts[step]
        client = self._client_factory(step)
        trace = StepTrace(step)
        self.traces.append(trace)
        convo = list(messages)
        for attempt in range(1, self.cfg.chain.gate_retries + 2):
            step_id = f"summarise.{step}.{attempt}"
            t0 = time.perf_counter()
            try:
                resp = await client.complete(convo, schema)
            except LLMError as exc:  # FR-30: failed calls are recorded too
                trace.attempts = attempt
                trace.latency_ms += int((time.perf_counter() - t0) * 1000)
                rec.event(
                    step_id,
                    "llm_call",
                    {
                        "step": step,
                        "attempt": attempt,
                        "provider": client.step.provider,
                        "model": client.step.model,
                        "prompt_id": prompt.id,
                        "prompt_version": prompt.version,
                        "input": [m.__dict__ for m in convo],
                        "error": f"{type(exc).__name__}: {exc}",
                        "latency_ms": int((time.perf_counter() - t0) * 1000),
                    },
                )
                raise
            trace.attempts = attempt
            trace.latency_ms += resp.latency_ms
            trace.prompt_tokens += resp.prompt_tokens
            trace.completion_tokens += resp.completion_tokens
            rec.event(
                step_id,
                "llm_call",
                {
                    "step": step,
                    "attempt": attempt,
                    "provider": client.step.provider,
                    "model": resp.model,
                    "prompt_id": prompt.id,
                    "prompt_version": prompt.version,
                    "temperature": client.step.temperature,
                    "max_tokens": client.step.max_tokens,
                    "input": [m.__dict__ for m in convo],
                    "output": resp.text,
                    "raw_output": resp.raw_text,
                    "prompt_tokens": resp.prompt_tokens,
                    "completion_tokens": resp.completion_tokens,
                    "reasoning_tokens": resp.reasoning_tokens,
                    "latency_ms": resp.latency_ms,
                    "cost_usd": resp.cost_usd,
                },
            )
            result = gate(resp.text)
            rec.event(
                step_id,
                "gate_result",
                {
                    "step": step,
                    "attempt": attempt,
                    "gate": "gate1" if step == "group" else "gate2",
                    "passed": result.passed,
                    "errors": [str(e) for e in result.errors],
                    "stats": result.stats,
                },
            )
            trace.errors.append([str(e) for e in result.errors])
            if result.passed:
                trace.passed = True
                trace.first_attempt_passed = attempt == 1
                return resp.text
            if stop_early and stop_early(result):
                break
            convo += [Message("assistant", resp.text), prompt.retry_message(errors=result.feedback())]
        raise _StepFailed(f"{step} failed its gate after {trace.attempts} attempt(s)")
