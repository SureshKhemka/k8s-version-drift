"""Record/replay of LLM calls (EV-15).

A cassette entry is keyed by everything that determines the model's answer: provider, model, temperature,
max tokens, the full message list and the schema. Replaying the same inputs returns the recorded output, so
code and gate changes can be evaluated without calling a model. Changing a prompt changes the key, which
shows up as a cassette miss rather than a silently stale answer.

Modes:
  live    call the model, keep nothing
  record  call the model, store every answer (overwrites)
  replay  never call the model; a missing entry raises CassetteMiss
  auto    replay when recorded, otherwise call the model and record
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, Protocol

from drift.config.models import StepModelConfig
from drift.llm.client import LLMError, LLMResponse, Message

CassetteMode = Literal["live", "record", "replay", "auto"]


class CassetteMiss(LLMError):
    """Replay mode found no recording for this exact request."""


class _Completer(Protocol):
    step: StepModelConfig

    async def complete(self, messages: list[Message], schema: dict[str, Any] | None = None) -> LLMResponse: ...


def request_key(step: StepModelConfig, messages: list[Message], schema: dict[str, Any] | None) -> str:
    payload = {
        "provider": step.provider,
        "model": step.model,
        "temperature": step.temperature,
        "max_tokens": step.max_tokens,
        "messages": [[m.role, m.content] for m in messages],
        "schema": schema,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class CassetteClient:
    """Wraps an LLM client; same interface (``step`` + ``complete``)."""

    def __init__(self, inner: _Completer, directory: Path, mode: CassetteMode) -> None:
        self.inner = inner
        self.step = inner.step
        self.dir = directory
        self.mode = mode
        self.hits = 0
        self.misses = 0

    def _path(self, key: str) -> Path:
        return self.dir / key[:2] / f"{key}.json"

    async def complete(self, messages: list[Message], schema: dict[str, Any] | None = None) -> LLMResponse:
        key = request_key(self.step, messages, schema)
        path = self._path(key)
        if self.mode in ("replay", "auto") and path.is_file():
            self.hits += 1
            return LLMResponse(**json.loads(path.read_text())["response"])
        if self.mode == "replay":
            self.misses += 1
            raise CassetteMiss(f"no recording for {self.step.model} request {key[:12]}")
        resp = await self.inner.complete(messages, schema)
        if self.mode in ("record", "auto"):
            path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "key": key,
                "request": {
                    "provider": self.step.provider,
                    "model": self.step.model,
                    "temperature": self.step.temperature,
                    "max_tokens": self.step.max_tokens,
                    "messages": [{"role": m.role, "content": m.content} for m in messages],
                    "schema": schema,
                },
                "response": asdict(resp),
            }
            path.write_text(json.dumps(entry, indent=1, ensure_ascii=False) + "\n")
        return resp
