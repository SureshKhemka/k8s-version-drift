"""Mock team channel that behaves like a Slack incoming webhook (MS-3).

  POST /webhook/{token}   body {"text": "..."} -> 200 "ok" and the message is stored
Admin:
  GET/DELETE /_admin/messages, GET/DELETE /_admin/calls, PUT /_admin/faults (FaultSpec), POST /_admin/reset
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

from drift_mocks.faults import CallLog, FaultSpec, FaultState


class ChannelMockState:
    def __init__(self, state_dir: Path, faults_file: Path | None, timeout_sleep_s: float) -> None:
        self.state_dir = state_dir
        self.faults_file = faults_file
        self.timeout_sleep_s = timeout_sleep_s
        self.calls = CallLog(state_dir / "channel_calls.jsonl")
        self.messages_path = state_dir / "channel_messages.jsonl"
        self.fault_state = FaultState()
        self.fault: FaultSpec | None = None
        self._load_fault_file()

    def _load_fault_file(self) -> None:
        self.fault = None
        if self.faults_file and self.faults_file.is_file():
            data = (yaml.safe_load(self.faults_file.read_text()) or {}).get("channel")
            self.fault = FaultSpec.model_validate(data) if data else None

    def messages(self) -> list[dict[str, Any]]:
        if not self.messages_path.is_file():
            return []
        return [json.loads(line) for line in self.messages_path.read_text().splitlines() if line.strip()]

    def store(self, entry: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.messages_path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def clear_messages(self) -> None:
        if self.messages_path.exists():
            self.messages_path.unlink()

    def reset(self) -> None:
        self.clear_messages()
        self.calls.clear()
        self.fault_state.reset()
        self._load_fault_file()


def create_app(state: ChannelMockState) -> FastAPI:
    app = FastAPI(title="mock-channel", docs_url=None, redoc_url=None)
    app.state.mock = state

    @app.post("/webhook/{token}")
    async def webhook(token: str, request: Request) -> Response:
        body = await request.body()

        def done(resp: Response, fault: str = "none") -> Response:
            # The token is part of the secret webhook URL: never recorded.
            state.calls.record(
                service="channel",
                method="POST",
                path="/webhook/***",
                status=resp.status_code,
                fault=fault,
                bytes=len(body),
            )
            return resp

        decision = state.fault_state.decide("channel", state.fault)
        if decision.latency_s:
            await asyncio.sleep(decision.latency_s)
        if decision.mode == "timeout":
            await asyncio.sleep(state.timeout_sleep_s)
            return done(PlainTextResponse("timeout", 504), "timeout")
        if decision.mode in ("http_error", "malformed"):
            return done(PlainTextResponse("internal_error", decision.status), decision.mode)
        if decision.mode == "auth_error":
            return done(PlainTextResponse("invalid_token", 403), "auth_error")

        try:
            payload = json.loads(body)
            text = payload["text"]
            if not isinstance(text, str) or not text:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return done(PlainTextResponse("invalid_payload", 400))
        state.store({"ts": _now(), "text": text, "payload": payload})
        return done(PlainTextResponse("ok", 200))

    @app.get("/_admin/messages")
    def messages() -> list[dict[str, Any]]:
        return state.messages()

    @app.delete("/_admin/messages")
    def clear_messages() -> dict[str, str]:
        state.clear_messages()
        return {"status": "cleared"}

    @app.get("/_admin/calls")
    def calls() -> list[dict[str, Any]]:
        return state.calls.calls

    @app.delete("/_admin/calls")
    def clear_calls() -> dict[str, str]:
        state.calls.clear()
        return {"status": "cleared"}

    @app.put("/_admin/faults")
    async def faults(request: Request) -> dict[str, Any]:
        body = await request.json()
        state.fault = FaultSpec.model_validate(body) if body else None
        state.fault_state.reset()
        return {"fault": state.fault.model_dump() if state.fault else None}

    @app.post("/_admin/reset")
    def reset() -> dict[str, str]:
        state.reset()
        return {"status": "reset"}

    return app


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")
