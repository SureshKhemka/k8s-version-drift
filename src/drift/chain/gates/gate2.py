"""Gate 2 — faithfulness (CH-4). Deterministic: extracts every cluster name, version, date and number from a
text and checks each against the fact sheet.

Extraction order (each step masks what it consumed so later steps do not re-read it):
  1. literals (run ID, matrix version, mentions, unreachable reasons, raw unparseable strings)
  2. dates (ISO and month-name)
  3. hyphenated names: known cluster / known pool / cluster-looking but unknown
  4. rule IDs, internal IDs (finding aliases, cluster IDs)
  5. versions
  6. counts ("N clusters", "76/80 clusters", "3 CRITICAL", "in 59 days", "three nodes") and bare numbers

Checks are per segment (line, sentence, or clause) so a value must fit the clusters named next to it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from drift.chain.facts import VERSION_RX, FactSheet
from drift.domain.enums import Rule, Severity


@dataclass(frozen=True)
class GateError:
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True)
class GateResult:
    errors: tuple[GateError, ...] = ()
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.errors

    @property
    def only_length(self) -> bool:
        return bool(self.errors) and all(e.code == "TOO_LONG" for e in self.errors)

    def feedback(self) -> str:
        return "\n".join(f"- {e}" for e in self.errors)


# ----------------------------------------------------------------------------- patterns

MONTHS = {
    m: i
    for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)
}
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
MONTH_DATE = re.compile(
    r"\b(\d{1,2})?\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s*"
    r"(\d{1,2})?(?:st|nd|rd|th)?,?\s*(\d{4})?\b",
    re.IGNORECASE,
)
HYPHEN_NAME = re.compile(r"(?<![\w.-])[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+(?![\w-])")
CLUSTER_LIKE = re.compile(r"(?:^|-)(?:prod|production|staging|stage|stg|dev|test|qa)(?:-\d+)?$|-\d+$", re.IGNORECASE)
RULE_ID = re.compile(r"\b[A-Z]+(?:_[A-Z]+)+\b")
ALIAS = re.compile(r"\bf\d{1,4}\b")
LIST_MARKER = re.compile(r"^(\s*)(\d{1,2})[.)](?=\s)", re.MULTILINE)
RATIO = re.compile(r"(?<![\w.])(\d+)\s*/\s*(\d+)\s+(clusters?)\b", re.IGNORECASE)
_WORDS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty"
)
NUMBER_WORDS = {w: i for i, w in enumerate(_WORDS.split())}
NUMBER = re.compile(r"(?<![\w.#/-])(\d+|" + "|".join(NUMBER_WORDS) + r")(?![\w]|\.\d|/\d)", re.IGNORECASE)
MASK = "█"  # full block; never appears in facts

NOUNS = {
    "cluster": "cluster",
    "clusters": "cluster",
    "compliant": "cluster",
    "unreachable": "cluster",
    "reachable": "cluster",
    "finding": "finding",
    "findings": "finding",
    "issue": "finding",
    "issues": "finding",
    "problem": "finding",
    "problems": "finding",
    "node": "node",
    "nodes": "node",
    "day": "day",
    "days": "day",
}
CHANGE_WORDS = {
    "new": "NEW",
    "worsened": "WORSENED",
    "worse": "WORSENED",
    "resolved": "RESOLVED",
    "fixed": "RESOLVED",
    "ongoing": "ONGOING",
}
SEVERITY_WORDS = {s.name: s for s in Severity}

CATEGORIES: dict[str, tuple[re.Pattern[str], frozenset[Rule]]] = {
    "unreachable": (
        re.compile(
            r"unreachable|could not be (?:checked|reached|contacted)|timed out|time-?out|auth(?:entication)? "
            r"(?:error|failure)|malformed|HTTP \d{3}",
            re.IGNORECASE,
        ),
        frozenset({Rule.UNREACHABLE}),
    ),
    "lifecycle": (
        re.compile(
            r"end[- ]of[- ](?:life|(?:standard )?support)|\bEOL\b|unsupported|extended support|out of support|"
            r"support ends|past support",
            re.IGNORECASE,
        ),
        frozenset({Rule.CP_EOL, Rule.NODE_EOL, Rule.CP_EOL_SOON, Rule.CP_UNSUPPORTED}),
    ),
    "skew": (
        re.compile(r"\bskew|too far behind|minors? behind the control plane", re.IGNORECASE),
        frozenset({Rule.NODE_SKEW_EXCEEDED}),
    ),
    "newer": (
        re.compile(r"newer than (?:the |its )?control plane|ahead of (?:the |its )?control plane", re.IGNORECASE),
        frozenset({Rule.NODE_NEWER_THAN_CP}),
    ),
    "mixed": (
        re.compile(r"\bmixed\b|mid-upgrade|stuck upgrade|partially upgraded", re.IGNORECASE),
        frozenset({Rule.NODE_POOL_MIXED}),
    ),
    "patch": (
        re.compile(r"patch(?:es)? behind|behind on patch|latest patch|patch level", re.IGNORECASE),
        frozenset({Rule.CP_PATCH_BEHIND}),
    ),
    "unknown": (
        re.compile(
            r"unknown (?:minor |control[- ]plane )?version|not in the (?:support(?:ed)?[- ]version )?matrix|"
            r"unrecogni[sz]ed version",
            re.IGNORECASE,
        ),
        frozenset({Rule.CP_UNKNOWN_VERSION}),
    ),
    "unparseable": (
        re.compile(r"unparse?able|could not be parsed|invalid version", re.IGNORECASE),
        frozenset({Rule.UNPARSEABLE_VERSION}),
    ),
}


def _lines(text: str) -> list[list[str]]:
    """Each line split into sentences and clauses. A part without a cluster name inherits the clusters named
    earlier on the same line ("payments-prod-11 is on 1.28.15; its pool has 4 nodes")."""
    out: list[list[str]] = []
    for line in text.splitlines():
        parts = [p for p in re.split(r"(?<=[.!?;])\s+(?=[A-Za-z*_•\-])|;\s+", line) if p.strip()]
        if parts:
            out.append(parts)
    return out


def _mask(text: str, start: int, end: int) -> str:
    return text[:start] + MASK * (end - start) + text[end:]


# ------------------------------------------------------------------------------- gate


def check(
    text: str,
    sheet: FactSheet,
    *,
    max_length: int | None,
    deny_list: Sequence[str] = (),
    require_clusters: bool = True,
) -> GateResult:
    errors: list[GateError] = []
    stats = {"clusters": 0, "versions": 0, "dates": 0, "numbers": 0}

    if not text.strip():
        return GateResult((GateError("EMPTY", "the text is empty"),), stats)
    if max_length is not None and len(text) > max_length:
        errors.append(GateError("TOO_LONG", f"{len(text)} characters; the limit is {max_length}"))
    for pattern in deny_list:
        if re.search(pattern, text):
            errors.append(GateError("DENY_LIST", f"contains text matching a forbidden pattern ({pattern})"))

    named: set[str] = set()
    for parts in _lines(text):
        context: list[str] = []
        for part in parts:
            seg_errors, found = _check_segment(part, sheet, context, stats)
            errors += seg_errors
            named.update(found)
            if found:
                context = found

    if require_clusters:
        for name in sorted(sheet.required_clusters() - named):
            errors.append(
                GateError(
                    "MISSING_REQUIRED_CLUSTER",
                    f"{name} has a CRITICAL finding or is unreachable in prod and must be named",
                )
            )
    return GateResult(tuple(_dedupe(errors)), stats)


def _dedupe(errors: list[GateError]) -> list[GateError]:
    seen, out = set(), []
    for e in errors:
        if (e.code, e.detail) not in seen:
            seen.add((e.code, e.detail))
            out.append(e)
    return out


def _check_segment(
    seg: str, sheet: FactSheet, inherited: list[str], stats: dict[str, int]
) -> tuple[list[GateError], list[str]]:
    errors: list[GateError] = []
    work = seg

    # 1. literals
    for lit in sheet.literals:
        if lit:
            work = work.replace(lit, MASK * len(lit))

    # 2. dates
    dates: list[date] = []
    for m in list(ISO_DATE.finditer(work)):
        try:
            dates.append(date(int(m[1]), int(m[2]), int(m[3])))
        except ValueError:
            errors.append(GateError("DATE_NOT_IN_FACTS", f"{m[0]} is not a valid date"))
        work = _mask(work, m.start(), m.end())
    for m in list(MONTH_DATE.finditer(work)):
        day_s = m[1] or m[3]
        if not day_s:
            continue  # a bare month name ("in October") is not a date claim
        year = int(m[4]) if m[4] else sheet.facts.run_date.year
        try:
            dates.append(date(year, MONTHS[m[2][:3].lower()], int(day_s)))
        except ValueError:
            errors.append(GateError("DATE_NOT_IN_FACTS", f"{m[0].strip()} is not a valid date"))
        work = _mask(work, m.start(), m.end())

    # 3. hyphenated names
    found: list[str] = []
    for m in list(HYPHEN_NAME.finditer(work)):
        token = m[0]
        if token in sheet.inventory_names:
            found.append(token)
            if token not in sheet.clusters:
                errors.append(
                    GateError(
                        "CLUSTER_WITHOUT_FINDINGS", f"{token} has no findings tonight and should not be mentioned"
                    )
                )
        elif token in sheet.pools:
            pass
        elif CLUSTER_LIKE.search(token):
            errors.append(GateError("UNKNOWN_CLUSTER", f"{token} is not a cluster in the inventory"))
        work = _mask(work, m.start(), m.end())
    stats["clusters"] += len(found)
    context = found or inherited
    cf = [sheet.clusters[c] for c in context if c in sheet.clusters]

    # 4. rule IDs and internal IDs
    for m in list(RULE_ID.finditer(work)):
        work = _mask(work, m.start(), m.end())
    for m in list(ALIAS.finditer(work)):
        errors.append(GateError("INTERNAL_ID", f"internal finding ID {m[0]} must not appear in the message"))
        work = _mask(work, m.start(), m.end())
    for cid in sheet.inventory_ids:
        for m in list(re.finditer(rf"(?<![\w-]){re.escape(cid)}(?![\w-])", work)):
            errors.append(GateError("INTERNAL_ID", f"cluster ID {cid} used; use the cluster name"))
            work = _mask(work, m.start(), m.end())

    # 5. versions
    versions: list[str] = []
    for m in list(VERSION_RX.finditer(work)):
        versions.append(m.group(1))
        work = _mask(work, m.start(), m.end())
    stats["versions"] += len(versions)
    stats["dates"] += len(dates)

    for v in versions:
        if v not in sheet.versions:
            errors.append(GateError("VERSION_NOT_IN_FACTS", f"version {v} does not appear in the findings"))
    for d in dates:
        if d not in sheet.dates:
            errors.append(GateError("DATE_NOT_IN_FACTS", f"date {d.isoformat()} does not appear in the findings"))

    # context: values next to named clusters must belong to those clusters
    who = ", ".join(context)
    if cf:
        own_versions = set().union(*(c.versions for c in cf))
        own_dates = set().union(*(c.dates for c in cf)) | {sheet.facts.run_date}
        for v in versions:
            if v in sheet.versions and v not in own_versions:
                errors.append(
                    GateError(
                        "CONTEXT_MISMATCH", f"version {v} is stated next to {who}, but it belongs to other clusters"
                    )
                )
        for d in dates:
            if d in sheet.dates and d not in own_dates:
                errors.append(
                    GateError(
                        "CONTEXT_MISMATCH",
                        f"date {d.isoformat()} is stated next to {who}, but it belongs to other clusters",
                    )
                )
        errors += _rule_context(seg, context, sheet)

    # 6. counts and numbers
    work = LIST_MARKER.sub(lambda m: m.group(1) + MASK * len(m.group(2)) + MASK, work)
    for m in list(RATIO.finditer(work)):
        for n in (int(m[1]), int(m[2])):
            stats["numbers"] += 1
            if n not in sheet.counts["cluster:checked"]:
                errors.append(GateError("COUNT_MISMATCH", f"'{m[0]}' does not match the clusters checked"))
        work = _mask(work, m.start(), m.end())
    own_nodes = set().union(*(c.nodes for c in cf)) if cf else set()
    if len(cf) > 1:
        own_nodes.add(sum(max(c.nodes, default=0) for c in cf))
    own_days = set().union(*(c.days for c in cf)) if cf else set()
    for m in list(NUMBER.finditer(work)):
        raw = m.group(1)
        is_word = not raw.isdigit()
        n = NUMBER_WORDS[raw.lower()] if is_word else int(raw)
        kind = _classify(work[m.end() :])
        if is_word and kind is None:
            continue  # "one of", "no one": words only count when they quantify something
        stats["numbers"] += 1
        if kind == "node" and cf:
            allowed: frozenset[int] | set[int] = own_nodes
        elif kind == "day" and cf:
            allowed = own_days
        else:
            allowed = sheet.counts.get(kind or "any", frozenset())
        if n not in allowed:
            what = f"'{raw}{_window(work[m.end() :])}'"
            where = f" for {who}" if kind in ("node", "day") and cf else ""
            code = "COUNT_MISMATCH" if kind else "NUMBER_NOT_IN_FACTS"
            errors.append(GateError(code, f"{what}{where} is not supported by the findings"))
    return errors, found


_STOP = re.compile(r"[;,.:()\[\]\n\d" + MASK + "]")


def _window(after: str) -> str:
    return _STOP.split(after, maxsplit=1)[0][:60].rstrip()


def _classify(after: str) -> str | None:
    """What a number counts, from the words right after it (up to punctuation or the next value).

    ``3 CRITICAL findings`` -> severity:CRITICAL · ``4 clusters could not be checked`` -> cluster:unreachable ·
    ``2 new clusters`` -> change:NEW_CLUSTER · ``5 prod clusters`` -> cluster:prod · ``2 nodes`` -> node
    """
    window = _window(after)
    words = re.findall(r"[A-Za-z]+", window)[:6]
    lw = [w.lower() for w in words]
    for w in words:
        if w in SEVERITY_WORDS:
            return f"severity:{w}"
    change = next((CHANGE_WORDS[w] for w in lw[:2] if w in CHANGE_WORDS), None)
    noun = next((NOUNS[w] for w in lw if w in NOUNS), None)
    if change and noun in (None, "finding", "cluster"):
        return "change:NEW_CLUSTER" if change == "NEW" and noun == "cluster" else f"change:{change}"
    if noun != "cluster":
        return noun
    text = " ".join(lw)
    if "unreachable" in lw or re.search(r"could not be (?:checked|reached|contacted)|not reachable", text):
        return "cluster:unreachable"
    if "compliant" in lw:
        return "cluster:compliant"
    if "checked" in lw:
        return "cluster:checked"
    env = next((w for w in lw[:3] if w in ("prod", "production", "staging", "dev")), None)
    if env:
        return f"cluster:{'prod' if env == 'production' else env}"
    return "cluster"


def _rule_context(seg: str, clusters: list[str], sheet: FactSheet) -> list[GateError]:
    present = {name: rules for name, (rx, rules) in CATEGORIES.items() if rx.search(seg)}
    errors: list[GateError] = []
    known = [c for c in clusters if c in sheet.clusters]
    resolved_ctx = bool(re.search(r"\bresolved\b|\bfixed\b|\bno longer\b|\bcleared\b", seg, re.IGNORECASE))
    for cat, rules in present.items():
        if not any(sheet.clusters[c].rules & rules for c in known):
            errors.append(
                GateError(
                    "CONTEXT_MISMATCH", f"the text says '{cat}' about {', '.join(known)}, which has no such finding"
                )
            )
    if present and not resolved_ctx:
        allowed = frozenset().union(*present.values())
        for c in known:
            if not sheet.clusters[c].rules & allowed:
                errors.append(
                    GateError(
                        "CONTEXT_MISMATCH",
                        f"{c} is described as {', '.join(present)}, which does not match its findings",
                    )
                )
    for word, sev in SEVERITY_WORDS.items():
        if re.search(rf"\b{word}\b", seg) and not any(sev in sheet.clusters[c].severities for c in known):
            errors.append(
                GateError("CONTEXT_MISMATCH", f"{', '.join(known)} is labelled {word}, but has no {word} finding")
            )
    return errors
