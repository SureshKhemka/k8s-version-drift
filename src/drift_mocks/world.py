"""Scenario world for the mock cluster API (MS-1, MS-2, MS-4, MS-6).

The world is loaded from seed files, can be reset, and can be advanced night by night.
It renders Kubernetes-shaped ``/version`` and ``NodeList`` payloads on demand.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class WorldError(Exception):
    pass


@dataclass
class World:
    seed_dir: Path
    night: int
    date: str
    clusters: dict[str, dict[str, Any]]  # id -> world cluster record (see seed/world.yaml)
    pool_labels: dict[str, str]

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(cls, seed_dir: Path, night: int = 0) -> World:
        base = yaml.safe_load((seed_dir / "world.yaml").read_text())
        scenario = yaml.safe_load((seed_dir / "scenario.yaml").read_text())
        nights = {int(k): v for k, v in scenario["nights"].items()}
        if night not in nights:
            raise WorldError(f"unknown night {night}; available: {sorted(nights)}")
        world = cls(
            seed_dir=seed_dir,
            night=0,
            date=nights[0]["date"],
            clusters={c["id"]: c for c in copy.deepcopy(base["clusters"])},
            pool_labels=base["pool_labels"],
        )
        for n in range(1, night + 1):
            world._apply_night(n)
        return world

    def _apply_night(self, n: int) -> None:
        path = self.seed_dir / "nights" / f"night_{n}.yaml"
        if not path.is_file():
            raise WorldError(f"missing {path}")
        data = yaml.safe_load(path.read_text())
        for change in data.get("changes", []):
            self._apply(change)
        self.night = n
        self.date = data["date"]

    def _apply(self, change: dict[str, Any]) -> None:
        op = change["op"]
        if op == "add_cluster":
            c = copy.deepcopy(change["cluster"])
            if c["id"] in self.clusters:
                raise WorldError(f"add_cluster: {c['id']} already exists")
            self.clusters[c["id"]] = c
            return
        cid = change["cluster"]
        if cid not in self.clusters:
            raise WorldError(f"{op}: unknown cluster {cid}")
        cluster = self.clusters[cid]
        if op == "remove_cluster":
            del self.clusters[cid]
        elif op == "set_control_plane":
            cluster["control_plane"] = change["version"]
        elif op == "set_fault":
            cluster["fault"] = change["fault"]
        elif op == "set_pool":
            pools = {p["name"]: p for p in cluster["node_pools"]}
            pool = pools.get(change["pool"])
            if pool is None:
                cluster["node_pools"].append(
                    {"name": change["pool"], "versions": dict(change["versions"]), "not_ready": 0}
                )
            else:
                pool["versions"] = dict(change["versions"])
        elif op == "remove_pool":
            cluster["node_pools"] = [p for p in cluster["node_pools"] if p["name"] != change["pool"]]
        else:
            raise WorldError(f"unknown change op {op!r}")

    # ---------------------------------------------------------------- inventory

    def inventory(self) -> dict[str, Any]:
        keys = ("id", "name", "provider", "environment", "region", "team")
        return {"clusters": [{k: c[k] for k in keys} for c in self.clusters.values()]}

    def write_inventory(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# Effective inventory written by the mock world: night {self.night} ({self.date}).\n"
            "# Do not edit; regenerate with `drift mocks reset` / `drift mocks night N`.\n"
        )
        tmp = path.with_suffix(".tmp")
        tmp.write_text(header + yaml.safe_dump(self.inventory(), sort_keys=False))
        tmp.replace(path)

    # --------------------------------------------------------- k8s-shaped payloads

    def version_payload(self, cid: str) -> dict[str, Any]:
        c = self.clusters[cid]
        git = c["control_plane"]
        m = re.match(r"v?(\d+)\.(\w+)", git)
        major, minor = (m.group(1), m.group(2)) if m else ("", "")
        if c["provider"] == "eks" and minor.isdigit():
            minor += "+"  # EKS reports e.g. "31+"
        return {
            "major": major,
            "minor": minor,
            "gitVersion": git,
            "gitCommit": hashlib.sha1(git.encode()).hexdigest(),
            "gitTreeState": "clean",
            "buildDate": "2026-08-20T10:00:00Z",
            "goVersion": "go1.24.6",
            "compiler": "gc",
            "platform": "linux/amd64",
        }

    def node_items(self, cid: str) -> list[dict[str, Any]]:
        c = self.clusters[cid]
        label_key = self.pool_labels[c["provider"]]
        items: list[dict[str, Any]] = []
        for pool in c["node_pools"]:
            not_ready_left = int(pool.get("not_ready", 0))
            idx = 0
            for version, count in pool["versions"].items():
                for _ in range(int(count)):
                    ready = not_ready_left <= 0
                    not_ready_left -= 0 if ready else 1
                    items.append(_node(c, pool["name"], idx, version, label_key, ready))
                    idx += 1
        return items


def _node(c: dict[str, Any], pool: str, idx: int, kubelet: str, label_key: str, ready: bool) -> dict[str, Any]:
    h = hashlib.sha1(f"{c['id']}/{pool}/{idx}".encode()).hexdigest()
    if c["provider"] == "eks":
        name = f"ip-10-{int(h[:2], 16)}-{int(h[2:4], 16)}-{int(h[4:6], 16)}.{c['region']}.compute.internal"
    elif c["provider"] == "gke":
        name = f"gke-{c['name']}-{pool}-{h[:8]}-{h[8:12]}"
    else:
        name = f"{c['name']}-{pool}-{idx:03d}"
    return {
        "metadata": {
            "name": name,
            "uid": f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}",
            "labels": {
                label_key: pool,
                "kubernetes.io/os": "linux",
                "kubernetes.io/arch": "amd64",
                "topology.kubernetes.io/region": c["region"],
            },
        },
        "status": {
            "conditions": [
                {"type": "MemoryPressure", "status": "False", "reason": "KubeletHasSufficientMemory"},
                {
                    "type": "Ready",
                    "status": "True" if ready else "False",
                    "reason": "KubeletReady" if ready else "KubeletNotReady",
                },
            ],
            "nodeInfo": {
                "kubeletVersion": kubelet,
                "kubeProxyVersion": kubelet,
                "containerRuntimeVersion": "containerd://1.7.27",
                "osImage": "Ubuntu 24.04.2 LTS",
                "architecture": "amd64",
            },
        },
    }
