"""Posting to the team channel (FR-25..FR-27).

Every message is written to the local outbox, whatever the mode. In POST mode it is also sent to the
webhook, with retries; if every attempt fails the run is POST_FAILED and the outbox copy is the record.
The webhook URL is a secret (CF-3): read from the environment, never logged.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from drift.config.models import ChannelConfig, OutputConfig

PostStatus = Literal["DRY_RUN", "POSTED", "POST_FAILED", "ALREADY_POSTED", "SILENT"]


@dataclass(frozen=True)
class PostResult:
    status: PostStatus
    outbox_path: str
    attempts: tuple[dict[str, object], ...] = field(default_factory=tuple)

    @property
    def delivered(self) -> bool:
        return self.status == "POSTED"


def write_outbox(outbox_dir: Path, run_date: str, run_id: str, text: str, meta: dict[str, object]) -> Path:
    d = outbox_dir / run_date
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{run_id}.txt"
    path.write_text(text + "\n")
    (d / f"{run_id}.json").write_text(json.dumps(meta, indent=2, default=str) + "\n")
    return path


class ChannelPublisher:
    def __init__(
        self,
        channel: ChannelConfig,
        output: OutputConfig,
        environ: Mapping[str, str],
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self._channel = channel
        self._output = output
        self._url = environ.get(channel.webhook_url_env, "")
        self._transport_factory = transport_factory

    async def post(self, text: str) -> tuple[bool, tuple[dict[str, object], ...]]:
        """Send with retries. Returns (delivered, attempts). Attempt records never include the URL."""
        if not self._url:
            return False, ({"attempt": 1, "outcome": "webhook URL env var not set"},)
        attempts: list[dict[str, object]] = []
        transport = self._transport_factory() if self._transport_factory else None
        async with httpx.AsyncClient(timeout=self._output.post_timeout_s, transport=transport) as http:
            for attempt in range(1, self._output.post_retries + 2):
                try:
                    resp = await http.post(self._url, json={"text": text})
                    ok = 200 <= resp.status_code < 300
                    attempts.append({"attempt": attempt, "outcome": "ok" if ok else f"HTTP {resp.status_code}"})
                    if ok:
                        return True, tuple(attempts)
                except httpx.HTTPError as exc:
                    attempts.append({"attempt": attempt, "outcome": type(exc).__name__})
                if attempt <= self._output.post_retries and self._output.post_retry_backoff_s:
                    await asyncio.sleep(self._output.post_retry_backoff_s * attempt)
        return False, tuple(attempts)
