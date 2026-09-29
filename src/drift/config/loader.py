"""Layered configuration loading (CF-2) and runtime validation (CF-4).

Layers, later wins:
  1. ``<config_dir>/defaults.yaml``
  2. ``<config_dir>/<env>.yaml``  (env from ``DRIFT_ENV``, default ``local``; optional file)
  3. environment variables ``DRIFT__SECTION__KEY=value`` (value parsed as YAML)

Relative paths in the config are resolved against the project root (parent of the config dir).
"""

from __future__ import annotations

import copy
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from drift.config.models import AppConfig
from drift.domain.enums import OutputMode

ENV_PREFIX = "DRIFT__"
_SENSITIVE_KEY = re.compile(r"(^|_)(url|endpoint|token|secret|password|api_key|headers)(_|$)", re.IGNORECASE)
MASK = "***"


class ConfigError(Exception):
    """Invalid configuration. Raised before any cluster is contacted (CF-4)."""


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _match_key(container: Mapping[str, Any], key: str) -> str:
    """Env var names are case-insensitive; reuse the existing key's spelling if there is one."""
    for existing in container:
        if existing.lower() == key.lower():
            return existing
    return key.lower()


def _env_overlay(raw: dict[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    out = copy.deepcopy(raw)
    for name, value in sorted(environ.items()):
        if not name.startswith(ENV_PREFIX):
            continue
        parts = [p for p in name[len(ENV_PREFIX) :].split("__") if p]
        if not parts:
            continue
        node = out
        for part in parts[:-1]:
            key = _match_key(node, part)
            if not isinstance(node.get(key), dict):
                node[key] = {}
            node = node[key]
        node[_match_key(node, parts[-1])] = yaml.safe_load(value) if value != "" else ""
    return out


def _resolve_paths(data: Any, root: Path) -> Any:
    if isinstance(data, Path):
        return data if data.is_absolute() else (root / data)
    if isinstance(data, dict):
        return {k: _resolve_paths(v, root) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return type(data)(_resolve_paths(v, root) for v in data)
    return data


def load_config(
    config_dir: Path | str | None = None,
    env: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    environ = os.environ if environ is None else environ
    cfg_dir = Path(config_dir or environ.get("DRIFT_CONFIG_DIR", "config")).resolve()
    env = env or environ.get("DRIFT_ENV", "local")

    defaults = cfg_dir / "defaults.yaml"
    if not defaults.is_file():
        raise ConfigError(f"missing {defaults}")
    merged = _read_yaml(defaults)
    env_file = cfg_dir / f"{env}.yaml"
    if env_file.is_file():
        merged = _deep_merge(merged, _read_yaml(env_file))
    merged = _env_overlay(merged, environ)
    merged["environment"] = env

    try:
        cfg = AppConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc

    # Resolve relative paths against the project root, then re-validate the frozen model.
    resolved = _resolve_paths(cfg.model_dump(), cfg_dir.parent)
    return AppConfig.model_validate(resolved)


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["invalid configuration:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        lines.append(f"  - {loc or '<root>'}: {err['msg']}")
    return "\n".join(lines)


def validate_runtime(cfg: AppConfig, environ: Mapping[str, str] | None = None) -> None:
    """Checks that depend on the filesystem and environment. Raises ConfigError listing every problem."""
    environ = os.environ if environ is None else environ
    problems: list[str] = []

    for label, src in (("inventory", cfg.sources.inventory), ("matrix", cfg.sources.matrix)):
        if not src.path.is_file():
            problems.append(f"sources.{label}.path does not exist: {src.path}")

    if cfg.llm.enabled:
        for step in ("group", "draft", "shorten"):
            p = cfg.prompts.dir / getattr(cfg.prompts, step)
            if not p.is_file():
                problems.append(f"prompts.{step} not found: {p}")

    if cfg.output.mode is OutputMode.POST and not environ.get(cfg.channel.webhook_url_env):
        problems.append(f"output.mode is POST but env var {cfg.channel.webhook_url_env} is not set")

    if cfg.clusters.token_env and not environ.get(cfg.clusters.token_env):
        problems.append(f"clusters.token_env names {cfg.clusters.token_env}, which is not set")

    if cfg.llm.enabled:
        used = {cfg.llm.for_step(s).provider for s in ("group", "draft", "shorten")}
        for name in sorted(used):
            key_env = cfg.llm.providers[name].api_key_env
            if key_env and not environ.get(key_env):
                problems.append(f"llm.providers.{name}.api_key_env names {key_env}, which is not set")

    if problems:
        raise ConfigError("invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


def masked_dump(cfg: AppConfig) -> dict[str, Any]:
    """Effective configuration safe to log (CF-2, FR-32): endpoint/secret-like values replaced."""

    def mask(node: Any, key: str = "") -> Any:
        if key and _SENSITIVE_KEY.search(key) and not key.endswith("_env") and node not in (None, "", {}):
            return MASK
        if isinstance(node, dict):
            return {k: mask(v, str(k)) for k, v in node.items()}
        if isinstance(node, list | tuple):
            return [mask(v, key) for v in node]
        return node

    result: dict[str, Any] = mask(cfg.model_dump(mode="json"))
    return result
