"""`drift eval` — one command for every eval (EV-1..EV-17), producing a comparable report.

Sections of a report:
  hard_gates       EV-1..EV-10 via pytest (must all pass)
  models           per model profile, the five scenario nights through the real pipeline + prompt chain:
                     EV-3 faithfulness of every posted message (must be 0 failures)
                     EV-11 per-step gate pass rates, retries, fallback rate, latency, tokens
                     EV-12 judge scores (rubric in prompts/judge_v1.md.j2), with the fallback as a baseline
                     EV-14 message length distribution against the limit
                     EV-16 side by side when several profiles run
  human_agreement  EV-13 judge vs human scores on the labelled set
Runs are repeatable (EV-17): fixed seeds, fixed clocks, in-process mocks; replay mode (EV-15) reuses recorded
LLM outputs so code and gate changes can be evaluated without a model.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from drift.chain.gates import gate2
from drift.chain.prompts import load_prompt
from drift.chain.runner import PromptChain
from drift.config import AppConfig
from drift.domain.enums import Rule, Severity
from drift.llm.cassette import CassetteClient, CassetteMode
from drift.llm.client import LLMClient, LLMError, Message
from drift.message.compose import Fmt, RunFacts, appendix, assemble, fallback_body, footer, mentions_line
from drift.store.sqlite import Store
from evals.support import REPO, eval_config, pipeline_env, run_pipeline_night, sheet_for_outcome

CRITERIA = ("most_important_first", "understandable_in_30s", "themes_sensible", "tone_neutral", "next_actions_clear")
_FENCE = re.compile(r"\A```(?:json)?\s*\n(.*)\n```\s*\Z", re.DOTALL)


# ----------------------------------------------------------------------------------------- judge


class Judge:
    """EV-12. Configured separately (llm.steps.judge) from the models under test."""

    def __init__(self, cfg: AppConfig, mode: CassetteMode) -> None:
        self.prompt = load_prompt(cfg.prompts.dir / cfg.prompts.judge)
        inner = LLMClient(cfg.llm.providers, cfg.llm.for_step("judge"), os.environ)
        self.client = CassetteClient(inner, cfg.evals.cassette_dir / "judge", mode)
        self.model = cfg.llm.for_step("judge").model

    async def score(self, message: str, context: str) -> dict[str, Any]:
        convo = self.prompt.messages(message=message, context=context)
        last_error = ""
        for _ in range(2):
            try:
                resp = await self.client.complete(convo)
            except LLMError as exc:
                return {"error": f"{type(exc).__name__}: {exc}"}
            parsed, last_error = _parse_judge(resp.text)
            if parsed is not None:
                return parsed | {"latency_ms": resp.latency_ms}
            convo = [*convo, Message("assistant", resp.text), self.prompt.retry_message(errors=last_error)]
        return {"error": f"unusable judge output: {last_error}"}


def _parse_judge(text: str) -> tuple[dict[str, Any] | None, str]:
    body = text.strip()
    if m := _FENCE.match(body):
        body = m.group(1)
    try:
        data = json.loads(body)
        scores = {k: data["scores"][k] for k in CRITERIA}
        overall = data["overall"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return None, f"not the expected JSON ({type(exc).__name__})"
    values = [*scores.values(), overall]
    if not all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5 for v in values):
        return None, "every score must be an integer from 1 to 5"
    return {"scores": scores, "overall": overall, "rationale": str(data.get("rationale", ""))[:500]}, ""


def judge_context(out: Any) -> str:
    """What the judge needs to assess priority: severity counts plus the must-know clusters."""
    det = out.detection
    counts = ", ".join(f"{n} {s.name}" for s, n in sorted(det.by_severity().items(), reverse=True) if n)
    crit = sorted({f"{f.cluster_name} ({f.rule.value})" for f in det.findings if f.severity is Severity.CRITICAL})
    unr = sorted({f"{f.cluster_name} ({f.environment.value})" for f in det.findings if f.rule is Rule.UNREACHABLE})
    return (
        f"Findings: {counts or 'none'}. Clusters checked {det.checked}/{det.total}.\n"
        f"CRITICAL: {', '.join(crit) or 'none'}\nUnreachable: {', '.join(unr) or 'none'}"
    )


def fallback_message(env: Any, out: Any) -> str:
    """The code-only message for the same run (judge baseline)."""
    fmt = Fmt(env.cfg.channel.format)
    sheet = sheet_for_outcome(env, out)
    facts: RunFacts = sheet.facts
    return assemble(
        mentions_line(out.detection.findings, env.cfg.channel.mentions.on_critical),
        fallback_body(facts, fmt),
        appendix(out.detection.findings, fmt),
        footer(facts, "fallback", fmt),
    )


# ------------------------------------------------------------------------------------- models


async def run_model(
    name: str, cfg: AppConfig, mode: CassetteMode, nights: tuple[int, ...], workdir: Path
) -> dict[str, Any]:
    profile = cfg.evals.models[name]
    default = cfg.llm.default.model_copy(update=profile.model_dump(exclude_none=True))
    env = pipeline_env(workdir / name)
    env = env.with_cfg(llm=env.cfg.llm.model_copy(update={"default": default, "enabled": True}))
    clients: list[CassetteClient] = []

    def factory(step):  # type: ignore[no-untyped-def]
        c = CassetteClient(
            LLMClient(env.cfg.llm.providers, env.cfg.llm.for_step(step), os.environ),
            cfg.evals.cassette_dir / "chain",
            mode,
        )
        clients.append(c)
        return c

    chain = PromptChain(env.cfg, os.environ, client_factory=factory)
    results = []
    for n in nights:
        chain.traces = []
        t0 = time.perf_counter()
        out = await run_pipeline_night(env, n, summarizer=chain)
        wall = time.perf_counter() - t0
        posted = env.messages()[-1] if env.messages() else out.message
        check = gate2.check(posted, sheet_for_outcome(env, out), max_length=None, deny_list=env.cfg.deny_list)
        store = Store(env.cfg.storage.db_path)
        kinds = [e["kind"] for e in store.events(out.run_id)]
        store.close()
        reason = next((k for k in ("llm_unavailable", "chain_failed", "chain_error") if k in kinds), None)
        row: dict[str, Any] = {
            "night": n,
            "run_date": out.run_date.isoformat(),
            "source": out.summary_source,
            "fallback_reason": reason if out.summary_source == "fallback" else None,
            "length": len(posted),
            "limit": env.cfg.channel.max_length,
            "faithful": check.passed,
            "faithfulness_errors": [str(e) for e in check.errors],
            "steps": [asdict(t) for t in chain.traces],
            "wall_s": round(wall, 1),
            "message": posted,
        }
        row["judge_context"] = judge_context(out)
        if out.summary_source != "all_clear":
            row["fallback_message"] = fallback_message(env, out)
        results.append(row)
        print(
            f"  {name} night {n}: {out.summary_source} ({row['fallback_reason'] or 'ok'}) "
            f"len={row['length']} faithful={check.passed} {wall:.0f}s",
            file=sys.stderr,
        )
    return {
        "provider": default.provider,
        "model": default.model,
        "cassette": {"hits": sum(c.hits for c in clients), "misses": sum(c.misses for c in clients)},
        "nights": results,
        "metrics": model_metrics(results),
    }


def _pct(n: int, d: int) -> float | None:
    return round(100 * n / d, 1) if d else None


def model_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    steps: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        for s in r["steps"]:
            steps.setdefault(s["step"], []).append(s)
    step_metrics = {}
    for name, ss in steps.items():
        lat = [s["latency_ms"] for s in ss]
        step_metrics[name] = {
            "invocations": len(ss),
            "first_attempt_pass_pct": _pct(sum(s["first_attempt_passed"] for s in ss), len(ss)),
            "pass_after_retries_pct": _pct(sum(s["passed"] for s in ss), len(ss)),
            "mean_attempts": round(statistics.mean(s["attempts"] for s in ss), 2),
            "latency_ms_mean": int(statistics.mean(lat)) if lat else None,
            "latency_ms_max": max(lat) if lat else None,
            "prompt_tokens_total": sum(s["prompt_tokens"] for s in ss),
            "completion_tokens_total": sum(s["completion_tokens"] for s in ss),
        }
    summarised = [r for r in rows if r["source"] != "all_clear"]
    lengths = sorted(r["length"] for r in rows)
    limit = rows[0]["limit"] if rows else None

    def judge_means(key: str) -> dict[str, float] | None:
        js = [r[key] for r in rows if key in r and "error" not in r[key]]
        if not js:
            return None
        means = {c: round(statistics.mean(j["scores"][c] for j in js), 2) for c in CRITERIA}
        return means | {"overall": round(statistics.mean(j["overall"] for j in js), 2), "n": len(js)}

    return {
        "runs": len(rows),
        "llm_pct": _pct(sum(r["source"] == "llm" for r in summarised), len(summarised)),
        "fallback_pct": _pct(sum(r["source"] == "fallback" for r in summarised), len(summarised)),
        "fallback_reasons": sorted({r["fallback_reason"] for r in rows if r["fallback_reason"]}),
        "faithfulness_failures": sum(not r["faithful"] for r in rows),
        "steps": step_metrics,
        "length": {
            "limit": limit,
            "min": lengths[0] if lengths else None,
            "median": int(statistics.median(lengths)) if lengths else None,
            "max": lengths[-1] if lengths else None,
            "over_limit": sum(n > (limit or 0) for n in lengths),
            "all": lengths,
        },
        "judge": judge_means("judge"),
        "judge_fallback_baseline": judge_means("judge_fallback_baseline"),
    }


# ----------------------------------------------------------------------------------- hard gates


def run_hard_gates(out_dir: Path) -> dict[str, Any]:
    junit = out_dir / "hard_gates.xml"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(REPO / "evals" / "hard"),
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    by_ev: dict[str, dict[str, int]] = {}
    failures = []
    for case in ET.parse(junit).getroot().iter("testcase"):
        name = case.get("name", "")
        m = re.match(r"test_ev(\d\d)", name)
        ev = f"EV-{int(m.group(1))}" if m else "run behaviour"
        bucket = by_ev.setdefault(ev, {"passed": 0, "failed": 0})
        failed = case.find("failure") is not None or case.find("error") is not None
        bucket["failed" if failed else "passed"] += 1
        if failed:
            failures.append(name)
    return {
        "passed": proc.returncode == 0,
        "by_ev": dict(sorted(by_ev.items(), key=_ev_key)),
        "failures": failures,
        "summary": proc.stdout.strip().splitlines()[-1] if proc.stdout else "",
    }


def _ev_key(item: tuple[str, Any]) -> tuple[int, str]:
    m = re.match(r"EV-(\d+)", item[0])
    return (int(m.group(1)) if m else 99, item[0])


# ------------------------------------------------------------------------------ human agreement


async def human_agreement(cfg: AppConfig, judge: Judge | None) -> dict[str, Any]:
    path = cfg.evals.human_labels
    if not path.is_file():
        return {"status": "no labelled set", "path": str(path)}
    items = (yaml.safe_load(path.read_text()) or {}).get("items", [])
    labelled = [i for i in items if isinstance(i.get("human_score"), int)]
    result: dict[str, Any] = {"items": len(items), "labelled": len(labelled), "required": 15}
    if len(labelled) < 15:
        return result | {"status": f"pending: {len(labelled)}/15 items have a human score"}
    if judge is None:
        return result | {"status": "judge disabled"}
    pairs = []
    for item in labelled:
        j = await judge.score(item["message"], item.get("context", ""))
        if "error" not in j:
            pairs.append((item["human_score"], j["overall"], item["id"]))
    if len(pairs) < 2:
        return result | {"status": "judge failed on the labelled set"}
    human = [p[0] for p in pairs]
    judged = [p[1] for p in pairs]
    diffs = [abs(h - j) for h, j in zip(human, judged, strict=True)]
    return result | {
        "status": "ok",
        "scored": len(pairs),
        "mean_abs_error": round(statistics.mean(diffs), 2),
        "exact_pct": _pct(sum(d == 0 for d in diffs), len(diffs)),
        "within_1_pct": _pct(sum(d <= 1 for d in diffs), len(diffs)),
        "spearman": _spearman(human, judged),
        "pairs": [{"id": i, "human": h, "judge": j} for h, j, i in pairs],
    }


def _spearman(a: list[int], b: list[int]) -> float | None:
    def ranks(xs: list[int]) -> list[float]:
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        r = [0.0] * len(xs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r

    ra, rb = ranks(a), ranks(b)
    try:
        return round(statistics.correlation(ra, rb), 3)
    except statistics.StatisticsError:
        return None  # constant input


# ----------------------------------------------------------------------------------------- report


def _git() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()
        return sha + ("+dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


async def run_eval(
    models: list[str], mode: CassetteMode, nights: tuple[int, ...], hard: bool, judge_on: bool, human: bool
) -> tuple[dict[str, Any], Path]:
    cfg = eval_config()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = cfg.evals.reports_dir / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    prompts = {
        s: load_prompt(cfg.prompts.dir / getattr(cfg.prompts, s)).version
        for s in ("group", "draft", "shorten", "judge")
    }
    report: dict[str, Any] = {
        "id": stamp,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "git": _git(),
        "mode": mode,
        "nights": list(nights),
        "prompts": prompts,
        "judge_model": cfg.llm.for_step("judge").model if judge_on else None,
        "message_limit": cfg.channel.max_length,
    }
    if hard:
        print("hard gates (EV-1..EV-10) ...", file=sys.stderr)
        report["hard_gates"] = run_hard_gates(out_dir)
    judge = Judge(cfg, mode) if judge_on else None
    report["models"] = {}
    with tempfile.TemporaryDirectory(prefix="drift-eval-") as tmp:
        for name in models:
            print(f"model {name} ({mode}) ...", file=sys.stderr)
            report["models"][name] = await run_model(name, cfg, mode, nights, Path(tmp))
    if judge is not None:
        # One pass after all chain runs, so a single local GPU does not swap models every night.
        print(f"judging with {judge.model} ...", file=sys.stderr)
        baseline_cache: dict[str, dict[str, Any]] = {}
        for m in report["models"].values():
            for row in m["nights"]:
                if row["source"] == "llm":
                    row["judge"] = await judge.score(row["message"], row["judge_context"])
                if row.get("fallback_message"):
                    fb = row["fallback_message"]
                    if fb not in baseline_cache:
                        baseline_cache[fb] = await judge.score(fb, row["judge_context"])
                    row["judge_fallback_baseline"] = baseline_cache[fb]
            m["metrics"] = model_metrics(m["nights"])
    if human:
        report["human_agreement"] = await human_agreement(cfg, judge)
    if judge is not None:
        report["judge_cassette"] = {"hits": judge.client.hits, "misses": judge.client.misses}
    report["warnings"] = warnings(report)
    report["verdict"] = verdict(report)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    (out_dir / "report.md").write_text(render_markdown(report))
    latest = cfg.evals.reports_dir / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(stamp)
    return report, out_dir


def warnings(report: dict[str, Any]) -> list[str]:
    """Things that do not fail the run but weaken what the numbers mean."""
    out = []
    jm = report.get("judge_model")
    same = [n for n, m in report.get("models", {}).items() if jm and m["model"] == jm]
    if same:
        out.append(
            f"judge model {jm} is also under test ({', '.join(same)}): scores for its own messages may be "
            "biased; set a different llm.steps.judge for a fair comparison"
        )
    ha = report.get("human_agreement") or {}
    if ha and ha.get("status") != "ok":
        out.append(f"judge not yet validated against human labels (EV-13): {ha.get('status')}")
    return out


def verdict(report: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    if "hard_gates" in report and not report["hard_gates"]["passed"]:
        reasons.append("hard gates failed: " + ", ".join(report["hard_gates"]["failures"][:5]))
    for name, m in report.get("models", {}).items():
        if m["metrics"]["faithfulness_failures"]:
            reasons.append(f"{name}: {m['metrics']['faithfulness_failures']} posted message(s) failed EV-3")
        if report["mode"] == "replay" and m["cassette"]["misses"]:
            reasons.append(f"{name}: {m['cassette']['misses']} cassette miss(es) — replay incomplete, re-record")
    jc = report.get("judge_cassette")
    if report["mode"] == "replay" and jc and jc["misses"]:
        reasons.append(f"judge: {jc['misses']} cassette miss(es) — replay incomplete, re-record")
    return {"status": "FAIL" if reasons else "PASS", "reasons": reasons}


def _fmt(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def render_markdown(r: dict[str, Any]) -> str:
    lines = [
        f"# Eval report {r['id']}",
        "",
        f"**Verdict: {r['verdict']['status']}**"
        + (" — " + "; ".join(r["verdict"]["reasons"]) if r["verdict"]["reasons"] else ""),
        "",
        f"git `{r['git']}` · mode `{r['mode']}` · nights {r['nights']} · prompts "
        + ", ".join(f"{k}=`{v}`" for k, v in r["prompts"].items())
        + f" · judge `{r['judge_model']}` · limit {r['message_limit']} chars",
        "",
    ]
    if r.get("warnings"):
        lines += ["**Warnings**", "", *(f"- {w}" for w in r["warnings"]), ""]
    if "hard_gates" in r:
        hg = r["hard_gates"]
        lines += ["## Hard gates (must be 100%)", "", "| Gate | Passed | Failed |", "|---|---|---|"]
        lines += [f"| {ev} | {v['passed']} | {v['failed']} |" for ev, v in hg["by_ev"].items()]
        lines += ["", f"`{hg['summary']}`", ""]
    models = r.get("models", {})
    if models:
        names = list(models)
        hdr = "| Metric | " + " | ".join(f"{n} (`{models[n]['model']}`)" for n in names) + " |"
        sep = "|---|" + "---|" * len(names)

        def row(label: str, get: Any) -> str:
            return f"| {label} | " + " | ".join(_fmt(get(models[n])) for n in names) + " |"

        lines += [
            "## Models side by side (EV-16)",
            "",
            hdr,
            sep,
            row("EV-3 posted messages failing faithfulness", lambda m: m["metrics"]["faithfulness_failures"]),
            row("LLM-written summaries %", lambda m: m["metrics"]["llm_pct"]),
            row("fallback %", lambda m: m["metrics"]["fallback_pct"]),
            row("fallback reasons", lambda m: ", ".join(m["metrics"]["fallback_reasons"]) or None),
        ]
        steps = sorted(
            {s for n in names for s in models[n]["metrics"]["steps"]},
            key=lambda s: ["group", "draft", "shorten"].index(s),
        )
        for s in steps:

            def g(m: dict[str, Any], k: str, s: str = s) -> Any:
                return m["metrics"]["steps"].get(s, {}).get(k)

            lines += [
                row(f"EV-11 {s}: first-attempt pass %", lambda m, s=s: g(m, "first_attempt_pass_pct", s)),
                row(f"EV-11 {s}: pass after retries %", lambda m, s=s: g(m, "pass_after_retries_pct", s)),
                row(f"EV-11 {s}: mean attempts", lambda m, s=s: g(m, "mean_attempts", s)),
                row(
                    f"EV-11 {s}: mean latency s",
                    lambda m, s=s: (
                        round(g(m, "latency_ms_mean", s) / 1000, 1) if g(m, "latency_ms_mean", s) is not None else None
                    ),
                ),
            ]
        lines += [
            row(
                "EV-14 length min / median / max",
                lambda m: (
                    f"{m['metrics']['length']['min']} / {m['metrics']['length']['median']} / "
                    f"{m['metrics']['length']['max']}"
                ),
            ),
            row("EV-14 over limit", lambda m: m["metrics"]["length"]["over_limit"]),
        ]
        for c in (*CRITERIA, "overall"):
            lines.append(row(f"EV-12 judge {c}", lambda m, c=c: (m["metrics"]["judge"] or {}).get(c)))
        lines.append(
            row(
                "EV-12 judge overall — fallback baseline",
                lambda m: (m["metrics"]["judge_fallback_baseline"] or {}).get("overall"),
            )
        )
        lines.append(row("cassette hits / misses", lambda m: f"{m['cassette']['hits']} / {m['cassette']['misses']}"))
        lines += ["", "_Judge scores are reported alongside, never instead of, the hard gates._", ""]
        lines += ["## Per night", ""]
        for n in names:
            lines += [
                f"### {n}",
                "",
                "| Night | Source | Length | Faithful | Attempts (group/draft/shorten) | Judge |",
                "|---|---|---|---|---|---|",
            ]
            for row_ in models[n]["nights"]:
                att = "/".join(
                    str(next((s["attempts"] for s in row_["steps"] if s["step"] == st), "–"))
                    for st in ("group", "draft", "shorten")
                )
                j = row_.get("judge", {})
                lines.append(
                    f"| {row_['night']} ({row_['run_date']}) | {row_['source']}"
                    f"{' (' + row_['fallback_reason'] + ')' if row_['fallback_reason'] else ''} | "
                    f"{row_['length']} | {'yes' if row_['faithful'] else 'NO'} | {att} | "
                    f"{j.get('overall', j.get('error', '–'))} |"
                )
            lines.append("")
    ha = r.get("human_agreement")
    if ha:
        lines += ["## Judge vs human (EV-13)", ""]
        if ha.get("status") == "ok":
            lines += [
                f"{ha['scored']} labelled messages · mean absolute error {ha['mean_abs_error']} · "
                f"exact {ha['exact_pct']}% · within one point {ha['within_1_pct']}% · "
                f"Spearman {ha['spearman']}",
                "",
            ]
        else:
            lines += [f"Status: {ha['status']}", ""]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------------- compare


def compare(a_path: Path, b_path: Path) -> str:
    a = json.loads((a_path / "report.json" if a_path.is_dir() else a_path).read_text())
    b = json.loads((b_path / "report.json" if b_path.is_dir() else b_path).read_text())
    lines = [
        f"# Compare {a['id']} → {b['id']}",
        "",
        f"git `{a['git']}` → `{b['git']}` · prompts {a['prompts']} → {b['prompts']}",
        "",
        f"Verdict: {a['verdict']['status']} → {b['verdict']['status']}",
        "",
        "| Model | Metric | A | B | Δ |",
        "|---|---|---|---|---|",
    ]
    keys = [
        ("faithfulness_failures",),
        ("llm_pct",),
        ("fallback_pct",),
        ("steps", "group", "first_attempt_pass_pct"),
        ("steps", "draft", "first_attempt_pass_pct"),
        ("steps", "draft", "mean_attempts"),
        ("length", "median"),
        ("length", "over_limit"),
        ("judge", "overall"),
    ]
    for model in sorted(set(a.get("models", {})) | set(b.get("models", {}))):
        for k in keys:
            va, vb = _dig(a, model, k), _dig(b, model, k)
            delta = round(vb - va, 2) if isinstance(va, int | float) and isinstance(vb, int | float) else "–"
            lines.append(f"| {model} | {'.'.join(k)} | {_fmt(va)} | {_fmt(vb)} | {_fmt(delta)} |")
    return "\n".join(lines) + "\n"


def _dig(report: dict[str, Any], model: str, keys: tuple[str, ...]) -> Any:
    node: Any = report.get("models", {}).get(model, {}).get("metrics")
    for k in keys:
        node = node.get(k) if isinstance(node, dict) else None
    return node


# ------------------------------------------------------------------------------ label export


def export_labels(report_path: Path, labels: Path, limit: int = 15) -> int:
    """Write messages from a report into the human-label file (human_score left empty for a person)."""
    r = json.loads((report_path / "report.json" if report_path.is_dir() else report_path).read_text())
    existing = yaml.safe_load(labels.read_text()) if labels.is_file() else None
    items = (existing or {}).get("items", [])
    seen = {i["message"] for i in items}
    for name, m in r.get("models", {}).items():
        for row in m["nights"]:
            candidates = (
                ("llm", row["message"] if row["source"] == "llm" else None),
                ("fallback", row.get("fallback_message")),
            )
            for key, text in candidates:
                if text and text not in seen and len(items) < limit:
                    seen.add(text)
                    items.append(
                        {
                            "id": f"H{len(items) + 1:02d}",
                            "source": f"{r['id']} {name} night {row['night']} {key}",
                            "message": text,
                            "context": row.get("judge_context", ""),
                            "human_score": None,
                            "notes": "",
                        }
                    )
    labels.parent.mkdir(parents=True, exist_ok=True)
    labels.write_text(
        "# EV-13 human-labelled set. Score each message 1-5 using the rubric in prompts/judge_v1.md.j2\n"
        "# (overall usefulness to an on-call engineer). Leave human_score empty if unsure. Needs >= 15 scored.\n"
        + yaml.safe_dump({"items": items}, sort_keys=False, allow_unicode=True, width=120)
    )
    return len(items)
