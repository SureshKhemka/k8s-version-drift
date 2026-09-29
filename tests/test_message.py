"""Code-generated message parts (FR-19..FR-24) and exit-code precedence (FR-29)."""

from datetime import date

from drift.diff.tagging import tag_changes
from drift.domain.enums import ChangeTag, ExitCode, Rule, Severity
from drift.message.compose import (
    Fmt,
    RunFacts,
    all_clear_body,
    appendix,
    appendix_findings,
    assemble,
    body_budget,
    changes_line,
    fallback_body,
    footer,
    is_all_clear,
    mentions_line,
)
from drift.pipeline import exit_code_for, scrub
from drift.publish.channel import PostResult
from drift.rules.base import Finding, finding_id
from drift.rules.engine import Detection

D = date(2026, 10, 1)
SLACK, PLAIN = Fmt("slack_mrkdwn"), Fmt("plain")


def F(cid, rule, sev, subject="control-plane", observed="1.28.15", limit="2026-09-30", nodes=None, env="prod"):
    return Finding(
        id=finding_id(cid, rule, subject),
        cluster_id=cid,
        cluster_name=f"svc-{cid}",
        environment=env,
        provider="eks",
        rule=rule,
        severity=sev,
        subject=subject,
        observed=observed,
        limit=limit,
        nodes_affected=nodes,
    )


def facts(findings, first_run=True, total=10, compliant=7):
    det = Detection(tuple(findings), tuple(f"ok{i}" for i in range(compliant)), total - 1, total, 0, 0)
    ch = tag_changes(findings, [f.cluster_id for f in findings], None, (30,))
    return RunFacts.build("2026-10-01-abc123", D, det, ch, "m-1")


CRIT = F("a", Rule.CP_EOL, Severity.CRITICAL)
NODE = F("a", Rule.NODE_EOL, Severity.CRITICAL, subject="ng-1", nodes=1)
UNR = F("b", Rule.UNREACHABLE, Severity.HIGH, subject="cluster", observed="timeout", limit=None)
LOW = F("c", Rule.CP_PATCH_BEHIND, Severity.LOW, observed="1.35.1", limit="1.35.4")


def test_appendix_has_every_critical_and_unreachable_only():
    assert appendix_findings([CRIT, NODE, UNR, LOW]) == [CRIT, NODE, UNR]
    text = appendix([CRIT, NODE, UNR, LOW], SLACK)
    assert "• svc-a — CP_EOL — 1.28.15 — limit 2026-09-30" in text
    assert "• svc-a / ng-1 — NODE_EOL — 1.28.15 — limit 2026-09-30 — 1 node" in text
    assert "• svc-b — UNREACHABLE — timeout" in text
    assert "svc-c" not in text
    assert appendix([LOW], SLACK) == ""


def test_all_clear_rule():
    assert is_all_clear([LOW])
    assert is_all_clear([])
    assert not is_all_clear([UNR.model_copy(update={"severity": Severity.LOW})])  # unreachable at any severity
    assert not is_all_clear([F("d", Rule.NODE_POOL_MIXED, Severity.MEDIUM)])
    body = all_clear_body(facts([LOW]), PLAIN)
    assert "All clear" in body and "1 LOW finding not shown" in body


def test_mentions_only_with_critical():
    assert mentions_line([CRIT, UNR], ("@oncall",)) == "@oncall"
    assert mentions_line([UNR, LOW], ("@oncall",)) == ""
    assert mentions_line([CRIT], ()) == ""


def test_fallback_body_counts_and_first_run():
    fx = facts([CRIT, NODE, UNR, LOW])
    body = fallback_body(fx, SLACK)
    assert body.splitlines()[0] == "*Kubernetes version drift — 2026-10-01*"
    assert "🔴 2 CRITICAL · 1 HIGH · 1 LOW (4 findings) on 3 clusters; 1 cluster unreachable." in body
    assert "First run" in body


def test_changes_line_after_first_run():
    fx = facts([CRIT], first_run=False)
    fx = RunFacts(
        **{
            **fx.__dict__,
            "first_run": False,
            "change_counts": {ChangeTag.NEW: 2, ChangeTag.WORSENED: 1, ChangeTag.RESOLVED: 3},
            "new_clusters": 1,
        }
    )
    assert changes_line(fx) == "Since last run: 2 new · 1 worsened · 3 resolved · 1 new cluster."


def test_footer_states_every_fr20_field():
    text = footer(facts([CRIT]), "fallback", SLACK)
    for part in (
        "Run date 2026-10-01",
        "checked 9/10 clusters",
        "7 compliant",
        "matrix m-1",
        "summary: fallback (code-generated)",
        "run ID 2026-10-01-abc123",
    ):
        assert part in text
    assert "LLM-written (qwen)" in footer(facts([CRIT]), "llm", SLACK, "qwen")


def test_assemble_and_budget():
    msg = assemble("@oncall", "BODY", "APPX", "FOOT")
    assert msg == "@oncall\n\nBODY\n\nAPPX\n\nFOOT"
    budget = body_budget(len(msg), "@oncall", "APPX", "FOOT")
    assert budget == len("BODY")
    assert body_budget(5, "@oncall", "APPX" * 10, "FOOT") == 0


def test_slack_escaping_of_data():
    weird = F("x", Rule.UNREACHABLE, Severity.HIGH, subject="cluster", observed="bad <html> & stuff", limit=None)
    assert "bad &lt;html&gt; &amp; stuff" in appendix([weird], SLACK)
    assert "bad <html> & stuff" in appendix([weird], PLAIN)


def test_scrub_removes_denylisted_strings():
    text, hits = scrub("see https://10.1.2.3:6443 acct 123456789012", [r"https?://\S+", r"\b\d{12}\b"])
    assert "https" not in text and "123456789012" not in text and len(hits) == 2


def test_exit_code_precedence():
    failed, posted = PostResult("POST_FAILED", "x"), PostResult("POSTED", "x")
    assert exit_code_for(True, "fallback", failed) is ExitCode.POST_FAILED
    assert exit_code_for(True, "fallback", posted) is ExitCode.PARTIAL
    assert exit_code_for(False, "fallback", posted) is ExitCode.SUCCESS_FALLBACK
    assert exit_code_for(False, "llm", posted) is ExitCode.SUCCESS
    assert exit_code_for(False, "all_clear", PostResult("SILENT", "x")) is ExitCode.SUCCESS
