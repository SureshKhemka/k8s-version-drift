"""Closed vocabularies shared across the system."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Severity(IntEnum):
    """Ordered so that comparisons (``>=``) express "at least as severe as"."""

    INFO = 1
    LOW = 2
    MEDIUM = 3
    HIGH = 4
    CRITICAL = 5

    @classmethod
    def parse(cls, value: str | int | Severity) -> Severity:
        if isinstance(value, Severity):
            return value
        if isinstance(value, int):
            return cls(value)
        try:
            return cls[value.strip().upper()]
        except KeyError as exc:
            raise ValueError(f"unknown severity {value!r}; expected one of {[s.name for s in cls]}") from exc


class Rule(StrEnum):
    CP_EOL = "CP_EOL"
    CP_UNSUPPORTED = "CP_UNSUPPORTED"
    CP_EOL_SOON = "CP_EOL_SOON"
    CP_UNKNOWN_VERSION = "CP_UNKNOWN_VERSION"
    CP_PATCH_BEHIND = "CP_PATCH_BEHIND"
    NODE_SKEW_EXCEEDED = "NODE_SKEW_EXCEEDED"
    NODE_NEWER_THAN_CP = "NODE_NEWER_THAN_CP"
    NODE_POOL_MIXED = "NODE_POOL_MIXED"
    NODE_EOL = "NODE_EOL"
    UNREACHABLE = "UNREACHABLE"
    UNPARSEABLE_VERSION = "UNPARSEABLE_VERSION"


class Provider(StrEnum):
    EKS = "eks"
    GKE = "gke"
    SELF_MANAGED = "self-managed"


class Environment(StrEnum):
    PROD = "prod"
    STAGING = "staging"
    DEV = "dev"


class ChangeTag(StrEnum):
    NEW = "NEW"
    ONGOING = "ONGOING"
    WORSENED = "WORSENED"
    RESOLVED = "RESOLVED"
    NEW_CLUSTER = "NEW_CLUSTER"


class OutputMode(StrEnum):
    DRY_RUN = "DRY_RUN"
    POST = "POST"


class ExitCode(IntEnum):
    """FR-29. Most severe applicable status wins (config error > post failed > partial > fallback > ok)."""

    SUCCESS = 0
    SUCCESS_FALLBACK = 10
    PARTIAL = 20
    POST_FAILED = 30
    CONFIG_ERROR = 40
