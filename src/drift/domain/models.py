"""Core data records: inventory, matrix, fetch results and normalised snapshots."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from drift.domain.enums import Environment, Provider
from drift.domain.versions import Minor, VersionReading, parse_version


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ------------------------------------------------------------------------- inventory (FR-1)


class ClusterRef(_Frozen):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    provider: Provider
    environment: Environment
    region: str
    team: str
    # Real deployments may set the API server per cluster. Never logged or written to run records (FR-32).
    api_server: str | None = Field(default=None, repr=False)


class Inventory(_Frozen):
    clusters: tuple[ClusterRef, ...]

    @model_validator(mode="after")
    def _unique(self) -> Inventory:
        for attr in ("id", "name"):
            seen: set[str] = set()
            for c in self.clusters:
                value = getattr(c, attr)
                if value in seen:
                    raise ValueError(f"duplicate cluster {attr} {value!r} in inventory")
                seen.add(value)
        return self

    def by_id(self) -> dict[str, ClusterRef]:
        return {c.id: c for c in self.clusters}


# ---------------------------------------------------------------------------- matrix (FR-8)


class MatrixEntry(_Frozen):
    minor: str
    latest_patch: str
    end_of_standard_support: date
    end_of_life: date

    @field_validator("minor")
    @classmethod
    def _minor(cls, v: str) -> str:
        Minor.parse(v)
        return v

    @model_validator(mode="after")
    def _consistent(self) -> MatrixEntry:
        latest = parse_version(self.latest_patch).parsed
        if latest is None or latest.minor_key != self.minor:
            raise ValueError(f"latest_patch {self.latest_patch!r} does not belong to minor {self.minor}")
        if self.end_of_life < self.end_of_standard_support:
            raise ValueError(f"{self.minor}: end_of_life is before end_of_standard_support")
        return self

    @property
    def latest(self) -> int:
        parsed = parse_version(self.latest_patch).parsed
        assert parsed is not None
        return parsed.patch


class Matrix(_Frozen):
    version: str = Field(min_length=1)
    disclaimer: str | None = None
    providers: dict[Provider, tuple[MatrixEntry, ...]]

    @model_validator(mode="after")
    def _unique_minors(self) -> Matrix:
        for provider, entries in self.providers.items():
            minors = [e.minor for e in entries]
            if len(minors) != len(set(minors)):
                raise ValueError(f"matrix provider {provider} lists a minor twice")
        return self

    def entry(self, provider: Provider, minor: str) -> MatrixEntry | None:
        for e in self.providers.get(provider, ()):
            if e.minor == minor:
                return e
        return None

    def known_range(self, provider: Provider) -> str:
        minors = sorted(
            (Minor.parse(e.minor) for e in self.providers.get(provider, ())), key=lambda m: (m.major, m.minor)
        )
        return f"{minors[0]}-{minors[-1]}" if minors else ""


# ------------------------------------------------------------------------ collection (FR-2, FR-5)


class NodeRecord(_Frozen):
    name: str
    pool: str
    kubelet_version: str
    ready: bool


class AttemptRecord(_Frozen):
    attempt: int
    outcome: str  # "ok" or an unreachable reason
    status_code: int | None = None
    error_type: str | None = None  # exception class name only; messages may contain endpoints (FR-32)
    duration_ms: int


class FetchResult(_Frozen):
    """Raw per-cluster collection outcome (FR-30). No endpoints, no credentials."""

    cluster_id: str
    reachable: bool
    reason: str | None = None  # set when unreachable
    control_plane_version: str | None = None
    nodes: tuple[NodeRecord, ...] = ()
    attempts: tuple[AttemptRecord, ...] = ()
    duration_ms: int = 0
    requests: int = 0


class PoolSnapshot(_Frozen):
    name: str
    nodes: tuple[tuple[str, VersionReading], ...]  # (node name, kubelet reading)
    not_ready: int = 0


class ClusterSnapshot(_Frozen):
    """A reachable cluster's normalised versions, ready for the rule engine."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)
    cluster: ClusterRef
    control_plane: VersionReading
    pools: tuple[PoolSnapshot, ...]

    @property
    def node_count(self) -> int:
        return sum(len(p.nodes) for p in self.pools)


def snapshot(cluster: ClusterRef, result: FetchResult) -> ClusterSnapshot:
    """Normalise a reachable fetch result (FR-7). Pools are sorted by name, nodes by name."""
    assert result.reachable, "only reachable clusters have snapshots"
    pools: dict[str, list[NodeRecord]] = {}
    for node in result.nodes:
        pools.setdefault(node.pool, []).append(node)
    return ClusterSnapshot(
        cluster=cluster,
        control_plane=parse_version(result.control_plane_version),
        pools=tuple(
            PoolSnapshot(
                name=name,
                nodes=tuple((n.name, parse_version(n.kubelet_version)) for n in sorted(nodes, key=lambda n: n.name)),
                not_ready=sum(1 for n in nodes if not n.ready),
            )
            for name, nodes in sorted(pools.items())
        ),
    )


def jsonable(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
