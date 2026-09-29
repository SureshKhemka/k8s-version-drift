"""Finding record (FR-10) and the context every rule sees."""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from drift.config.models import PolicyConfig
from drift.domain.enums import Environment, Provider, Rule, Severity
from drift.domain.models import ClusterRef, Matrix

CONTROL_PLANE = "control-plane"
CLUSTER = "cluster"


def finding_id(cluster_id: str, rule: Rule, subject: str) -> str:
    """Stable across runs for the same cluster + rule + subject (FR-10)."""
    digest = hashlib.sha256(f"{cluster_id}|{rule.value}|{subject}".encode()).hexdigest()[:10]
    return f"F-{digest}"


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    cluster_id: str
    cluster_name: str
    environment: Environment
    provider: Provider
    rule: Rule
    severity: Severity
    subject: str
    observed: str
    limit: str | None = None
    date: dt.date | None = None
    days_remaining: int | None = None
    nodes_affected: int | None = None

    @property
    def key(self) -> tuple[str, Rule, str]:
        return (self.cluster_id, self.rule, self.subject)


@dataclass(frozen=True)
class RuleContext:
    matrix: Matrix
    policy: PolicyConfig
    run_date: dt.date

    def make(
        self,
        cluster: ClusterRef,
        rule: Rule,
        subject: str,
        observed: str,
        *,
        limit: str | None = None,
        on: dt.date | None = None,
        nodes: int | None = None,
    ) -> Finding:
        return Finding(
            id=finding_id(cluster.id, rule, subject),
            cluster_id=cluster.id,
            cluster_name=cluster.name,
            environment=cluster.environment,
            provider=cluster.provider,
            rule=rule,
            severity=self.policy.severity(rule, cluster.environment),
            subject=subject,
            observed=observed,
            limit=limit,
            date=on,
            days_remaining=(on - self.run_date).days if on else None,
            nodes_affected=nodes,
        )

    def days_until(self, d: dt.date) -> int:
        return (d - self.run_date).days


SEVERITY_ORDER = tuple(sorted(Severity, reverse=True))
