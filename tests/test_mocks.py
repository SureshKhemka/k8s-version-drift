"""Mock cluster API and mock channel behaviour (MS-1..MS-6)."""

import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from drift_mocks.channel import ChannelMockState
from drift_mocks.channel import create_app as channel_app
from drift_mocks.k8s_api import K8sMockState
from drift_mocks.k8s_api import create_app as k8s_app

SEED = Path(__file__).resolve().parents[1] / "seed"


@pytest.fixture
def k8s(tmp_path):
    state = K8sMockState(SEED, tmp_path / "state", SEED / "faults.yaml", timeout_sleep_s=0.01)
    return TestClient(k8s_app(state)), state


@pytest.fixture
def channel(tmp_path):
    state = ChannelMockState(tmp_path / "state", None, timeout_sleep_s=0.01)
    return TestClient(channel_app(state)), state


# ------------------------------------------------------------------------ k8s shapes


def test_version_payload_is_kubernetes_shaped(k8s):
    client, _ = k8s
    body = client.get("/clusters/c001/version").json()
    assert body["gitVersion"].startswith("v1.35.4-eks-")
    assert body["major"] == "1" and body["minor"] == "35+"
    assert {"gitCommit", "platform", "goVersion"} <= set(body)
    gke = client.get("/clusters/c033/version").json()
    assert gke["gitVersion"] == "v1.29.14-gke.2400" and gke["minor"] == "29"


def test_node_list_shape_and_labels(k8s):
    client, _ = k8s
    body = client.get("/clusters/c012/api/v1/nodes").json()
    assert body["kind"] == "NodeList" and body["apiVersion"] == "v1"
    pools = {n["metadata"]["labels"]["eks.amazonaws.com/nodegroup"] for n in body["items"]}
    assert pools == {"ng-general", "ng-legacy", "ng-spot"}
    node = body["items"][0]
    assert node["status"]["nodeInfo"]["kubeletVersion"].startswith("v1.35.4-eks-")
    assert any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"])


def test_not_ready_nodes(k8s):
    client, _ = k8s
    items = client.get("/clusters/c020/api/v1/nodes").json()["items"]
    not_ready = [n for n in items for c in n["status"]["conditions"] if c["type"] == "Ready" and c["status"] != "True"]
    assert len(not_ready) == 1


def test_pagination_on_large_cluster(k8s):
    client, _ = k8s
    seen, token, pages = [], None, 0
    while True:
        params = {"limit": 100} | ({"continue": token} if token else {})
        body = client.get("/clusters/c024/api/v1/nodes", params=params).json()
        seen += [n["metadata"]["name"] for n in body["items"]]
        pages += 1
        token = body["metadata"].get("continue")
        if not token:
            break
    assert pages == 4 and len(seen) == 350 and len(set(seen)) == 350


def test_zero_node_cluster(k8s):
    client, _ = k8s
    assert client.get("/clusters/c025/api/v1/nodes").json()["items"] == []


def test_unknown_cluster_and_path(k8s):
    client, _ = k8s
    assert client.get("/clusters/nope/version").status_code == 404
    assert client.get("/clusters/c001/api/v1/pods").status_code == 404


# ---------------------------------------------------------------- read-only record (MS-4)


def test_write_calls_are_rejected_and_recorded(k8s):
    client, state = k8s
    assert client.get("/clusters/c001/version").status_code == 200
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert client.request(method, "/clusters/c001/api/v1/nodes/x").status_code == 405
    calls = client.get("/_admin/calls").json()
    assert [c["method"] for c in calls] == ["GET", "POST", "PUT", "PATCH", "DELETE"]
    assert all("authorization" not in str(c).lower() for c in calls)
    assert (state.state_dir / "k8s_calls.jsonl").read_text().count("\n") == 5
    client.delete("/_admin/calls")
    assert client.get("/_admin/calls").json() == []


# --------------------------------------------------------------------- faults (MS-5)


def test_world_faults(k8s):
    client, _ = k8s
    assert client.get("/clusters/c015/version").status_code == 401
    assert client.get("/clusters/c065/version").status_code == 500
    malformed = client.get("/clusters/c016/version")
    assert malformed.status_code == 200 and malformed.text.startswith("<html>")
    assert client.get("/clusters/c047/version").status_code == 504


