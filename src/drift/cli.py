"""Command-line entry point: ``drift <command>``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from drift.config import AppConfig, ConfigError, load_config, masked_dump, validate_runtime
from drift.domain.enums import ExitCode, OutputMode

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


# ------------------------------------------------------------------------------------------------ run


def _start_logging(cfg: AppConfig) -> None:
    import os

    from drift.logs import setup
    from drift.store.recorder import Redactor

    names = [cfg.clusters.token_env, cfg.channel.webhook_url_env, *(p.api_key_env for p in cfg.llm.providers.values())]
    setup(cfg.logging.level, Redactor.from_env(names, os.environ, cfg.deny_list))


def _execute(cfg: AppConfig, run_date: str | None, cluster: list[str] | None, mode: str | None, force: bool) -> int:
    import asyncio
    import logging
    import os
    from datetime import date
    from zoneinfo import ZoneInfo

    from drift.chain.runner import PromptChain
    from drift.domain.clock import SystemClock
    from drift.domain.enums import OutputMode
    from drift.pipeline import run_once

    clock = SystemClock(ZoneInfo(cfg.schedule.timezone))
    try:
        day = date.fromisoformat(run_date) if run_date else None
    except ValueError:
        typer.echo(f"invalid --date {run_date!r}", err=True)
        return int(ExitCode.CONFIG_ERROR)
    logging.getLogger("drift").info("effective config: %s", json.dumps(masked_dump(cfg)))
    outcome = asyncio.run(
        run_once(
            cfg,
            clock=clock,
            run_date=day,
            environ=os.environ,
            clusters=cluster,
            force=force,
            mode=OutputMode(mode) if mode else None,
            summarizer=PromptChain(cfg, os.environ) if cfg.llm.enabled else None,
        )
    )
    for err in outcome.errors:
        typer.echo(err, err=True)
    if outcome.message is not None:
        typer.echo(outcome.message)
        typer.echo(
            f"\n[{outcome.status}] exit={int(outcome.exit_code)} run_id={outcome.run_id} "
            f"summary={outcome.summary_source} post={outcome.post.status if outcome.post else None}",
            err=True,
        )
    return int(outcome.exit_code)


@app.command("run")
def run_cmd(
    run_date: Annotated[
        str | None, typer.Option("--date", help="Run date YYYY-MM-DD (default: today in schedule tz)")
    ] = None,
    cluster: Annotated[
        list[str] | None, typer.Option("--cluster", "-c", help="Subset (always DRY_RUN); repeatable")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Force DRY_RUN")] = False,
    post: Annotated[bool, typer.Option("--post", help="Force POST")] = False,
    force: Annotated[bool, typer.Option("--force", help="Post even if this run date was already posted")] = False,
    config_dir: ConfigDirOpt = None,
    env: EnvOpt = None,
) -> None:
    """Run one night on demand (FR-28). Exit codes: 0 ok, 10 fallback, 20 partial, 30 post failed, 40 config."""
    if dry_run and post:
        typer.echo("--dry-run and --post are mutually exclusive", err=True)
        raise typer.Exit(ExitCode.CONFIG_ERROR)
    mode = "DRY_RUN" if dry_run else "POST" if post else None
    cfg = _load(config_dir, env, runtime=False)
    if mode:
        cfg = cfg.model_copy(update={"output": cfg.output.model_copy(update={"mode": OutputMode(mode)})})
    try:
        validate_runtime(cfg)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(ExitCode.CONFIG_ERROR) from exc
    _start_logging(cfg)
    raise typer.Exit(_execute(cfg, run_date, cluster, None, force))


@app.command("schedule")
def schedule_cmd(config_dir: ConfigDirOpt = None, env: EnvOpt = None) -> None:
    """Run nightly on the configured cron schedule and time zone (FR-28). Foreground; Ctrl-C to stop."""
    from apscheduler.schedulers.blocking import BlockingScheduler  # type: ignore[import-untyped]
    from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

    cfg = _load(config_dir, env)
    _start_logging(cfg)
    scheduler = BlockingScheduler(timezone=cfg.schedule.timezone)
    trigger = CronTrigger.from_crontab(cfg.schedule.cron, timezone=cfg.schedule.timezone)

    def job() -> None:
        # Re-read configuration each night so edits apply without a restart.
        fresh = _load(config_dir, env)
        code = _execute(fresh, None, None, None, False)
        typer.echo(f"scheduled run finished with exit code {code}", err=True)

    scheduler.add_job(job, trigger, id="nightly", max_instances=1, coalesce=True)
    from datetime import datetime
    from zoneinfo import ZoneInfo

    nxt = trigger.get_next_fire_time(None, datetime.now(ZoneInfo(cfg.schedule.timezone)))
    typer.echo(f"scheduled '{cfg.schedule.cron}' in {cfg.schedule.timezone}; next run {nxt}")
    scheduler.start()


# -------------------------------------------------------------------------------------------- collect


@app.command("collect")
def collect_cmd(
    cluster: Annotated[list[str] | None, typer.Option("--cluster", "-c", help="Cluster ID or name; repeatable")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print raw fetch results as JSON lines")] = False,
    config_dir: ConfigDirOpt = None,
    env: EnvOpt = None,
) -> None:
    """Fetch versions from clusters and print what was collected (no rules, no posting)."""
    import asyncio

    from drift.collect.collector import Collector
    from drift.collect.sources import SourceError, load_inventory, select
    from drift.domain.models import snapshot

    cfg = _load(config_dir, env, runtime=False)
    try:
        inventory = load_inventory(cfg.sources.inventory)
        clusters = select(inventory, cluster)
    except SourceError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(ExitCode.CONFIG_ERROR) from exc
    results = asyncio.run(Collector(cfg.clusters).collect(clusters))
    by_id = inventory.by_id()
    for r in results:
        if as_json:
            typer.echo(r.model_dump_json())
            continue
        c = by_id[r.cluster_id]
        if not r.reachable:
            typer.echo(f"{c.id}  {c.name:<28} UNREACHABLE  {r.reason}  (attempts={len(r.attempts)})")
            continue
        s = snapshot(c, r)
        cp = str(s.control_plane.parsed) if s.control_plane.ok else f"UNPARSEABLE({s.control_plane.raw})"
        pools = ", ".join(
            f"{p.name}:{'/'.join(sorted({str(v.parsed) if v.ok else v.raw for _, v in p.nodes}))}x{len(p.nodes)}"
            for p in s.pools
        )
        typer.echo(f"{c.id}  {c.name:<28} cp={cp:<8} nodes={s.node_count:<4} {pools}")
    ok = sum(r.reachable for r in results)
    typer.echo(f"\n{ok}/{len(results)} clusters reachable", err=True)


@app.command("detect")
def detect_cmd(
    run_date: Annotated[
        str | None, typer.Option("--date", help="Run date YYYY-MM-DD (default: today in schedule tz)")
    ] = None,
    cluster: Annotated[list[str] | None, typer.Option("--cluster", "-c", help="Cluster ID or name; repeatable")] = None,
    config_dir: ConfigDirOpt = None,
    env: EnvOpt = None,
) -> None:
    """Collect and evaluate drift rules; print findings (no LLM, no posting)."""
    import asyncio
    from datetime import date
    from zoneinfo import ZoneInfo

    from drift.collect.collector import Collector
    from drift.collect.sources import SourceError, load_inventory, load_matrix, select
    from drift.domain.clock import SystemClock
    from drift.rules.engine import detect

    cfg = _load(config_dir, env, runtime=False)
    try:
        inventory = load_inventory(cfg.sources.inventory)
        matrix = load_matrix(cfg.sources.matrix)
        clusters = select(inventory, cluster)
        day = date.fromisoformat(run_date) if run_date else SystemClock(ZoneInfo(cfg.schedule.timezone)).now().date()
    except (SourceError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(ExitCode.CONFIG_ERROR) from exc
    results = asyncio.run(Collector(cfg.clusters).collect(clusters))
    det = detect(clusters, results, matrix, cfg.policy, day)
    for f in det.findings:
        extra = " ".join(
            part
            for part in (
                f"limit={f.limit}" if f.limit else "",
                f"days={f.days_remaining}" if f.days_remaining is not None else "",
                f"nodes={f.nodes_affected}" if f.nodes_affected is not None else "",
            )
            if part
        )
        typer.echo(
            f"{f.severity.name:<8} {f.rule.value:<20} {f.cluster_name:<26} {f.subject:<14} {f.observed:<16} {extra}"
        )
    counts = ", ".join(f"{s.name}={n}" for s, n in sorted(det.by_severity().items(), reverse=True) if n)
    typer.echo(
        f"\nrun date {day}  matrix {matrix.version}  checked {det.checked}/{det.total}  "
        f"compliant {len(det.compliant)}  findings: {counts or 'none'}",
        err=True,
    )


@app.command("history")
def history_cmd(
    cluster: Annotated[str, typer.Argument(help="Cluster ID or name")],
    since: Annotated[str | None, typer.Option("--since", help="Only runs on/after YYYY-MM-DD")] = None,
    config_dir: ConfigDirOpt = None,
    env: EnvOpt = None,
) -> None:
    """Findings history for one cluster (FR-33)."""
    from datetime import date

    from drift.collect.sources import SourceError, load_inventory
    from drift.store.sqlite import Store

    cfg = _load(config_dir, env, runtime=False)
    cid = cluster
    try:
        inv = load_inventory(cfg.sources.inventory)
        cid = next((c.id for c in inv.clusters if cluster in (c.id, c.name)), cluster)
    except SourceError:
        pass  # history may outlive the current inventory; fall back to the literal ID
    store = Store(cfg.storage.db_path)
    rows = store.history(cid, date.fromisoformat(since) if since else None)
    store.close()
    if not rows:
        typer.echo(f"no history for {cluster}")
        return
    for r in rows:
        f = r["finding"]
        typer.echo(
            f"{r['run_date']}  {r['tag']:<11} {r['severity']:<8} {r['rule']:<20} {r['subject']:<14} {f['observed']}"
        )


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
