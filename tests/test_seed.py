"""Seed data conformance: SD-1..SD-8, MS-2, and that generated files match scripts/build_seed.py."""

import importlib.util
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import pytest
import yaml

from drift.domain.enums import Rule

REPO = Path(__file__).resolve().parents[1]
SEED = REPO / "seed"
EXPECTED = REPO / "evals" / "expected"
N0 = date(2026, 10, 1)


def _y(path: Path):
    return yaml.safe_load(path.read_text())


@pytest.fixture(scope="module")
def world():
    return _y(SEED / "world.yaml")


@pytest.fixture(scope="module")
def clusters(world):
    return {c["id"]: c for c in world["clusters"]}


@pytest.fixture(scope="module")
def matrix():
    return _y(SEED / "matrix.yaml")


@pytest.fixture(scope="module")
def findings0():
    return _y(EXPECTED / "findings_night_0.yaml")["findings"]


def _minor(raw: str) -> str | None:
    m = re.match(r"v?(\d+\.\d+)\.\d+", raw)
    return m.group(1) if m else None


def test_generated_files_are_up_to_date(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("build_seed", REPO / "scripts" / "build_seed.py")
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "build_seed", mod)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "SEED", tmp_path / "seed")
    monkeypatch.setattr(mod, "EXPECTED", tmp_path / "expected")
    mod.main()
    for fresh in (tmp_path / "seed").rglob("*.yaml"):
        committed = SEED / fresh.relative_to(tmp_path / "seed")
        assert committed.read_text() == fresh.read_text(), f"{committed} is stale; run scripts/build_seed.py"
    for fresh in (tmp_path / "expected").rglob("*.yaml"):
        committed = EXPECTED / fresh.name
        assert committed.read_text() == fresh.read_text(), f"{committed} is stale; run scripts/build_seed.py"


def test_sd1_inventory_shape(clusters):
    assert len(clusters) == 80
    assert {c["provider"] for c in clusters.values()} == {"eks", "gke", "self-managed"}
    assert {c["environment"] for c in clusters.values()} == {"prod", "staging", "dev"}
    names = [c["name"] for c in clusters.values()]
    assert len(set(names)) == 80
    assert all(c["team"] and c["region"] for c in clusters.values())
    inv = _y(SEED / "inventory.yaml")["clusters"]
    assert [c["id"] for c in inv] == list(clusters)


def test_sd2_matrix(matrix):
    assert "ILLUSTRATIVE" in (SEED / "matrix.yaml").read_text().splitlines()[1]
    assert "NOT an authoritative" in matrix["disclaimer"]
    assert matrix["version"]
    for provider, rows in matrix["providers"].items():
        assert len(rows) >= 5, provider
        states = set()
        for r in rows:
            eos, eol = date.fromisoformat(r["end_of_standard_support"]), date.fromisoformat(r["end_of_life"])
            states.add("eol" if eol <= N0 else "extended" if eos <= N0 else "supported")
        expected = {"eol", "supported"} | ({"extended"} if provider != "self-managed" else set())
        assert expected <= states, (provider, states)


def test_sd3_rule_coverage_and_compliant_count(clusters, findings0):
    per_rule = defaultdict(set)
    for f in findings0:
        per_rule[f["rule"]].add(f["cluster"])
    for rule in Rule:
        assert len(per_rule[rule.value]) >= 3, f"{rule} triggered by {len(per_rule[rule.value])} clusters"
    compliant = set(clusters) - {f["cluster"] for f in findings0}
    assert len(compliant) >= 30


def _eol_days(matrix, provider, minor):
    for r in matrix["providers"][provider]:
        if r["minor"] == minor:
            return (
                (date.fromisoformat(r["end_of_standard_support"]) - N0).days,
                (date.fromisoformat(r["end_of_life"]) - N0).days,
            )
    return None


