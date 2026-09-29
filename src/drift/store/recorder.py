"""Run recorder: every event goes to SQLite and to a per-run JSONL file, redacted first (FR-30..FR-32)."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from drift.domain.clock import Clock
from drift.store.sqlite import Store

REDACTED = "[REDACTED]"

# Always-on patterns, independent of config: URLs, bearer tokens, JWT-like strings, IPv4 addresses.
_BUILTIN = (
    r"https?://[^\s\"'<>]+",
    r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+",
    r"\beyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+)*",
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b",
)


def new_run_id(run_date: date) -> str:
    return f"{run_date.isoformat()}-{secrets.token_hex(3)}"


@dataclass
class Redactor:
    """Removes secret values (by exact value) and endpoint/token-shaped strings (by pattern)."""

    secret_values: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    _compiled: list[re.Pattern[str]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._compiled = [re.compile(p) for p in (*_BUILTIN, *self.patterns)]

    @classmethod
    def from_env(
        cls, env_var_names: Iterable[str | None], environ: Mapping[str, str], patterns: Iterable[str]
    ) -> Redactor:
        values = tuple(v for n in env_var_names if n and (v := environ.get(n)) and len(v) >= 4)
        return cls(secret_values=values, patterns=tuple(patterns))

    def text(self, s: str) -> str:
        for v in self.secret_values:
            s = s.replace(v, REDACTED)
        for rx in self._compiled:
            s = rx.sub(REDACTED, s)
        return s


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set | frozenset):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, date):
        return obj.isoformat()
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _jsonable(getattr(obj, k)) for k in obj.__dataclass_fields__}
    if hasattr(obj, "value") and hasattr(obj, "name"):  # enums
        return obj.value if not isinstance(obj.value, int) else obj.name
    return obj


class Recorder:
    """Append-only structured run record. ``step_id`` values are stable so a tracer can map them later (FR-31)."""

    def __init__(self, store: Store, run_id: str, runs_dir: Path, clock: Clock, redactor: Redactor) -> None:
        self.store = store
        self.run_id = run_id
        self._clock = clock
        self._redactor = redactor
        runs_dir.mkdir(parents=True, exist_ok=True)
        self.path = runs_dir / f"{run_id}.jsonl"

    def event(self, step_id: str, kind: str, payload: Any) -> None:
        ts = self._clock.now().isoformat(timespec="milliseconds")
        body = self._redactor.text(json.dumps(_jsonable(payload), sort_keys=True, default=str))
        self.store.append_event(self.run_id, step_id, kind, ts, body)
        line = json.dumps({"run_id": self.run_id, "step_id": step_id, "kind": kind, "ts": ts})
        with self.path.open("a") as fh:
            fh.write(line[:-1] + f', "payload": {body}}}\n')
