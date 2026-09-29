"""Hard gates on whole runs (EV-4..EV-8), exercised through the real pipeline against both mocks.

These hold for every summary source. Until the LLM chain lands (M6) runs use the fallback; in M6 the
same checks are re-run with LLM-written bodies.
"""

from __future__ import annotations

import asyncio
import io
import re
import sqlite3
from pathlib import Path

import pytest

from drift.domain.enums import ExitCode, Rule, Severity
from drift.logs import setup as setup_logging
from drift.message.compose import Fmt, appendix_line
from drift.store.recorder import Redactor
from drift_mocks.faults import FaultSpec
from evals.support import EVAL_SECRETS, NIGHTS, pipeline_env, run_pipeline_night


@pytest.fixture(scope="module")
def sequence(tmp_path_factory):
    """Nights 0..4, in order, full pipeline, POST mode. Logs captured through the redacting handler."""
    root = tmp_path_factory.mktemp("ev-seq")
    env = pipeline_env(root)
    log = io.StringIO()
    cfg = env.cfg
    names = [cfg.clusters.token_env, cfg.channel.webhook_url_env, *(p.api_key_env for p in cfg.llm.providers.values())]
    setup_logging("DEBUG", Redactor.from_env(names, env.environ, cfg.deny_list), log)

    async def go():
        return [await run_pipeline_night(env, n) for n in NIGHTS]

    outcomes = asyncio.run(go())
    return env, outcomes, log.getvalue()


# ------------------------------------------------------------------------------------ EV-4


def test_ev04_every_critical_and_prod_unreachable_in_every_message(sequence):
    env, outcomes, _ = sequence
    fmt = Fmt(env.cfg.channel.format)
    problems = []
    for night, out in zip(NIGHTS, outcomes, strict=True):
        assert out.post.status == "POSTED", f"night {night}: {out.post.status}"
        for f in out.detection.findings:
            must = f.severity is Severity.CRITICAL or (f.rule is Rule.UNREACHABLE and f.environment.value == "prod")
            if must and appendix_line(f, fmt) not in out.message:
                problems.append(f"night {night}: missing {f.cluster_name} {f.rule.value} {f.subject}")
    assert not problems, "\n".join(problems)
    # the posted text is exactly what the pipeline composed
    assert env.messages() == [o.message for o in outcomes]


# ------------------------------------------------------------------------------------ EV-5


def test_ev05_no_write_call_to_any_cluster(sequence):
    env, _, _ = sequence
    calls = env.k8s.calls.calls
    assert calls, "expected the mock to have been called"
    writes = [c for c in calls if c["method"] != "GET"]
    assert writes == []


# ------------------------------------------------------------------------------------ EV-6


def _everything_written(env) -> dict[str, str]:
    """Every artefact a run leaves behind: messages, outbox, run JSONL, SQLite contents."""
    blobs = {f"message[{i}]": m for i, m in enumerate(env.messages())}
    for p in sorted(Path(env.root).rglob("*")):
        if p.is_file() and p.suffix in (".txt", ".json", ".jsonl") and "mocks" not in p.parts:
            blobs[str(p.relative_to(env.root))] = p.read_text()
    con = sqlite3.connect(env.cfg.storage.db_path)
    for table in ("runs", "run_events", "findings_history", "run_clusters", "posts"):
        blobs[f"sqlite:{table}"] = "\n".join(map(str, con.execute(f"SELECT * FROM {table}").fetchall()))
    con.close()
    return blobs


def test_ev06_no_secret_endpoint_or_denylisted_string_anywhere(sequence):
    env, _, logs = sequence
    blobs = _everything_written(env) | {"logs": logs}
    forbidden = [*EVAL_SECRETS.values(), "k8s.mock", "channel.mock", "/clusters/c0", "T-SECRET-HOOK"]
    problems = [f"{name}: contains {s!r}" for name, text in blobs.items() for s in forbidden if s in text]
    deny = [re.compile(p) for p in env.cfg.deny_list]
    for name, text in blobs.items():
        if name.startswith("message") or name.startswith("outbox"):
            problems += [f"{name}: deny-list {rx.pattern!r}" for rx in deny if rx.search(text)]
    assert not problems, "\n".join(problems)
    assert "collected" in logs  # logging really ran through the redacting handler


# ------------------------------------------------------------------------------------ EV-7


class _Raises:
    async def summarize(self, req, recorder):
        raise RuntimeError("LLM unreachable at http://127.0.0.1:1234/v1")


class _GivesUp:
    async def summarize(self, req, recorder):
        return None  # every chain step failed its gate after retries


def _fast(env):
    """Replace slow timeout faults so these runs take milliseconds, not seconds."""
    env.k8s.overrides = {
        "c047": FaultSpec(mode="auth_error"),
        "c021": FaultSpec(),
        "c014": FaultSpec(mode="auth_error"),
    }
    return env


