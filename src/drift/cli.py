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
