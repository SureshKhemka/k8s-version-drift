"""Mock Kubernetes API server for many clusters (MS-1).

Routes, per cluster ``{cid}`` (base URL ``http://host:port/clusters/{cid}``):
  GET /clusters/{cid}/version
  GET /clusters/{cid}/api/v1/nodes?limit=&continue=
  anything else under /clusters/{cid}/  -> recorded, 405 (writes) or 404

Admin (not part of the Kubernetes surface):
  GET  /_admin/state            current night/date/cluster count
  POST /_admin/reset            back to seed night 0, clear calls and fault counters
  POST /_admin/night/{n}        time-travel to night n (MS-6)
  GET  /_admin/calls            recorded calls; DELETE clears them
  PUT  /_admin/faults           replace runtime fault overrides {"clusters": {cid: FaultSpec}}
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from drift_mocks.faults import CallLog, FaultSpec, FaultState
from drift_mocks.world import World

DEFAULT_PAGE = 100


class K8sMockState:
    def __init__(self, seed_dir: Path, state_dir: Path, faults_file: Path | None, timeout_sleep_s: float) -> None:
        self.seed_dir = seed_dir
        self.state_dir = state_dir
        self.faults_file = faults_file
        self.timeout_sleep_s = timeout_sleep_s
        self.calls = CallLog(state_dir / "k8s_calls.jsonl")
        self.fault_state = FaultState()
        self.overrides: dict[str, FaultSpec] = {}
        self.world = World.load(seed_dir, self._saved_night())
        self._load_fault_file()
        self.world.write_inventory(self.inventory_path)

    @property
    def inventory_path(self) -> Path:
        return self.state_dir / "inventory.yaml"

    def _saved_night(self) -> int:
        p = self.state_dir / "night"
        return int(p.read_text().strip()) if p.is_file() else 0

    def _load_fault_file(self) -> None:
        self.overrides = {}
        if self.faults_file and self.faults_file.is_file():
            data = yaml.safe_load(self.faults_file.read_text()) or {}
            for cid, spec in ((data.get("k8s") or {}).get("clusters") or {}).items():
                self.overrides[cid] = FaultSpec.model_validate(spec)

    def set_night(self, night: int) -> None:
        self.world = World.load(self.seed_dir, night)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "night").write_text(str(night))
        self.fault_state.reset()
        self.world.write_inventory(self.inventory_path)

    def reset(self) -> None:
        self.calls.clear()
        self._load_fault_file()
        self.set_night(0)

    def fault_for(self, cid: str) -> FaultSpec | None:
        if cid in self.overrides:
            return self.overrides[cid]
        raw = self.world.clusters[cid].get("fault")
        return FaultSpec.model_validate(raw) if raw else None


def create_app(state: K8sMockState) -> FastAPI:
    app = FastAPI(title="mock-k8s", docs_url=None, redoc_url=None)
    app.state.mock = state

    # ------------------------------------------------------------------ admin

    @app.get("/_admin/state")
    def admin_state() -> dict[str, Any]:
        return {"night": state.world.night, "date": state.world.date, "clusters": len(state.world.clusters)}

    @app.post("/_admin/reset")
    def admin_reset() -> dict[str, Any]:
        state.reset()
        return admin_state()

    @app.post("/_admin/night/{night}")
    def admin_night(night: int) -> Response:
        try:
            state.set_night(night)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(admin_state())

    @app.get("/_admin/calls")
    def admin_calls() -> list[dict[str, Any]]:
        return state.calls.calls

    @app.delete("/_admin/calls")
    def admin_clear_calls() -> dict[str, str]:
        state.calls.clear()
        return {"status": "cleared"}

    @app.put("/_admin/faults")
    async def admin_faults(request: Request) -> dict[str, Any]:
        body = await request.json()
        state.overrides = {cid: FaultSpec.model_validate(s) for cid, s in (body.get("clusters") or {}).items()}
        state.fault_state.reset()
        return {"clusters": sorted(state.overrides)}

    # -------------------------------------------------------- cluster surface

    @app.api_route("/clusters/{cid}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def cluster(cid: str, path: str, request: Request) -> Response:
        method = request.method
        query = dict(request.query_params)

        def done(resp: Response, fault: str = "none") -> Response:
            state.calls.record(
                service="k8s",
                cluster=cid,
                method=method,
                path=f"/{path}",
                query=query,
                status=resp.status_code,
                fault=fault,
            )
            return resp

        if cid not in state.world.clusters:
            return done(_status(404, "NotFound", f"cluster {cid} not found"))
        if method != "GET":
            return done(_status(405, "MethodNotAllowed", "mock clusters are read-only"))

        decision = state.fault_state.decide(cid, state.fault_for(cid))
        if decision.latency_s:
            await asyncio.sleep(decision.latency_s)
        if decision.mode == "timeout":
            await asyncio.sleep(state.timeout_sleep_s)
            return done(_status(504, "Timeout", "gateway timeout"), "timeout")
        if decision.mode == "auth_error":
            return done(_status(401, "Unauthorized", "Unauthorized"), "auth_error")
        if decision.mode == "http_error":
            return done(_status(decision.status, "InternalError", "internal error"), "http_error")
        if decision.mode == "malformed":
            return done(PlainTextResponse("<html><body>502 Bad Gateway</body></html>", 200), "malformed")

        if path == "version":
            return done(JSONResponse(state.world.version_payload(cid)))
        if path == "api/v1/nodes":
            return done(_node_list(state.world.node_items(cid), query))
        return done(_status(404, "NotFound", f"the server could not find the requested resource ({path})"))

    return app


def _status(code: int, reason: str, message: str) -> JSONResponse:
    return JSONResponse(
        {
            "kind": "Status",
            "apiVersion": "v1",
            "metadata": {},
            "status": "Failure",
            "message": message,
            "reason": reason,
            "code": code,
        },
        status_code=code,
    )


def _node_list(items: list[dict[str, Any]], query: dict[str, str]) -> JSONResponse:
    """NodeList with Kubernetes-style chunking (``limit`` / ``continue``)."""
    try:
        limit = int(query.get("limit", "0")) or len(items) or DEFAULT_PAGE
    except ValueError:
        return _status(400, "BadRequest", "invalid limit")
    start = 0
    if token := query.get("continue"):
        try:
            start = json.loads(base64.urlsafe_b64decode(token.encode()))["start"]
        except (ValueError, KeyError):
            return _status(410, "Expired", "invalid continue token")
    page = items[start : start + limit]
    nxt = start + limit
    meta: dict[str, Any] = {"resourceVersion": "1"}
    if nxt < len(items):
        meta["continue"] = base64.urlsafe_b64encode(json.dumps({"start": nxt}).encode()).decode()
        meta["remainingItemCount"] = len(items) - nxt
    return JSONResponse({"kind": "NodeList", "apiVersion": "v1", "metadata": meta, "items": page})
