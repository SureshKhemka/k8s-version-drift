"""Fault injection (MS-5) and call recording (MS-4) shared by both mock services."""

from __future__ import annotations

import hashlib
import json
import random
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Mode = Literal["none", "timeout", "auth_error", "malformed", "http_error"]


class FaultSpec(BaseModel):
    """What a mock does to a request. ``script`` entries are consumed one per request, then ``mode`` applies.

    Script entries: ``ok``, ``timeout``, ``auth_error``, ``malformed``, ``http_error`` or ``http_error:<status>``.
    """

    model_config = ConfigDict(extra="forbid")
    mode: Mode = "none"
    status: int = 500
    latency_ms: int = Field(default=0, ge=0)
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    seed: int = 0
    script: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class Decision:
    mode: Mode
    status: int
    latency_s: float


class FaultState:
    """Deterministic per-key fault decisions: script counters and seeded RNGs, cleared on reset."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consumed: dict[str, int] = {}
        self._rngs: dict[str, random.Random] = {}

    def reset(self) -> None:
        with self._lock:
            self._consumed.clear()
            self._rngs.clear()

    def decide(self, key: str, spec: FaultSpec | None) -> Decision:
        if spec is None:
            return Decision("none", 200, 0.0)
        latency = spec.latency_ms / 1000
        with self._lock:
            used = self._consumed.get(key, 0)
            if used < len(spec.script):
                self._consumed[key] = used + 1
                entry = spec.script[used]
                if entry == "ok":
                    return Decision("none", 200, latency)
                mode, _, status = entry.partition(":")
                return Decision(mode, int(status) if status else spec.status, latency)  # type: ignore[arg-type]
            if spec.error_rate > 0:
                digest = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)
                rng = self._rngs.setdefault(key, random.Random(spec.seed + digest))
                if rng.random() < spec.error_rate:
                    return Decision("http_error", 500, latency)
        return Decision(spec.mode, spec.status, latency)


@dataclass
class CallLog:
    """Every request a mock receives, in memory and appended to a JSONL file. Headers are never stored."""

    path: Path | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, **entry: Any) -> None:
        entry = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), **entry}
        with self._lock:
            self.calls.append(entry)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a") as fh:
                    fh.write(json.dumps(entry) + "\n")

    def clear(self) -> None:
        with self._lock:
            self.calls.clear()
            if self.path is not None and self.path.exists():
                self.path.unlink()