def test_sd4_date_boundaries_both_sides(clusters, matrix, findings0):
    by_cluster = defaultdict(set)
    for f in findings0:
        by_cluster[f["cluster"]].add(f["rule"])
    eos_days, eol_days = defaultdict(list), defaultdict(list)
    for cid, c in clusters.items():
        minor = _minor(c["control_plane"])
        d = _eol_days(matrix, c["provider"], minor) if minor else None
        if d:
            eos_days[d[0]].append(cid)
            eol_days[d[1]].append(cid)
    # EOL yesterday / today -> CP_EOL; tomorrow -> not CP_EOL
    for day in (-1, 0):
        assert any("CP_EOL" in by_cluster[c] for c in eol_days[day]), day
    assert eol_days[1] and all("CP_EOL" not in by_cluster[c] for c in eol_days[1])
    # EOS at window-1 and window -> CP_EOL_SOON; window+1 -> nothing
    for day in (59, 60):
        assert any("CP_EOL_SOON" in by_cluster[c] for c in eos_days[day]), day
    assert eos_days[61] and all(not by_cluster[c] for c in eos_days[61])
    # EOS yesterday / today -> CP_UNSUPPORTED; tomorrow -> CP_EOL_SOON
    for day in (-1, 0):
        assert any("CP_UNSUPPORTED" in by_cluster[c] for c in eos_days[day]), day
    assert any("CP_EOL_SOON" in by_cluster[c] for c in eos_days[1])


def test_sd5_skew_boundaries(clusters, findings0):
    rules = defaultdict(set)
    for f in findings0:
        rules[f["cluster"]].add(f["rule"])
    gaps: dict[int, list[str]] = defaultdict(list)
    for cid, c in clusters.items():
        cp = _minor(c["control_plane"])
        if not cp:
            continue
        for pool in c["node_pools"]:
            for v in pool["versions"]:
                m = _minor(v)
                if m and m.split(".")[0] == cp.split(".")[0]:
                    gaps[int(cp.split(".")[1]) - int(m.split(".")[1])].append(cid)
    assert any("NODE_SKEW_EXCEEDED" not in rules[c] for c in gaps[3]), "exactly-at-skew pool must exist"
    assert any("NODE_SKEW_EXCEEDED" in rules[c] for c in gaps[4])
    assert any("NODE_NEWER_THAN_CP" in rules[c] for c in gaps[-1])


def test_sd6_structural_cases(clusters, findings0):
    pool_findings = defaultdict(set)
    for f in findings0:
        if f["subject"] not in ("control-plane", "cluster"):
            pool_findings[f["cluster"]].add(f["subject"])
    assert any(len(c["node_pools"]) >= 3 and len(pool_findings[cid]) == 1 for cid, c in clusters.items())
    assert any(len(p["versions"]) > 1 for c in clusters.values() for p in c["node_pools"])
    sizes = {cid: sum(sum(p["versions"].values()) for p in c["node_pools"]) for cid, c in clusters.items()}
    assert 0 in sizes.values()
    assert max(sizes.values()) >= 200


def test_sd7_multi_night_sequences():
    scenario = _y(SEED / "scenario.yaml")
    assert len(scenario["sequences"]) >= 5
    tags = Counter()
    for n in range(1, len(scenario["nights"])):
        for t in _y(EXPECTED / f"tags_night_{n}.yaml")["tags"]:
            tags[t["tag"]] += 1
    for tag in ("NEW", "ONGOING", "WORSENED", "RESOLVED", "NEW_CLUSTER"):
        assert tags[tag] > 0, tag
    # was reachable, now unreachable
    n1 = {(f["cluster"], f["rule"]) for f in _y(EXPECTED / "findings_night_1.yaml")["findings"]}
    n2 = _y(EXPECTED / "tags_night_2.yaml")["tags"]
    assert any(t["rule"] == "UNREACHABLE" and t["tag"] == "NEW" and (t["cluster"], "UNREACHABLE") not in n1 for t in n2)


def test_sd8_lookalikes(clusters):
    names = [c["name"] for c in clusters.values()]
    assert any(a != b and b.startswith(a) and b[len(a) :].isdigit() for a in names for b in names)
    minors = {_minor(c["control_plane"]) for c in clusters.values()}
    assert {"1.3", "1.30"} <= minors


def test_ms2_provider_suffixes(clusters):
    raws = [c["control_plane"] for c in clusters.values()]
    assert any(re.fullmatch(r"v\d+\.\d+\.\d+-eks-[0-9a-f]{7}", r) for r in raws)
    assert any(re.fullmatch(r"v\d+\.\d+\.\d+-gke\.\d+", r) for r in raws)
    assert any(re.fullmatch(r"v\d+\.\d+\.\d+", r) for r in raws)
