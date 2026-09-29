"""Gate 1 — structure and coverage of Step 1's themes (CH-2).

Malformed output is a failure, never something to repair (LL-3). The only normalisation is removing one
surrounding Markdown code fence, because the content inside is then parsed strictly (D35).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from drift.chain.facts import FactSheet, Theme
from drift.chain.gates.gate2 import GateError, GateResult
from drift.domain.enums import ChangeTag, Severity

GROUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["themes"],
    "properties": {
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "priority", "finding_ids"],
                "properties": {
                    "title": {"type": "string"},
                    "priority": {"type": "integer"},
                    "finding_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}

_FENCE = re.compile(r"\A```(?:json)?\s*\n(.*)\n```\s*\Z", re.DOTALL)


def must_cover(sheet: FactSheet) -> set[str]:
    """CH-2: every CRITICAL and HIGH finding, and every NEW/WORSENED finding at MEDIUM or above."""
    out = set()
    for ff in sheet.findings:
        sev = ff.finding.severity
        if sev >= Severity.HIGH or (
            sev >= Severity.MEDIUM and ff.tag in (ChangeTag.NEW, ChangeTag.WORSENED, ChangeTag.NEW_CLUSTER)
        ):
            out.add(ff.alias)
    return out


def parse_and_check(text: str, sheet: FactSheet, max_themes: int) -> tuple[GateResult, list[Theme]]:
    body = text.strip()
    if m := _FENCE.match(body):
        body = m.group(1)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        return GateResult((GateError("PARSE", f"output is not valid JSON ({exc.msg} at char {exc.pos})"),)), []
    errors = _schema_errors(data)
    if errors:
        return GateResult(tuple(errors)), []

    themes = [Theme(t["title"].strip(), t["priority"], tuple(t["finding_ids"])) for t in data["themes"]]
    known = sheet.by_alias()
    if not themes:
        errors.append(GateError("NO_THEMES", "at least one theme is required"))
    if len(themes) > max_themes:
        errors.append(GateError("TOO_MANY_THEMES", f"{len(themes)} themes; the maximum is {max_themes}"))
    prios = [t.priority for t in themes]
    if len(set(prios)) != len(prios) or any(p < 1 for p in prios):
        errors.append(GateError("BAD_PRIORITY", "priorities must be distinct integers starting at 1"))
    for t in themes:
        if not t.title or len(t.title) > 120:
            errors.append(GateError("BAD_TITLE", f"theme titles must be 1-120 characters (got {len(t.title)})"))
        if not t.aliases:
            errors.append(GateError("EMPTY_THEME", f"theme '{t.title[:40]}' has no finding IDs"))
    counts = Counter(a for t in themes for a in t.aliases)
    for alias in sorted(counts, key=_alias_key):
        if alias not in known:
            errors.append(GateError("UNKNOWN_ID", f"{alias} is not a finding ID"))
        elif counts[alias] > 1:
            errors.append(GateError("DUPLICATE_ID", f"{alias} appears in more than one theme"))
    missing = sorted(must_cover(sheet) - set(counts), key=_alias_key)
    if missing:
        errors.append(GateError("MISSING_COVERAGE", f"these findings must be in a theme: {', '.join(missing)}"))
    return GateResult(tuple(errors)), themes if not errors else []


def _alias_key(a: str) -> tuple[int, str]:
    return (int(a[1:]), a) if a[1:].isdigit() else (10**9, a)


def _schema_errors(data: Any) -> list[GateError]:
    if not isinstance(data, dict) or set(data) != {"themes"} or not isinstance(data["themes"], list):
        return [GateError("SCHEMA", 'expected exactly {"themes": [...]}')]
    errs = []
    for i, t in enumerate(data["themes"]):
        if not isinstance(t, dict) or set(t) != {"title", "priority", "finding_ids"}:
            errs.append(GateError("SCHEMA", f"theme {i + 1} must have exactly title, priority, finding_ids"))
            continue
        if not isinstance(t["title"], str):
            errs.append(GateError("SCHEMA", f"theme {i + 1}: title must be a string"))
        if not isinstance(t["priority"], int) or isinstance(t["priority"], bool):
            errs.append(GateError("SCHEMA", f"theme {i + 1}: priority must be an integer"))
        ids = t["finding_ids"]
        if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
            errs.append(GateError("SCHEMA", f"theme {i + 1}: finding_ids must be a list of strings"))
    return errs