def test_scripted_fault_then_recovers(k8s):
    client, state = k8s
    codes = [client.get("/clusters/c021/version").status_code for _ in range(3)]
    assert codes == [504, 504, 200]
    state.set_night(0)  # counters reset on time travel
    assert client.get("/clusters/c021/version").status_code == 504


def test_latency_fault(k8s):
    client, _ = k8s
    t = time.perf_counter()
    assert client.get("/clusters/c022/version").status_code == 200
    assert time.perf_counter() - t >= 0.3


def test_runtime_fault_override_is_deterministic(k8s):
    client, _ = k8s
    client.put("/_admin/faults", json={"clusters": {"c001": {"error_rate": 0.5, "seed": 7}}})
    first = [client.get("/clusters/c001/version").status_code for _ in range(20)]
    client.put("/_admin/faults", json={"clusters": {"c001": {"error_rate": 0.5, "seed": 7}}})
    second = [client.get("/clusters/c001/version").status_code for _ in range(20)]
    assert first == second and {200, 500} == set(first)
    client.put("/_admin/faults", json={"clusters": {}})
    assert client.get("/clusters/c015/version").status_code == 401  # world fault back


# ------------------------------------------------------------------- time travel (MS-6)


def test_night_advance_and_reset(k8s):
    client, state = k8s
    assert client.get("/_admin/state").json() == {"night": 0, "date": "2026-10-01", "clusters": 80}
    assert client.post("/_admin/night/2").json() == {"night": 2, "date": "2026-10-03", "clusters": 81}
    assert client.get("/clusters/c003/version").json()["gitVersion"].startswith("v1.35.4")
    assert client.get("/clusters/c014/version").status_code == 504
    assert client.get("/clusters/c081/version").status_code == 200
    inv = yaml.safe_load(state.inventory_path.read_text())
    assert len(inv["clusters"]) == 81
    assert client.post("/_admin/night/99").status_code == 400
    client.post("/_admin/reset")
    assert client.get("/_admin/state").json()["night"] == 0
    assert len(yaml.safe_load(state.inventory_path.read_text())["clusters"]) == 80
    assert client.get("/_admin/calls").json() == []


def test_night_persists_across_restart(tmp_path):
    state = K8sMockState(SEED, tmp_path, None, 0.01)
    state.set_night(3)
    again = K8sMockState(SEED, tmp_path, None, 0.01)
    assert again.world.night == 3 and again.world.date == "2026-10-05"


def test_every_night_loads():
    from drift_mocks.world import World

    for n in range(5):
        assert World.load(SEED, n).night == n


# ------------------------------------------------------------------------- channel (MS-3)


def test_channel_accepts_and_stores(channel):
    client, _ = channel
    r = client.post("/webhook/T123-secret", json={"text": "hello *team*"})
    assert r.status_code == 200 and r.text == "ok"
    msgs = client.get("/_admin/messages").json()
    assert [m["text"] for m in msgs] == ["hello *team*"]
    calls = client.get("/_admin/calls").json()
    assert "T123-secret" not in str(calls)


def test_channel_rejects_bad_payload(channel):
    client, _ = channel
    assert client.post("/webhook/x", content=b"not json").status_code == 400
    assert client.post("/webhook/x", json={"txt": "hi"}).status_code == 400
    assert client.get("/_admin/messages").json() == []


def test_channel_scripted_failures_then_ok(channel):
    client, _ = channel
    client.put("/_admin/faults", json={"script": ["http_error:500", "http_error:503"]})
    codes = [client.post("/webhook/x", json={"text": "m"}).status_code for _ in range(3)]
    assert codes == [500, 503, 200]
    assert len(client.get("/_admin/messages").json()) == 1


def test_channel_reset(channel):
    client, _ = channel
    client.post("/webhook/x", json={"text": "m"})
    client.post("/_admin/reset")
    assert client.get("/_admin/messages").json() == []
    assert client.get("/_admin/calls").json() == []
