"""Loading the inventory (FR-1) and the supported-version matrix (FR-8) from configured sources."""

from __future__ import annotations

from collections.abc import Iterable

import yaml
from pydantic import ValidationError

from drift.config.models import SourceConfig
from drift.domain.models import ClusterRef, Inventory, Matrix


class SourceError(Exception):
    """The inventory or matrix could not be loaded. Treated as a configuration error (CF-4)."""


def _read(src: SourceConfig, label: str) -> object:
    if src.type != "file":  # pragma: no cover - guarded by config schema
        raise SourceError(f"{label}: unsupported source type {src.type!r}")
    try:
        return yaml.safe_load(src.path.read_text())
    except OSError as exc:
        raise SourceError(f"{label}: cannot read {src.path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise SourceError(f"{label}: invalid YAML in {src.path}: {exc}") from exc


def _errors(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())


def load_inventory(src: SourceConfig) -> Inventory:
    try:
        return Inventory.model_validate(_read(src, "inventory"))
    except ValidationError as exc:
        raise SourceError(f"inventory {src.path}: {_errors(exc)}") from exc


def load_matrix(src: SourceConfig) -> Matrix:
    try:
        return Matrix.model_validate(_read(src, "matrix"))
    except ValidationError as exc:
        raise SourceError(f"matrix {src.path}: {_errors(exc)}") from exc


def select(inventory: Inventory, only: Iterable[str] | None) -> list[ClusterRef]:
    """FR-28: optionally restrict to a subset, by cluster ID or name. Unknown selectors are an error."""
    if not only:
        return list(inventory.clusters)
    wanted = set(only)
    chosen = [c for c in inventory.clusters if c.id in wanted or c.name in wanted]
    unknown = wanted - {c.id for c in chosen} - {c.name for c in chosen}
    if unknown:
        raise SourceError(f"unknown clusters: {', '.join(sorted(unknown))}")
    return chosen