@pytest.mark.parametrize("summarizer", [None, _Raises(), _GivesUp()], ids=["no-llm", "llm-down", "all-steps-fail"])
async def test_ev07_message_posted_even_when_llm_fails(tmp_path, summarizer):
    env = _fast(pipeline_env(tmp_path))
    env = env.with_cfg(llm=env.cfg.llm.model_copy(update={"enabled": True}))
    out = await run_pipeline_night(env, 0, summarizer=summarizer)
    assert out.post.status == "POSTED" and out.summary_source == "fallback"
    assert len(env.messages()) == 1
    assert "127.0.0.1" not in env.messages()[0]


async def test_ev07_flaky_channel_still_gets_the_message(tmp_path):
    env = _fast(pipeline_env(tmp_path))
    env.channel.fault = FaultSpec(script=["http_error:500", "http_error:503"])
    out = await run_pipeline_night(env, 0)
    assert out.post.status == "POSTED" and len(env.messages()) == 1
    assert [a["outcome"] for a in out.post.attempts] == ["HTTP 500", "HTTP 503", "ok"]


async def test_channel_down_is_post_failed_and_saved_locally(tmp_path):
    env = _fast(pipeline_env(tmp_path))
    env.channel.fault = FaultSpec(mode="http_error", status=500)
    out = await run_pipeline_night(env, 0)
    assert out.post.status == "POST_FAILED" and out.exit_code is ExitCode.POST_FAILED
    assert Path(out.post.outbox_path).read_text().strip() == out.message.strip()
    assert env.messages() == []


# ------------------------------------------------------------------------------------ EV-8


async def test_ev08_no_run_date_posted_twice_without_force(tmp_path):
    env = _fast(pipeline_env(tmp_path))
    first = await run_pipeline_night(env, 0)
    again = await run_pipeline_night(env, 0)
    assert (first.post.status, again.post.status) == ("POSTED", "ALREADY_POSTED")
    assert len(env.messages()) == 1
    assert Path(again.post.outbox_path).is_file()  # local record still updated (FR-26)
    forced = await run_pipeline_night(env, 0, force=True)
    assert forced.post.status == "POSTED" and len(env.messages()) == 2
    nxt = await run_pipeline_night(env, 1)
    assert nxt.post.status == "POSTED" and len(env.messages()) == 3


async def test_ev08_failed_post_does_not_block_a_retry(tmp_path):
    env = _fast(pipeline_env(tmp_path))
    env.channel.fault = FaultSpec(mode="http_error")
    assert (await run_pipeline_night(env, 0)).post.status == "POST_FAILED"
    env.channel.fault = None
    assert (await run_pipeline_night(env, 0)).post.status == "POSTED"
    assert len(env.messages()) == 1


# ------------------------------------------------------------------------- run behaviour (FR-21, CF-4)


async def test_all_clear_run_and_silent_option(tmp_path):
    env = _fast(pipeline_env(tmp_path))
    out = await run_pipeline_night(env, 0, clusters=["payments-prod-1", "c023"])
    assert out.summary_source == "all_clear" and out.post.status == "DRY_RUN"  # subsets never post (D30)
    assert "All clear" in out.message and "summary: all clear" in out.message
    assert out.exit_code is ExitCode.SUCCESS
    quiet = env.with_cfg(channel=env.cfg.channel.model_copy(update={"post_all_clear": False}))
    out = await run_pipeline_night(quiet, 0, clusters=["c001"])
    assert out.post.status == "SILENT" and env.messages() == []


async def test_bad_inventory_is_config_error_before_any_cluster_call(tmp_path):
    env = pipeline_env(tmp_path)
    bad = env.with_cfg(
        sources=env.cfg.sources.model_copy(
            update={"inventory": env.cfg.sources.inventory.model_copy(update={"path": tmp_path / "missing.yaml"})}
        )
    )
    out = await run_pipeline_night(bad, 0)
    assert out.exit_code is ExitCode.CONFIG_ERROR and out.run_id is None
    assert env.k8s.calls.calls == [] and env.messages() == []


# ------------------------------------------------------------------------------------ EV-3


def _sheet_for(env, out):
    from drift.chain.facts import FactSheet
    from drift.message.compose import RunFacts

    matrix_version = (
        next(line for line in out.message.splitlines() if "matrix " in line).split("matrix ")[1].split(" ·")[0]
    )
    facts = RunFacts.build(out.run_id, out.run_date, out.detection, out.changes, matrix_version)
    clusters = env.k8s.world.clusters.values()
    return FactSheet.build(
        facts,
        out.changes,
        [c["name"] for c in clusters],
        [c["id"] for c in clusters],
        env.cfg.channel.mentions.on_critical,
    )


def test_ev03_no_posted_message_states_a_fact_not_in_the_findings(sequence):
    from drift.chain.gates import gate2

    env, outcomes, _ = sequence
    posted = env.messages()
    assert len(posted) == len(outcomes)
    problems = []
    for out, text in zip(outcomes, posted, strict=True):
        res = gate2.check(text, _sheet_for(env, out), max_length=None, deny_list=env.cfg.deny_list)
        problems += [f"{out.run_date} ({out.summary_source}): {e}" for e in res.errors]
    assert not problems, "\n".join(problems)
