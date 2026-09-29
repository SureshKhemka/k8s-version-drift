"""Run the mock services as network servers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

import uvicorn

from drift_mocks.channel import ChannelMockState
from drift_mocks.channel import create_app as channel_app
from drift_mocks.k8s_api import K8sMockState
from drift_mocks.k8s_api import create_app as k8s_app

Service = Literal["k8s", "channel"]


def build_servers(
    services: list[Service],
    *,
    seed_dir: Path,
    state_dir: Path,
    faults: Path,
    timeout_sleep_s: float,
    k8s_addr: tuple[str, int],
    channel_addr: tuple[str, int],
    log_level: str = "warning",
) -> list[uvicorn.Server]:
    servers = []
    if "k8s" in services:
        state = K8sMockState(seed_dir, state_dir, faults, timeout_sleep_s)
        cfg = uvicorn.Config(k8s_app(state), host=k8s_addr[0], port=k8s_addr[1], log_level=log_level)
        servers.append(uvicorn.Server(cfg))
    if "channel" in services:
        cstate = ChannelMockState(state_dir, faults, timeout_sleep_s)
        cfg = uvicorn.Config(channel_app(cstate), host=channel_addr[0], port=channel_addr[1], log_level=log_level)
        servers.append(uvicorn.Server(cfg))
    return servers


def serve(servers: list[uvicorn.Server]) -> None:
    async def main() -> None:
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(main())
