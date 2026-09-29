"""Builds the seed world, the night deltas and the hand-written expectations (SD-1..SD-8, EV-1, EV-2).

This file is the single hand-authored source of truth for the scenario. It deliberately does NOT
import the drift rule engine: every expected finding below is written out by hand. The only thing
computed here is ``days_remaining``, which is plain date subtraction from the literal dates.

Run:  uv run python scripts/build_seed.py      (rewrites seed/ and evals/expected/)
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "seed"
EXPECTED = ROOT / "evals" / "expected"

NIGHT_DATES = {
    0: date(2026, 10, 1),
    1: date(2026, 10, 2),
    2: date(2026, 10, 3),
    3: date(2026, 10, 5),
    4: date(2026, 10, 8),
}

# Copy of config/defaults.yaml policy.severities, written independently on purpose.
SEVERITY = {
    "CP_EOL": {"*": "CRITICAL"},
    "CP_UNSUPPORTED": {"*": "HIGH", "dev": "MEDIUM"},
    "CP_EOL_SOON": {"*": "MEDIUM", "prod": "HIGH", "dev": "LOW"},
    "CP_UNKNOWN_VERSION": {"*": "MEDIUM"},
    "CP_PATCH_BEHIND": {"*": "LOW", "prod": "MEDIUM"},
    "NODE_SKEW_EXCEEDED": {"*": "HIGH", "dev": "MEDIUM"},
    "NODE_NEWER_THAN_CP": {"*": "HIGH"},
    "NODE_POOL_MIXED": {"*": "MEDIUM", "dev": "LOW"},
    "NODE_EOL": {"*": "HIGH", "prod": "CRITICAL"},
    "UNREACHABLE": {"*": "HIGH", "dev": "MEDIUM"},
    "UNPARSEABLE_VERSION": {"*": "MEDIUM"},
}

# --------------------------------------------------------------------------- matrix (SD-2)
# ILLUSTRATIVE dates, chosen around the scenario base date 2026-10-01 to hit exact boundaries.
LATEST_PATCH = {"1.28": 15, "1.29": 14, "1.30": 12, "1.31": 9, "1.32": 7, "1.33": 5, "1.34": 6, "1.35": 4}
MATRIX_DATES: dict[str, dict[str, tuple[str, str]]] = {  # provider -> minor -> (end_of_standard_support, end_of_life)
    "eks": {
        "1.28": ("2025-11-26", "2026-09-30"),  # EOL yesterday
        "1.29": ("2026-03-23", "2026-10-01"),  # EOL today
        "1.30": ("2026-07-23", "2026-10-02"),  # EOL tomorrow (extended support until then)
        "1.31": ("2026-09-30", "2027-09-30"),  # standard support ended yesterday
        "1.32": ("2026-11-29", "2027-11-29"),  # EOS in 59 days (window - 1)
        "1.33": ("2026-11-30", "2027-11-30"),  # EOS in 60 days (exactly window)
        "1.34": ("2026-12-01", "2027-12-01"),  # EOS in 61 days (window + 1)
        "1.35": ("2027-06-01", "2028-06-01"),
    },
    "gke": {
        "1.28": ("2025-12-01", "2026-06-01"),
        "1.29": ("2026-01-15", "2026-09-15"),
        "1.30": ("2026-10-01", "2027-04-01"),  # standard support ends today
        "1.31": ("2026-10-02", "2027-04-02"),  # standard support ends tomorrow
        "1.32": ("2026-12-15", "2027-06-15"),
        "1.33": ("2027-03-01", "2027-09-01"),
        "1.34": ("2027-07-01", "2028-01-01"),
        "1.35": ("2027-10-01", "2028-04-01"),
    },
    "self-managed": {  # no paid extended support: EOS == EOL
        "1.29": ("2026-02-28", "2026-02-28"),
        "1.30": ("2026-06-28", "2026-06-28"),
        "1.31": ("2026-11-01", "2026-11-01"),  # 31 days -> crosses the 30-day WORSENED threshold on night 1
        "1.32": ("2027-02-28", "2027-02-28"),
        "1.33": ("2027-06-28", "2027-06-28"),
        "1.34": ("2027-10-28", "2027-10-28"),
        "1.35": ("2028-02-28", "2028-02-28"),
    },
}

POOL_LABEL = {
    "eks": "eks.amazonaws.com/nodegroup",
    "gke": "cloud.google.com/gke-nodepool",
    "self-managed": "node.kubernetes.io/pool",
}

_SEMVER = re.compile(r"\d+\.\d+\.\d+")


def raw(provider: str, version: str) -> str:
    """Provider-style version string (MS-2). Anything not plain x.y.z is passed through literally."""
    if not _SEMVER.fullmatch(version):
        return version
    if provider == "eks":
        return f"v{version}-eks-{hashlib.sha1(version.encode()).hexdigest()[:7]}"
    if provider == "gke":
        patch = int(version.split(".")[2])
        return f"v{version}-gke.{1000 + 100 * patch}"
    return f"v{version}"


# ------------------------------------------------------------------------- expectations


@dataclass
class Exp:
    rule: str
    subject: str
    observed: str
    limit: str | None = None
    date: str | None = None
    nodes: int | None = None

    def render(self, env: str, night: int) -> dict[str, Any]:
        sev = SEVERITY[self.rule].get(env, SEVERITY[self.rule]["*"])
        days = (date.fromisoformat(self.date) - NIGHT_DATES[night]).days if self.date else None
        return {
            "rule": self.rule,
            "subject": self.subject,
            "severity": sev,
            "observed": self.observed,
            "limit": self.limit,
            "date": self.date,
            "days_remaining": days,
            "nodes_affected": self.nodes,
        }


CP = "control-plane"
CL = "cluster"


@dataclass
class Cluster:
    id: str
    name: str
    provider: str
    env: str
    region: str
    team: str
    cp: str
    pools: list[tuple[str, dict[str, int]]] = field(default_factory=list)
    fault: dict[str, Any] | None = None
    expect: list[Exp] = field(default_factory=list)
    not_ready: dict[str, int] = field(default_factory=dict)

    def world(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "environment": self.env,
            "region": self.region,
            "team": self.team,
            "control_plane": raw(self.provider, self.cp),
            "node_pools": [
                {
                    "name": pool,
                    "versions": {raw(self.provider, v): n for v, n in versions.items()},
                    "not_ready": self.not_ready.get(pool, 0),
                }
                for pool, versions in self.pools
            ],
            "fault": self.fault,
        }

    def inventory(self) -> dict[str, Any]:
        return {k: self.world()[k] for k in ("id", "name", "provider", "environment", "region", "team")}


def one(pool: str, version: str, n: int) -> list[tuple[str, dict[str, int]]]:
    return [(pool, {version: n})]


E, G, S = "eks", "gke", "self-managed"

CLUSTERS: list[Cluster] = [
    # ============================== EKS (c001-c032) ==============================
    Cluster("c001", "payments-prod-1", E, "prod", "us-east-1", "payments", "1.35.4",
            [("ng-general", {"1.35.4": 6}), ("ng-spot", {"1.35.4": 4})]),
    Cluster("c002", "payments-prod-11", E, "prod", "us-east-1", "payments", "1.28.15", one("ng-general", "1.28.15", 4),
            expect=[Exp("CP_EOL", CP, "1.28.15", "2026-09-30", "2026-09-30"),
                    Exp("NODE_EOL", "ng-general", "1.28.15", "2026-09-30", "2026-09-30", 4)]),
    # S1: upgraded CP on night 2 (nodes left behind -> skew), nodes upgraded night 3.
    Cluster("c003", "checkout-prod-2", E, "prod", "eu-west-1", "checkout", "1.31.9",
            [("ng-general", {"1.31.9": 5}), ("ng-batch", {"1.31.9": 3})],
            expect=[Exp("CP_UNSUPPORTED", CP, "1.31.9", "2026-09-30", "2027-09-30")]),
    Cluster("c004", "identity-dev-4", E, "dev", "ap-south-1", "identity", "1.29.14", one("ng-general", "1.29.14", 2),
            expect=[Exp("CP_EOL", CP, "1.29.14", "2026-10-01", "2026-10-01"),
                    Exp("NODE_EOL", "ng-general", "1.29.14", "2026-10-01", "2026-10-01", 2)]),
    # EOL tomorrow -> escalates to CP_EOL on night 1.
    Cluster("c005", "search-staging-1", E, "staging", "us-east-1", "search", "1.30.12", one("ng-general", "1.30.12", 3),
            expect=[Exp("CP_UNSUPPORTED", CP, "1.30.12", "2026-07-23", "2026-10-02")]),
    Cluster("c006", "web-frontend-prod-3", E, "prod", "eu-west-1", "web-frontend", "1.32.7",
            one("ng-general", "1.32.7", 4),
            expect=[Exp("CP_EOL_SOON", CP, "1.32.7", "2026-11-29", "2026-11-29")]),
    Cluster("c007", "mobile-api-staging-2", E, "staging", "us-east-1", "mobile-api", "1.33.5",
            one("ng-general", "1.33.5", 3),
            expect=[Exp("CP_EOL_SOON", CP, "1.33.5", "2026-11-30", "2026-11-30")]),
    # Window + 1 day: compliant on night 0, CP_EOL_SOON appears on night 1.
    Cluster("c008", "data-platform-prod-1", E, "prod", "ap-south-1", "data-platform", "1.34.6",
            one("ng-general", "1.34.6", 4)),
    Cluster("c009", "risk-dev-2", E, "dev", "us-east-1", "risk", "1.36.0", one("ng-general", "1.36.0", 2),
            expect=[Exp("CP_UNKNOWN_VERSION", CP, "1.36.0", "1.28-1.35")]),
    Cluster("c010", "ml-infra-staging-1", E, "staging", "eu-west-1", "ml-infra", "1.35.1",
            one("ng-general", "1.35.1", 3),
            expect=[Exp("CP_PATCH_BEHIND", CP, "1.35.1", "1.35.4")]),
    # Patch 2 behind: below threshold, compliant.
    Cluster("c011", "observability-prod-1", E, "prod", "us-east-1", "observability", "1.35.2",
            one("ng-general", "1.35.2", 3)),
    # SD-6: three pools, only one drifts.
    Cluster("c012", "checkout-prod-12", E, "prod", "us-east-1", "checkout", "1.35.4",
            [("ng-general", {"1.35.4": 6}), ("ng-legacy", {"1.31.9": 3}), ("ng-spot", {"1.35.4": 4})],
            expect=[Exp("NODE_SKEW_EXCEEDED", "ng-legacy", "1.31.9", "1.32", nodes=3)]),
    # SD-5: exactly at the allowed skew (3 minors): compliant.
    Cluster("c013", "payments-staging-3", E, "staging", "eu-west-1", "payments", "1.35.3",
            [("ng-general", {"1.35.3": 2}), ("ng-old", {"1.32.7": 2})]),
    # S3: reachable, unreachable on nights 2-3, back on night 4.
    Cluster("c014", "search-prod-3", E, "prod", "us-east-1", "search", "1.35.1", one("ng-general", "1.35.1", 4),
            expect=[Exp("CP_PATCH_BEHIND", CP, "1.35.1", "1.35.4")]),
    Cluster("c015", "identity-prod-5", E, "prod", "ap-south-1", "identity", "1.35.4", one("ng-general", "1.35.4", 3),
            fault={"mode": "auth_error"},
            expect=[Exp("UNREACHABLE", CL, "auth error (HTTP 401)")]),
    Cluster("c016", "data-platform-staging-2", E, "staging", "us-east-1", "data-platform", "1.35.4",
            one("ng-general", "1.35.4", 2), fault={"mode": "malformed"},
            expect=[Exp("UNREACHABLE", CL, "malformed response")]),
    Cluster("c017", "web-frontend-dev-1", E, "dev", "us-east-1", "web-frontend", "v1.x.3-eks-bad",
            one("ng-general", "1.35.4", 2),
            expect=[Exp("UNPARSEABLE_VERSION", CP, "v1.x.3-eks-bad")]),
    # S5: compliant, pool mid-upgrade nights 1-2, completes night 3.
    Cluster("c018", "mobile-api-prod-4", E, "prod", "eu-west-1", "mobile-api", "1.35.4", one("ng-general", "1.34.6", 6)),
    Cluster("c019", "risk-prod-1", E, "prod", "us-east-1", "risk", "1.35.4",
            [("ng-general", {"1.35.4": 3, "1.34.6": 2})],
            expect=[Exp("NODE_POOL_MIXED", "ng-general", "1.34, 1.35", nodes=2)]),
    Cluster("c020", "checkout-prod-3", E, "prod", "eu-west-1", "checkout", "1.35.4",
            [("ng-general", {"1.35.4": 4}), ("ng-arm", {"1.35.4": 2})], not_ready={"ng-general": 1}),
    # Times out twice, succeeds on the third attempt (retries = 2).
    Cluster("c021", "checkout-dev-5", E, "dev", "ap-south-1", "checkout", "1.35.3", one("ng-general", "1.35.3", 2),
            fault={"script": ["timeout", "timeout"]}),
    Cluster("c022", "observability-staging-3", E, "staging", "us-east-1", "observability", "1.35.4",
            one("ng-general", "1.35.4", 3), fault={"latency_ms": 300}),
    Cluster("c023", "identity-prod-2", E, "prod", "eu-west-1", "identity", "1.35.4", one("ng-general", "1.35.4", 5)),
    # SD-6: several hundred nodes (exercises pagination).
    Cluster("c024", "data-platform-prod-7", E, "prod", "us-east-1", "data-platform", "1.35.4",
            [("ng-general", {"1.35.4": 150}), ("ng-memory", {"1.35.4": 120}), ("ng-spot", {"1.35.3": 80})]),
    # SD-6: zero nodes.
    Cluster("c025", "ml-infra-dev-3", E, "dev", "us-east-1", "ml-infra", "1.35.4", []),
    Cluster("c026", "identity-staging-6", E, "staging", "eu-west-1", "identity", "1.31.8", one("ng-general", "1.31.8", 3),
            expect=[Exp("CP_UNSUPPORTED", CP, "1.31.8", "2026-09-30", "2027-09-30")]),
    Cluster("c027", "search-prod-4", E, "prod", "us-east-1", "search", "1.35.4", one("ng-general", "1.35.4", 6)),
    Cluster("c028", "web-frontend-prod-1", E, "prod", "eu-west-1", "web-frontend", "1.35.3",
            [("ng-general", {"1.35.3": 4}), ("ng-spot", {"1.35.3": 6})]),
    Cluster("c029", "mobile-api-dev-2", E, "dev", "ap-south-1", "mobile-api", "1.35.2", one("ng-general", "1.35.2", 2)),
    Cluster("c030", "risk-staging-1", E, "staging", "us-east-1", "risk", "1.35.4", one("ng-general", "1.35.4", 3)),
    Cluster("c031", "platform-tools-prod-1", E, "prod", "us-east-1", "platform", "1.35.4",
            one("ng-general", "1.35.4", 4)),
    Cluster("c032", "payments-dev-1", E, "dev", "us-east-1", "payments", "1.35.4", one("ng-general", "1.35.4", 2)),
    # ============================== GKE (c033-c060) ==============================
    Cluster("c033", "payments-prod-21", G, "prod", "us-central1", "payments", "1.29.14",
            one("default-pool", "1.29.14", 3),
            expect=[Exp("CP_EOL", CP, "1.29.14", "2026-09-15", "2026-09-15"),
                    Exp("NODE_EOL", "default-pool", "1.29.14", "2026-09-15", "2026-09-15", 3)]),
    Cluster("c034", "search-staging-4", G, "staging", "europe-west1", "search", "1.30.12",
            one("default-pool", "1.30.12", 3),
            expect=[Exp("CP_UNSUPPORTED", CP, "1.30.12", "2026-10-01", "2027-04-01")]),
    # S2: EOS tomorrow -> escalates to CP_UNSUPPORTED on night 1.
    Cluster("c035", "checkout-staging-7", G, "staging", "us-central1", "checkout", "1.31.9",
            one("default-pool", "1.31.9", 3),
            expect=[Exp("CP_EOL_SOON", CP, "1.31.9", "2026-10-02", "2026-10-02")]),
    Cluster("c036", "ml-infra-dev-5", G, "dev", "asia-south1", "ml-infra", "1.36.1", one("default-pool", "1.36.1", 2),
            expect=[Exp("CP_UNKNOWN_VERSION", CP, "1.36.1", "1.28-1.35")]),
    Cluster("c037", "observability-prod-2", G, "prod", "us-central1", "observability", "1.34.1",
            one("default-pool", "1.34.1", 3),
            expect=[Exp("CP_PATCH_BEHIND", CP, "1.34.1", "1.34.6")]),
    # Patch 3 behind (exact threshold); patched on night 4.
    Cluster("c038", "web-frontend-staging-5", G, "staging", "europe-west1", "web-frontend", "1.33.2",
            one("default-pool", "1.33.2", 2),
            expect=[Exp("CP_PATCH_BEHIND", CP, "1.33.2", "1.33.5")]),
    Cluster("c039", "mobile-api-prod-6", G, "prod", "europe-west1", "mobile-api", "1.33.5",
            [("default-pool", {"1.33.5": 3}), ("canary-pool", {"1.34.6": 2})],
            expect=[Exp("NODE_NEWER_THAN_CP", "canary-pool", "1.34.6", "1.33", nodes=2)]),
    Cluster("c040", "risk-staging-3", G, "staging", "us-central1", "risk", "1.32.7", one("default-pool", "1.33.5", 3),
            expect=[Exp("NODE_NEWER_THAN_CP", "default-pool", "1.33.5", "1.32", nodes=3)]),
    Cluster("c041", "identity-dev-7", G, "dev", "asia-south1", "identity", "1.34.6", one("default-pool", "1.35.4", 2),
            expect=[Exp("NODE_NEWER_THAN_CP", "default-pool", "1.35.4", "1.34", nodes=2)]),
    Cluster("c042", "data-platform-prod-3", G, "prod", "us-central1", "data-platform", "1.32.7",
            [("default-pool", {"1.32.7": 4}), ("legacy-pool", {"1.29.14": 2})],
            expect=[Exp("NODE_EOL", "legacy-pool", "1.29.14", "2026-09-15", "2026-09-15", 2)]),
    Cluster("c043", "search-dev-2", G, "dev", "asia-south1", "search", "1.32.6", one("default-pool", "1.29.13", 2),
            expect=[Exp("NODE_EOL", "default-pool", "1.29.13", "2026-09-15", "2026-09-15", 2)]),
    Cluster("c044", "payments-staging-8", G, "staging", "europe-west1", "payments", "1.34.6",
            [("default-pool", {"1.34.6": 3}), ("batch-pool", {"1.30.12": 2})],
            expect=[Exp("NODE_SKEW_EXCEEDED", "batch-pool", "1.30.12", "1.31", nodes=2)]),
    Cluster("c045", "checkout-dev-9", G, "dev", "us-central1", "checkout", "1.35.4", one("default-pool", "1.31.9", 2),
            expect=[Exp("NODE_SKEW_EXCEEDED", "default-pool", "1.31.9", "1.32", nodes=2)]),
    Cluster("c046", "observability-staging-8", G, "staging", "europe-west1", "observability", "1.34.6",
            [("default-pool", {"1.34.6": 4, "1.33.5": 1})],
            expect=[Exp("NODE_POOL_MIXED", "default-pool", "1.33, 1.34", nodes=1)]),
    Cluster("c047", "ml-infra-staging-9", G, "staging", "us-central1", "ml-infra", "1.34.6",
            one("default-pool", "1.34.6", 3), fault={"mode": "timeout"},
            expect=[Exp("UNREACHABLE", CL, "timeout")]),
    Cluster("c048", "web-frontend-prod-8", G, "prod", "us-central1", "web-frontend", "1.34.6",
            [("default-pool", {"1.34.6": 3}), ("gpu-pool", {"unknown": 2})],
            expect=[Exp("UNPARSEABLE_VERSION", "gpu-pool", "unknown", nodes=2)]),
    # S7: new pool with newer nodes appears on night 3.
    Cluster("c049", "mobile-api-staging-10", G, "staging", "europe-west1", "mobile-api", "1.33.5",
            one("default-pool", "1.33.5", 3)),
    Cluster("c050", "data-platform-staging-5", G, "staging", "us-central1", "data-platform", "1.32.7",
            one("default-pool", "1.32.7", 3)),
    Cluster("c051", "payments-dev-21", G, "dev", "asia-south1", "payments", "1.35.4", one("default-pool", "1.35.4", 2)),
    Cluster("c052", "checkout-prod-21", G, "prod", "us-central1", "checkout", "1.34.6",
            [("default-pool", {"1.34.6": 5}), ("highmem-pool", {"1.34.6": 3})]),
    Cluster("c053", "search-prod-21", G, "prod", "europe-west1", "search", "1.34.5", one("default-pool", "1.34.5", 6)),
    Cluster("c054", "identity-staging-21", G, "staging", "us-central1", "identity", "1.33.5",
            one("default-pool", "1.33.5", 3)),
    Cluster("c055", "ml-infra-prod-21", G, "prod", "us-central1", "ml-infra", "1.35.4",
            [("default-pool", {"1.35.4": 4}), ("gpu-pool", {"1.35.4": 2})]),
    Cluster("c056", "observability-dev-21", G, "dev", "asia-south1", "observability", "1.35.3",
            one("default-pool", "1.35.3", 2)),
    Cluster("c057", "web-frontend-prod-21", G, "prod", "europe-west1", "web-frontend", "1.34.6",
            one("default-pool", "1.34.6", 8), not_ready={"default-pool": 2}),
    Cluster("c058", "risk-prod-21", G, "prod", "us-central1", "risk", "1.35.4", one("default-pool", "1.35.4", 4)),
    Cluster("c059", "platform-tools-staging-2", G, "staging", "europe-west1", "platform", "1.33.4",
            one("default-pool", "1.33.4", 2)),
    Cluster("c060", "mobile-api-prod-21", G, "prod", "us-central1", "mobile-api", "1.34.6",
            one("default-pool", "1.33.5", 4)),  # nodes one minor behind: within skew
    # ========================== self-managed (c061-c080) ==========================
    # SD-8: "1.3" vs "1.30" look-alike.
    Cluster("c061", "platform-lab-dev-1", S, "dev", "dc-mumbai-1", "platform", "1.3.0", one("workers", "1.3.0", 2),
            expect=[Exp("CP_UNKNOWN_VERSION", CP, "1.3.0", "1.29-1.35")]),
    Cluster("c062", "risk-prod-4", S, "prod", "dc-frankfurt-1", "risk", "1.30.12", one("workers", "1.30.12", 3),
            expect=[Exp("CP_EOL", CP, "1.30.12", "2026-06-28", "2026-06-28"),
                    Exp("NODE_EOL", "workers", "1.30.12", "2026-06-28", "2026-06-28", 3)]),
    # S6: 31 days -> 30 on night 1 (crosses WORSENED threshold).
    Cluster("c063", "payments-prod-31", S, "prod", "dc-mumbai-1", "payments", "1.31.9", one("workers", "1.31.9", 4),
            expect=[Exp("CP_EOL_SOON", CP, "1.31.9", "2026-11-01", "2026-11-01")]),
    # Also S6; upgraded on night 4 -> RESOLVED.
    Cluster("c064", "search-staging-9", S, "staging", "dc-frankfurt-1", "search", "1.31.9", one("workers", "1.31.9", 3),
            expect=[Exp("CP_EOL_SOON", CP, "1.31.9", "2026-11-01", "2026-11-01")]),
    Cluster("c065", "identity-prod-9", S, "prod", "dc-mumbai-1", "identity", "1.34.6", one("workers", "1.34.6", 3),
            fault={"mode": "http_error", "status": 500},
            expect=[Exp("UNREACHABLE", CL, "HTTP 500")]),
    Cluster("c066", "checkout-dev-3", S, "dev", "dc-mumbai-1", "checkout", "v1.33", one("workers", "1.33.4", 2),
            expect=[Exp("UNPARSEABLE_VERSION", CP, "v1.33")]),
    Cluster("c067", "observability-dev-4", S, "dev", "dc-frankfurt-1", "observability", "1.34.5",
            [("workers", {"1.34.5": 2}), ("gpu", {"v1.34.x": 1})],
            expect=[Exp("UNPARSEABLE_VERSION", "gpu", "v1.34.x", nodes=1)]),
    Cluster("c068", "ml-infra-prod-2", S, "prod", "dc-mumbai-1", "ml-infra", "1.35.3",
            [("workers", {"1.35.3": 4}), ("gpu", {"1.31.9": 2})],
            expect=[Exp("NODE_SKEW_EXCEEDED", "gpu", "1.31.9", "1.32", nodes=2)]),
    Cluster("c069", "web-frontend-staging-2", S, "staging", "dc-frankfurt-1", "web-frontend", "1.34.5",
            [("workers", {"1.33.5": 2, "1.34.5": 3})],
            expect=[Exp("NODE_POOL_MIXED", "workers", "1.33, 1.34", nodes=2)]),
    Cluster("c070", "mobile-api-dev-5", S, "dev", "dc-mumbai-1", "mobile-api", "1.33.4",
            [("workers", {"1.33.4": 2}), ("old", {"1.30.12": 1})],
            expect=[Exp("NODE_EOL", "old", "1.30.12", "2026-06-28", "2026-06-28", 1)]),
    Cluster("c071", "platform-tools-prod-31", S, "prod", "dc-mumbai-1", "platform", "1.35.4", one("workers", "1.35.4", 3)),
    Cluster("c072", "payments-dev-31", S, "dev", "dc-mumbai-1", "payments", "1.34.6", one("workers", "1.34.6", 2)),
    Cluster("c073", "checkout-staging-31", S, "staging", "dc-frankfurt-1", "checkout", "1.34.5",
            one("workers", "1.34.5", 3)),
    Cluster("c074", "search-prod-31", S, "prod", "dc-mumbai-1", "search", "1.33.5",
            [("workers", {"1.33.5": 5}), ("infra", {"1.33.5": 2})]),
    Cluster("c075", "identity-dev-31", S, "dev", "dc-frankfurt-1", "identity", "1.33.4", one("workers", "1.33.4", 2)),
    Cluster("c076", "data-platform-prod-31", S, "prod", "dc-mumbai-1", "data-platform", "1.32.7",
            one("workers", "1.32.7", 6)),
    Cluster("c077", "ml-infra-staging-31", S, "staging", "dc-frankfurt-1", "ml-infra", "1.35.4",
            one("workers", "1.35.4", 3)),
    Cluster("c078", "observability-prod-31", S, "prod", "dc-mumbai-1", "observability", "1.34.6",
            one("workers", "1.34.6", 4)),
    Cluster("c079", "web-frontend-dev-31", S, "dev", "dc-frankfurt-1", "web-frontend", "1.35.3",
            one("workers", "1.35.3", 2)),
    Cluster("c080", "risk-staging-31", S, "staging", "dc-mumbai-1", "risk", "1.33.5", one("workers", "1.33.5", 3)),
]

BY_ID = {c.id: c for c in CLUSTERS}

# c081 joins the inventory on night 2 (NEW_CLUSTER).
C081 = Cluster("c081", "ml-infra-dev-7", E, "dev", "us-east-1", "ml-infra", "1.35.1", one("ng-general", "1.35.1", 2),
               expect=[Exp("CP_PATCH_BEHIND", CP, "1.35.1", "1.35.4")])


def ev(provider: str, v: str) -> str:
    return raw(provider, v)


# ------------------------------------------------------------------------ nights (SD-7)
# Each night: world changes (applied cumulatively) + expected-finding overrides + expected tag overrides.
# Unlisted current findings are expected ONGOING; unlisted clusters keep their previous expectations.

NIGHTS: dict[int, dict[str, Any]] = {
    1: {
        "changes": [
            {"op": "set_pool", "cluster": "c018", "pool": "ng-general",
             "versions": {ev(E, "1.35.4"): 2, ev(E, "1.34.6"): 4}},
        ],
        "expect": {
            "c005": [Exp("CP_EOL", CP, "1.30.12", "2026-10-02", "2026-10-02"),
                     Exp("NODE_EOL", "ng-general", "1.30.12", "2026-10-02", "2026-10-02", 3)],
            "c035": [Exp("CP_UNSUPPORTED", CP, "1.31.9", "2026-10-02", "2027-04-02")],
            "c008": [Exp("CP_EOL_SOON", CP, "1.34.6", "2026-12-01", "2026-12-01")],
            "c018": [Exp("NODE_POOL_MIXED", "ng-general", "1.34, 1.35", nodes=4)],
        },
        "tags": [
            ("c005", "CP_EOL", CP, "WORSENED"),  # escalated from CP_UNSUPPORTED (not reported RESOLVED)
            ("c005", "NODE_EOL", "ng-general", "NEW"),
            ("c035", "CP_UNSUPPORTED", CP, "WORSENED"),  # escalated from CP_EOL_SOON
            ("c008", "CP_EOL_SOON", CP, "NEW"),
            ("c063", "CP_EOL_SOON", CP, "WORSENED"),  # 31 -> 30 days crosses threshold 30
            ("c064", "CP_EOL_SOON", CP, "WORSENED"),
            ("c018", "NODE_POOL_MIXED", "ng-general", "NEW"),
        ],
    },
    2: {
        "changes": [
            {"op": "set_control_plane", "cluster": "c003", "version": ev(E, "1.35.4")},
            {"op": "set_fault", "cluster": "c014", "fault": {"mode": "timeout"}},
            {"op": "add_cluster", "cluster": C081.world()},
            {"op": "set_pool", "cluster": "c018", "pool": "ng-general",
             "versions": {ev(E, "1.35.4"): 3, ev(E, "1.34.6"): 3}},
        ],
        "expect": {
            "c003": [Exp("NODE_SKEW_EXCEEDED", "ng-batch", "1.31.9", "1.32", nodes=3),
                     Exp("NODE_SKEW_EXCEEDED", "ng-general", "1.31.9", "1.32", nodes=5)],
            "c014": [Exp("UNREACHABLE", CL, "timeout")],
            "c081": C081.expect,
            "c018": [Exp("NODE_POOL_MIXED", "ng-general", "1.34, 1.35", nodes=3)],
        },
        "tags": [
            ("c003", "CP_UNSUPPORTED", CP, "RESOLVED"),
            ("c003", "NODE_SKEW_EXCEEDED", "ng-general", "NEW"),
            ("c003", "NODE_SKEW_EXCEEDED", "ng-batch", "NEW"),
            ("c014", "UNREACHABLE", CL, "NEW"),  # FR-17: reachable last run, unreachable tonight
            # c014 CP_PATCH_BEHIND is NOT resolved: the cluster could not be checked (carried as stale).
            ("c081", "CP_PATCH_BEHIND", CP, "NEW_CLUSTER"),
        ],
    },
    3: {
        "changes": [
            {"op": "set_pool", "cluster": "c003", "pool": "ng-general", "versions": {ev(E, "1.35.4"): 5}},
            {"op": "set_pool", "cluster": "c003", "pool": "ng-batch", "versions": {ev(E, "1.35.4"): 3}},
            {"op": "set_pool", "cluster": "c018", "pool": "ng-general", "versions": {ev(E, "1.35.4"): 6}},
            {"op": "set_pool", "cluster": "c049", "pool": "new-pool", "versions": {ev(G, "1.34.6"): 2}},
        ],
        "expect": {
            "c003": [],
            "c018": [],
            "c049": [Exp("NODE_NEWER_THAN_CP", "new-pool", "1.34.6", "1.33", nodes=2)],
        },
        "tags": [
            ("c003", "NODE_SKEW_EXCEEDED", "ng-general", "RESOLVED"),
            ("c003", "NODE_SKEW_EXCEEDED", "ng-batch", "RESOLVED"),
            ("c018", "NODE_POOL_MIXED", "ng-general", "RESOLVED"),
            ("c049", "NODE_NEWER_THAN_CP", "new-pool", "NEW"),
        ],
    },
    4: {
        "changes": [
            {"op": "set_fault", "cluster": "c014", "fault": None},
            {"op": "set_control_plane", "cluster": "c038", "version": ev(G, "1.33.5")},
            {"op": "set_pool", "cluster": "c038", "pool": "default-pool", "versions": {ev(G, "1.33.5"): 2}},
            {"op": "set_control_plane", "cluster": "c064", "version": ev(S, "1.33.4")},
            {"op": "set_pool", "cluster": "c064", "pool": "workers", "versions": {ev(S, "1.33.4"): 3}},
        ],
        "expect": {
            "c014": [Exp("CP_PATCH_BEHIND", CP, "1.35.1", "1.35.4")],
            "c038": [],
            "c064": [],
        },
        "tags": [
            ("c014", "UNREACHABLE", CL, "RESOLVED"),
            ("c014", "CP_PATCH_BEHIND", CP, "ONGOING"),  # stale during outage, still present
            ("c038", "CP_PATCH_BEHIND", CP, "RESOLVED"),
            ("c064", "CP_EOL_SOON", CP, "RESOLVED"),
        ],
    },
}

# ------------------------------------------------------------------------------ writers

HEADER = "# GENERATED by scripts/build_seed.py — edit that file, not this one.\n"


def _dump(path: Path, data: Any, header: str = HEADER) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + yaml.safe_dump(data, sort_keys=False, width=120, allow_unicode=True))


def build_matrix() -> dict[str, Any]:
    providers: dict[str, Any] = {}
    for provider, minors in MATRIX_DATES.items():
        providers[provider] = [
            {
                "minor": minor,
                "latest_patch": f"{minor}.{LATEST_PATCH[minor]}",
                "end_of_standard_support": eos,
                "end_of_life": eol,
            }
            for minor, (eos, eol) in minors.items()
        ]
    return {
        "version": "illustrative-2026-09-15.1",
        "disclaimer": (
            "ILLUSTRATIVE TEST DATA. These dates and patch levels are invented to exercise boundary "
            "conditions around 2026-10-01. They are NOT an authoritative statement of any provider's "
            "real support dates."
        ),
        "providers": providers,
    }


def expectations() -> dict[int, dict[str, list[Exp]]]:
    """Expected findings per night per cluster (cluster id -> list)."""
    cur: dict[str, list[Exp]] = {c.id: list(c.expect) for c in CLUSTERS}
    envs = {c.id: c.env for c in [*CLUSTERS, C081]}
    out = {0: {k: list(v) for k, v in cur.items()}}
    for night in sorted(NIGHTS):
        for cid, exps in NIGHTS[night]["expect"].items():
            cur[cid] = list(exps)
        out[night] = {k: list(v) for k, v in cur.items()}
    assert set(envs) >= set(cur)
    return out


def render_findings(night: int, per_cluster: dict[str, list[Exp]]) -> list[dict[str, Any]]:
    envs = {c.id: c.env for c in [*CLUSTERS, C081]}
    rows = []
    for cid in sorted(per_cluster):
        for e in sorted(per_cluster[cid], key=lambda x: (x.rule, x.subject)):
            rows.append({"cluster": cid, **e.render(envs[cid], night)})
    return rows


def render_tags(night: int, per_cluster: dict[str, list[Exp]]) -> list[dict[str, str]]:
    if night == 0:
        return [
            {"cluster": cid, "rule": e.rule, "subject": e.subject, "tag": "NEW"}
            for cid in sorted(per_cluster)
            for e in sorted(per_cluster[cid], key=lambda x: (x.rule, x.subject))
        ]
    overrides = {(c, r, s): t for c, r, s, t in NIGHTS[night]["tags"]}
    rows = []
    current = set()
    for cid in sorted(per_cluster):
        for e in per_cluster[cid]:
            key = (cid, e.rule, e.subject)
            current.add(key)
            rows.append({"cluster": cid, "rule": e.rule, "subject": e.subject, "tag": overrides.get(key, "ONGOING")})
    for key, tag in overrides.items():
        if key not in current:
            assert tag == "RESOLVED", f"night {night}: {key} tagged {tag} but not a current finding"
            rows.append({"cluster": key[0], "rule": key[1], "subject": key[2], "tag": tag})
    return sorted(rows, key=lambda r: (r["cluster"], r["rule"], r["subject"]))


def main() -> None:
    _dump(SEED / "matrix.yaml", build_matrix(),
          HEADER + "# ILLUSTRATIVE TEST DATA — not an authoritative statement of any provider's support dates.\n")
    _dump(SEED / "world.yaml", {
        "base_night": 0,
        "pool_labels": POOL_LABEL,
        "clusters": [c.world() for c in CLUSTERS],
    })
    _dump(SEED / "inventory.yaml", {"clusters": [c.inventory() for c in CLUSTERS]},
          HEADER + "# Night-0 inventory. The mock writes the effective inventory for the current night to\n"
                   "# <mocks.state_dir>/inventory.yaml; the default config reads that file.\n")
    _dump(SEED / "scenario.yaml", {
        "nights": {n: {"date": d.isoformat()} for n, d in NIGHT_DATES.items()},
        "sequences": [
            {"id": "S1", "clusters": ["c003"], "story": "CP upgraded night 2 leaving nodes behind (skew), nodes upgraded night 3"},
            {"id": "S2", "clusters": ["c005", "c035"], "story": "date passes: escalation EOL_SOON->UNSUPPORTED, UNSUPPORTED->EOL"},
            {"id": "S3", "clusters": ["c014"], "story": "reachable, unreachable nights 2-3, reachable night 4"},
            {"id": "S4", "clusters": ["c081"], "story": "new cluster joins inventory night 2"},
            {"id": "S5", "clusters": ["c018"], "story": "node pool mid-upgrade nights 1-2, completes night 3"},
            {"id": "S6", "clusters": ["c063", "c064"], "story": "days remaining crosses 30-day threshold night 1; c064 upgraded night 4"},
            {"id": "S7", "clusters": ["c049"], "story": "new node pool newer than control plane night 3"},
            {"id": "S8", "clusters": ["c008"], "story": "window+1 day on night 0 becomes EOL_SOON on night 1"},
            {"id": "S9", "clusters": ["c038"], "story": "patched night 4"},
        ],
    })
    for night in sorted(NIGHTS):
        _dump(SEED / "nights" / f"night_{night}.yaml",
              {"night": night, "date": NIGHT_DATES[night].isoformat(), "changes": NIGHTS[night]["changes"]})

    exp = expectations()
    for night, per_cluster in exp.items():
        _dump(EXPECTED / f"findings_night_{night}.yaml",
              {"night": night, "date": NIGHT_DATES[night].isoformat(), "findings": render_findings(night, per_cluster)})
        _dump(EXPECTED / f"tags_night_{night}.yaml",
              {"night": night, "first_run": night == 0, "tags": render_tags(night, per_cluster)})
    print(f"wrote seed for {len(CLUSTERS)} clusters and {len(exp)} nights")


if __name__ == "__main__":
    main()
