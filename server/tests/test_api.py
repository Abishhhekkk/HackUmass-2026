import json
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "edge"))

from rescue_api.app import create_app  # noqa: E402
from rescue_api.storage import Storage  # noqa: E402
from rescue_edge.aggregate import Aggregator  # noqa: E402
from rescue_edge.geolocate import offset_to_latlon  # noqa: E402
from rescue_edge.snapshot import SnapshotWriter, snapshot_dir  # noqa: E402
from rescue_edge.uploader import Uploader  # noqa: E402

LAT, LON = 42.3893, -72.5283


def make_snapshots(root, n_people_per_version=(2, 3)):
    agg = Aggregator()
    w = SnapshotWriter(root, "m1", "drone-01")
    tid = 0
    for n in n_people_per_version:
        while len(agg.finalize().people) < n:
            tid += 1
            lat, lon = offset_to_latlon(LAT, LON, 20 * tid, 0)
            for i in range(5):
                agg.add(tid * 10 + i * 0.1, tid, 0.8, lat, lon, 3.0, None)
                agg.note_frame(tid * 10 + i * 0.1)
        w.write_if_changed(agg.finalize())
    return w


def files_for(root, version, corrupt=False):
    d = snapshot_dir(root, "m1", version)
    m = json.loads((d / "manifest.json").read_text())
    out = [("manifest", ("manifest.json", (d / "manifest.json").read_bytes(), "application/json"))]
    for f in m["files"]:
        data = (d / f["name"]).read_bytes()
        out.append(("files", (f["name"], data[:10] if corrupt and f["name"] == "sightings.csv" else data)))
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INGEST_API_KEY", "secret")
    store = Storage(f"sqlite:///{tmp_path}/cloud.db", tmp_path / "cloud")
    client = TestClient(create_app(store))
    edge_root = tmp_path / "edge"
    make_snapshots(edge_root)
    return client, edge_root, store


H = {"X-API-Key": "secret"}


def test_auth_required(env):
    client, root, _ = env
    assert client.post("/api/v1/snapshots", files=files_for(root, 1)).status_code == 401


def test_accept_duplicate_stale(env):
    client, root, _ = env
    r = client.post("/api/v1/snapshots", files=files_for(root, 2), headers=H)
    assert r.status_code == 201 and r.json()["status"] == "accepted"
    r = client.post("/api/v1/snapshots", files=files_for(root, 2), headers=H)
    assert r.json()["status"] == "duplicate"
    r = client.post("/api/v1/snapshots", files=files_for(root, 1), headers=H)
    assert r.json() == {"status": "stale", "latest_version": 2}
    latest = client.get("/api/v1/missions/m1/latest").json()
    assert latest["manifest"]["version"] == 2
    assert latest["manifest"]["summary"]["estimated_people"] == 3
    assert len(client.get("/api/v1/missions").json()) == 1


def test_truncated_upload_rejected_and_nothing_stored(env):
    client, root, _ = env
    r = client.post("/api/v1/snapshots", files=files_for(root, 1, corrupt=True), headers=H)
    assert r.status_code == 422 and "checksum" in r.text
    assert client.get("/api/v1/missions/m1/latest").status_code == 404


def test_people_near_and_reports(env, monkeypatch):
    client, root, _ = env
    client.post("/api/v1/snapshots", files=files_for(root, 2), headers=H)
    near = client.get("/api/v1/missions/m1/people/near", params={"lat": LAT, "lon": LON, "radius_m": 30}).json()
    assert [p["distance_m"] < 30 for p in near] == [True]
    assert "Mission report" in client.get("/api/v1/missions/m1/report").text
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    r = client.post("/api/v1/missions/m1/ai-report").json()
    assert r["generated_by"] == "deterministic" and r["human_review_required"]


def test_agent_tool_loop_with_fake_llm(env):
    from langchain_core.messages import AIMessage

    from rescue_api.agent import generate_report

    client, root, store = env
    client.post("/api/v1/snapshots", files=files_for(root, 2), headers=H)

    class FakeLLM:
        def __init__(self):
            self.step = 0

        def bind_tools(self, tools):
            self.tools = {t.name for t in tools}
            return self

        def invoke(self, msgs):
            self.step += 1
            if self.step == 1:
                return AIMessage("", tool_calls=[
                    {"name": "get_mission_summary", "args": {}, "id": "1"},
                    {"name": "get_densest_cells", "args": {"limit": 99}, "id": "2"},
                    {"name": "drop_tables", "args": {}, "id": "3"}])
            tool_text = " ".join(m.content for m in msgs if m.type == "tool")
            assert "not allowed" in tool_text and '"estimated_people": 3' in tool_text
            return AIMessage("3 estimated people in two cells.")

    row = store.snapshot_row("m1")
    r = generate_report(store, "m1", row, "fallback", llm=FakeLLM())
    assert r["generated_by"] == "gemini"
    assert r["report"].endswith("Human review required - estimates only.")
    assert [c["tool"] for c in r["tool_calls"]] == ["get_mission_summary", "get_densest_cells", "drop_tables"]


def test_uploader_sends_latest_after_outage(tmp_path, monkeypatch):
    """Real HTTP: outage -> versions pile up -> link returns -> only newest is sent, once."""
    monkeypatch.setenv("INGEST_API_KEY", "secret")
    store = Storage(f"sqlite:///{tmp_path}/cloud.db", tmp_path / "cloud")
    server = uvicorn.Server(uvicorn.Config(create_app(store), host="127.0.0.1", port=8765, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    try:
        edge = tmp_path / "edge"
        flag = tmp_path / "OFFLINE"
        flag.touch()
        make_snapshots(edge, (1, 2, 3))
        up = Uploader(edge, "m1", "http://127.0.0.1:8765", api_key="secret",
                      offline_flag_file=str(flag), base_backoff_s=0.1, poll_s=0.1)
        up.corrupt_next = True  # first attempt after reconnect is truncated mid-transfer
        up.start()
        time.sleep(0.4)
        assert up.status()["online"] is False and up.status()["acked_version"] == 0
        flag.unlink()
        for _ in range(50):
            if up.status()["acked_version"] == 3:
                break
            time.sleep(0.1)
        assert up.status()["acked_version"] == 3
        assert [m["latest_version"] for m in store.missions()] == [3]
        with store.tx() as cur:
            cur.execute("SELECT version FROM snapshots")
            assert [r[0] for r in cur.fetchall()] == [3]  # v1, v2 skipped; corrupt try rejected
        up.stop()
    finally:
        server.should_exit = True
