"""Command-line entry point: ``drift <command>``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from drift.config import AppConfig, ConfigError, load_config, masked_dump, validate_runtime
from drift.domain.enums import ExitCode

app = typer.Typer(no_args_is_help=True, add_completion=False)
config_app = typer.Typer(no_args_is_help=True, help="Inspect and validate configuration.")
app.add_typer(config_app, name="config")

ConfigDirOpt = Annotated[Path | None, typer.Option("--config-dir", help="Defaults to $DRIFT_CONFIG_DIR or ./config")]
EnvOpt = Annotated[str | None, typer.Option("--env", help="Defaults to $DRIFT_ENV or 'local'")]


def _load(config_dir: Path | None, env: str | None, runtime: bool = True) -> AppConfig:
    try:
        cfg = load_config(config_dir, env)
        if runtime:
            validate_runtime(cfg)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(ExitCode.CONFIG_ERROR) from exc
    return cfg


@config_app.command("show")
def config_show(config_dir: ConfigDirOpt = None, env: EnvOpt = None) -> None:
    """Print the effective configuration with endpoints and secrets masked."""
    cfg = _load(config_dir, env, runtime=False)
    typer.echo(json.dumps(masked_dump(cfg), indent=2))


@config_app.command("check")
def config_check(config_dir: ConfigDirOpt = None, env: EnvOpt = None) -> None:
    """Validate configuration, including files and env vars it refers to."""
    _load(config_dir, env)
    typer.echo("configuration OK")


# ---------------------------------------------------------------------------------------------- mocks

mocks_app = typer.Typer(no_args_is_help=True, help="Run and control the local mock services.")
app.add_typer(mocks_app, name="mocks")


def _admin(cfg: AppConfig, service: str, method: str, path: str, json_body: object = None) -> object:
    import httpx

    svc = cfg.mocks.k8s if service == "k8s" else cfg.mocks.channel
    url = f"http://{svc.host}:{svc.port}{path}"
    try:
        resp = httpx.request(method, url, json=json_body, timeout=10)
    except httpx.ConnectError as exc:
        typer.echo(f"mock {service} is not running on {svc.host}:{svc.port} (start it with `drift mocks up`)", err=True)
        raise typer.Exit(1) from exc
    if resp.status_code >= 400:
        typer.echo(f"mock {service} {method} {path}: HTTP {resp.status_code} {resp.text}", err=True)
        raise typer.Exit(1)
    return resp.json()


@mocks_app.command("up")
def mocks_up(
    service: Annotated[list[str] | None, typer.Option("--service", help="k8s and/or channel; default both")] = None,
    config_dir: ConfigDirOpt = None,
    env: EnvOpt = None,
) -> None:
    """Start the mock cluster API and mock channel (foreground)."""
    from drift_mocks.server import build_servers, serve

    cfg = _load(config_dir, env, runtime=False)
    services = service or ["k8s", "channel"]
    servers = build_servers(
        services,  # type: ignore[arg-type]
        seed_dir=cfg.mocks.seed_dir,
        state_dir=cfg.mocks.state_dir,
        faults=cfg.mocks.faults,
        timeout_sleep_s=cfg.mocks.timeout_sleep_s,
        k8s_addr=(cfg.mocks.k8s.host, cfg.mocks.k8s.port),
        channel_addr=(cfg.mocks.channel.host, cfg.mocks.channel.port),
    )
    for name in services:
        svc = cfg.mocks.k8s if name == "k8s" else cfg.mocks.channel
        typer.echo(f"mock {name} listening on {svc.host}:{svc.port}")
    serve(servers)


@mocks_app.command("reset")
def mocks_reset(config_dir: ConfigDirOpt = None, env: EnvOpt = None) -> None:
    """Reset both mocks to seed state (night 0), clearing calls, messages and fault counters."""
    cfg = _load(config_dir, env, runtime=False)
    typer.echo(json.dumps(_admin(cfg, "k8s", "POST", "/_admin/reset")))
    _admin(cfg, "channel", "POST", "/_admin/reset")


@mocks_app.command("night")
def mocks_night(night: int, config_dir: ConfigDirOpt = None, env: EnvOpt = None) -> None:
    """Time-travel the scenario to NIGHT (MS-6). Rewrites the effective inventory."""
    cfg = _load(config_dir, env, runtime=False)
    typer.echo(json.dumps(_admin(cfg, "k8s", "POST", f"/_admin/night/{night}")))


@mocks_app.command("status")
def mocks_status(config_dir: ConfigDirOpt = None, env: EnvOpt = None) -> None:
    """Show the current scenario night and date."""
    cfg = _load(config_dir, env, runtime=False)
    typer.echo(json.dumps(_admin(cfg, "k8s", "GET", "/_admin/state")))


@mocks_app.command("calls")
def mocks_calls(
    service: Annotated[str, typer.Option(help="k8s or channel")] = "k8s",
    writes_only: Annotated[bool, typer.Option("--writes-only", help="Only non-GET calls")] = False,
    config_dir: ConfigDirOpt = None,
    env: EnvOpt = None,
) -> None:
    """Print recorded calls as JSON lines."""
    cfg = _load(config_dir, env, runtime=False)
    calls = _admin(cfg, service, "GET", "/_admin/calls")
    assert isinstance(calls, list)
    for call in calls:
        if not writes_only or call.get("method") != "GET":
            typer.echo(json.dumps(call))


@mocks_app.command("messages")
def mocks_messages(config_dir: ConfigDirOpt = None, env: EnvOpt = None) -> None:
    """Print messages received by the mock channel."""
    cfg = _load(config_dir, env, runtime=False)
    messages = _admin(cfg, "channel", "GET", "/_admin/messages")
    assert isinstance(messages, list)
    for m in messages:
        typer.echo(f"--- {m['ts']}\n{m['text']}\n")
