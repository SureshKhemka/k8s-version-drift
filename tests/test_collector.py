"""Collection against the mock cluster API (FR-2..FR-7), in-process over httpx's ASGI transport."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import yaml

from drift.collect.collector import TIME_BUDGET_EXCEEDED, Collector, endpoint_for
from drift.collect.k8s_client import ReadOnlyTransport, ReadOnlyViolation
from drift.collect.sources import SourceError, load_inventory, load_matrix, select
from drift.config.models import ClustersConfig, SourceConfig
from drift.domain.models import ClusterRef, Inventory, snapshot
from drift_mocks.faults import FaultSpec
from drift_mocks.k8s_api import K8sMockState, create_app

REPO = Path(__file__).resolve().parents[1]
SEED = REPO / "seed"
EXPECTED = REPO / "evals" / "expected"
POOL_LABELS = ("eks.amazonaws.com/nodegroup", "cloud.google.com/gke-nodepool", "node.kubernetes.io/pool")


def _cfg(**over) -> ClustersConfig:
    base = dict(
        concurrency=16,
        timeout_s=1.0,
        retries=2,
        retry_backoff_s=0.0,
        time_budget_s=20,
        endpoint_template="http://mock/clusters/{id}",
        node_pool_labels=POOL_LABELS,
        page_size=100,
    )
    return ClustersConfig(**(base | over))


@pytest.fixture
def mock(tmp_path):
    state = K8sMockState(SEED, tmp_path / "state", None, timeout_sleep_s=5)
    return state


def _collector(state, cfg=None, environ=None):
    app = create_app(state)
    return Collector(cfg or _cfg(), environ=environ or {}, transport_factory=lambda: httpx.ASGITransport(app=app))


def _inventory(state) -> Inventory:
    return load_inventory(SourceConfig(type="file", path=state.inventory_path))


def _expected_unreachable(night: int) -> dict[str, str]:
    rows = yaml.safe_load((EXPECTED / f"findings_night_{night}.yaml").read_text())["findings"]
    return {r["cluster"]: r["observed"] for r in rows if r["rule"] == "UNREACHABLE"}


async def test_night0_full_collection_matches_expected_reachability(mock):
    inv = _inventory(mock)
    results = await _collector(mock).collect(inv.clusters)
    assert [r.cluster_id for r in results] == [c.id for c in inv.clusters]  # FR-5: none dropped
    unreachable = {r.cluster_id: r.reason for r in results if not r.reachable}
    assert unreachable == _expected_unreachable(0)
    # FR-3 / EV-5 precursor: only GETs reached the mock
    assert {c["method"] for c in mock.calls.calls} == {"GET"}
    # FR-32: no endpoints in the raw fetch records
    dumped = json.dumps([r.model_dump(mode="json") for r in results])
    assert "http://" not in dumped and "mock/clusters" not in dumped


async def test_retry_behaviour(mock):
    inv = _inventory(mock).by_id()
    res = {r.cluster_id: r for r in await _collector(mock).collect([inv[c] for c in ("c015", "c021", "c065")])}
    assert len(res["c015"].attempts) == 1  # auth errors are not retried
    assert res["c015"].attempts[0].status_code == 401
    assert res["c021"].reachable and [a.outcome for a in res["c021"].attempts] == ["timeout", "timeout", "ok"]
    assert [a.outcome for a in res["c065"].attempts] == ["HTTP 500"] * 3


async def test_pagination_and_node_details(mock):
    inv = _inventory(mock).by_id()
    (big,) = await _collector(mock).collect([inv["c024"]])
    assert big.reachable and len(big.nodes) == 350 and big.requests == 1 + 4
    assert {n.pool for n in big.nodes} == {"ng-general", "ng-memory", "ng-spot"}
    (nr,) = await _collector(mock).collect([inv["c020"]])
    assert sum(not n.ready for n in nr.nodes) == 1
    (empty,) = await _collector(mock).collect([inv["c025"]])
    assert empty.reachable and empty.nodes == ()


async def test_time_budget_marks_unfinished_clusters(mock):
    inv = _inventory(mock).by_id()
    mock.overrides = {"c001": FaultSpec(latency_ms=3000)}
    cfg = _cfg(time_budget_s=0.5, timeout_s=5)
    results = {r.cluster_id: r for r in await _collector(mock, cfg).collect([inv["c001"], inv["c023"]])}
    assert results["c001"].reason == TIME_BUDGET_EXCEEDED and not results["c001"].reachable
    assert results["c023"].reachable


async def test_time_budget_keeps_attempts_made_so_far(mock):
    inv = _inventory(mock).by_id()
    cfg = _cfg(time_budget_s=1.5, timeout_s=1.0)
    (res,) = await _collector(mock, cfg).collect([inv["c047"]])  # times out every attempt
    assert res.reason == TIME_BUDGET_EXCEEDED
    assert [a.outcome for a in res.attempts] == ["timeout"]


async def test_concurrency_limit_is_respected():
    in_flight, peak = 0, 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        if request.url.path.endswith("/version"):
            return httpx.Response(200, json={"gitVersion": "v1.35.4"})
        return httpx.Response(200, json={"kind": "NodeList", "metadata": {}, "items": []})

    clusters = [
        ClusterRef(id=f"x{i}", name=f"x-{i}", provider="eks", environment="dev", region="r", team="t")
        for i in range(12)
    ]
    col = Collector(_cfg(concurrency=3), environ={}, transport_factory=lambda: httpx.MockTransport(handler))
    results = await col.collect(clusters)
    assert all(r.reachable for r in results) and peak == 3


async def test_read_only_transport_refuses_writes():
    sent = []
    inner = httpx.MockTransport(lambda req: sent.append(req) or httpx.Response(200))
    async with httpx.AsyncClient(transport=ReadOnlyTransport(inner)) as http:
        assert (await http.get("http://x/version")).status_code == 200
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with pytest.raises(ReadOnlyViolation):
                await http.request(method, "http://x/api/v1/nodes/n1")
    assert [r.method for r in sent] == ["GET"]


async def test_bearer_token_sent_but_never_recorded(mock):
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        if request.url.path.endswith("/version"):
            return httpx.Response(200, json={"gitVersion": "v1.35.4"})
        return httpx.Response(200, json={"kind": "NodeList", "metadata": {}, "items": []})

    cfg = _cfg(token_env="K8S_TOKEN")
    col = Collector(cfg, environ={"K8S_TOKEN": "s3cr3t-token"}, transport_factory=lambda: httpx.MockTransport(handler))
    (res,) = await col.collect([ClusterRef(id="x", name="x", provider="eks", environment="dev", region="r", team="t")])
    assert seen == ["Bearer s3cr3t-token", "Bearer s3cr3t-token"]
    assert "s3cr3t" not in res.model_dump_json()


async def test_malformed_node_items_are_unreachable():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/version"):
            return httpx.Response(200, json={"gitVersion": "v1.35.4"})
        return httpx.Response(200, json={"kind": "NodeList", "metadata": {}, "items": [{"metadata": {}}]})

    col = Collector(_cfg(retries=0), environ={}, transport_factory=lambda: httpx.MockTransport(handler))
    (res,) = await col.collect([ClusterRef(id="x", name="x", provider="eks", environment="dev", region="r", team="t")])
    assert res.reason == "malformed response"


async def test_connection_error_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom at http://10.0.0.1:6443")

    col = Collector(_cfg(retries=1), environ={}, transport_factory=lambda: httpx.MockTransport(handler))
    (res,) = await col.collect([ClusterRef(id="x", name="x", provider="eks", environment="dev", region="r", team="t")])
    assert res.reason == "connection error" and len(res.attempts) == 2
    assert "10.0.0.1" not in res.model_dump_json()


async def test_night2_scenario(mock):
    mock.set_night(2)
    inv = _inventory(mock)
    assert "c081" in inv.by_id()
    results = {r.cluster_id: r for r in await _collector(mock).collect(inv.clusters)}
    assert {cid for cid, r in results.items() if not r.reachable} == set(_expected_unreachable(2))
    assert results["c003"].control_plane_version.startswith("v1.35.4")


async def test_snapshot_normalisation(mock):
    inv = _inventory(mock).by_id()
    res = {r.cluster_id: r for r in await _collector(mock).collect([inv[c] for c in ("c002", "c017", "c019", "c048")])}
    s = snapshot(inv["c002"], res["c002"])
    assert str(s.control_plane.parsed) == "1.28.15" and s.control_plane.raw.endswith("-eks-1b99f80")
    assert not snapshot(inv["c017"], res["c017"]).control_plane.ok
    mixed = snapshot(inv["c019"], res["c019"]).pools[0]
    assert sorted({str(v.parsed) for _, v in mixed.nodes}) == ["1.34.6", "1.35.4"]
    gpu = {p.name: p for p in snapshot(inv["c048"], res["c048"]).pools}["gpu-pool"]
    assert all(not v.ok and v.raw == "unknown" for _, v in gpu.nodes)


# ----------------------------------------------------------------------------- sources


def test_matrix_loads_and_answers():
    m = load_matrix(SourceConfig(type="file", path=SEED / "matrix.yaml"))
    assert m.version.startswith("illustrative")
    assert m.entry("eks", "1.28").latest == 15
    assert m.entry("gke", "1.36") is None
    assert m.known_range("self-managed") == "1.29-1.35"


def test_bad_matrix_rejected(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "version": "x",
                "providers": {
                    "eks": [
                        {
                            "minor": "1.30",
                            "latest_patch": "1.31.2",
                            "end_of_standard_support": "2026-01-01",
                            "end_of_life": "2026-06-01",
                        }
                    ]
                },
            }
        )
    )
    with pytest.raises(SourceError, match="does not belong"):
        load_matrix(SourceConfig(type="file", path=p))


def test_duplicate_inventory_rejected(tmp_path):
    p = tmp_path / "i.yaml"
    c = {"id": "a", "name": "a", "provider": "eks", "environment": "dev", "region": "r", "team": "t"}
    p.write_text(yaml.safe_dump({"clusters": [c, c | {"name": "b"}]}))
    with pytest.raises(SourceError, match="duplicate cluster id"):
        load_inventory(SourceConfig(type="file", path=p))


def test_missing_source_is_clear(tmp_path):
    with pytest.raises(SourceError, match="cannot read"):
        load_inventory(SourceConfig(type="file", path=tmp_path / "nope.yaml"))


def test_select_subset(mock):
    inv = _inventory(mock)
    assert [c.id for c in select(inv, ["c001", "payments-prod-11"])] == ["c001", "c002"]
    assert len(select(inv, None)) == 80
    with pytest.raises(SourceError, match="unknown clusters: zzz"):
        select(inv, ["zzz"])


def test_endpoint_resolution():
    c = ClusterRef(id="c1", name="n", provider="eks", environment="dev", region="r", team="t")
    assert endpoint_for(c, _cfg()) == "http://mock/clusters/c1"
    assert endpoint_for(c.model_copy(update={"api_server": "https://real:6443"}), _cfg()) == "https://real:6443"
