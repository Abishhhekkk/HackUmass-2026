import base64
import json
import os
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
from rescue_edge.wire import seal  # noqa: E402

LAT, LON = 42.3893, -72.5283


def make_snapshots(root, n_people_per_version=(2, 3), telemetry=None):
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
        w.write_if_changed(agg.finalize(), telemetry=telemetry)
    return w


def files_for(root, version, corrupt=False, mission_id="m1"):
    d = snapshot_dir(root, mission_id, version)
    m = json.loads((d / "manifest.json").read_text())
    out = [("manifest", ("manifest.json", (d / "manifest.json").read_bytes(), "application/json"))]
    for f in m["files"]:
        data = (d / f["name"]).read_bytes()
        out.append(("files", (f["name"], data[:10] if corrupt and f["name"] == "sightings.csv" else data)))
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INGEST_API_KEY", "secret")
    monkeypatch.delenv("RESCUE_PAYLOAD_KEY", raising=False)  # plaintext mode unless a test opts in
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
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
    client, root, store = env
    client.post("/api/v1/snapshots", files=files_for(root, 2), headers=H)
    near = client.get("/api/v1/missions/m1/people/near", params={"lat": LAT, "lon": LON, "radius_m": 30}).json()
    assert [p["distance_m"] < 30 for p in near] == [True]
    assert "Mission report" in client.get("/api/v1/missions/m1/report").text
    automated = client.get("/api/v1/missions/m1/ai-report").json()
    assert automated["generated_by"] == "deterministic"
    assert automated["human_review_required"] is True
    assert automated["snapshot_version"] == 2
    assert automated["report"]
    assert store._conn.execute("SELECT COUNT(*) FROM heatmap_cells WHERE mission_id='m1'").fetchone()[0] > 0
    assert store._conn.execute("SELECT COUNT(*) FROM people WHERE mission_id='m1'").fetchone()[0] == 3
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    r = client.post("/api/v1/missions/m1/ai-report").json()
    assert r["generated_by"] == "deterministic" and r["human_review_required"]


def test_unlocated_vision_tracks_are_indexed(env):
    client, _, store = env
    root = store.snapshot_dir.parent / "unlocated-edge"
    aggregate = Aggregator(min_frames=3)
    for index in range(3):
        timestamp = index * 0.1
        aggregate.add(timestamp, 9, 0.7, None, None, None, "gnss_fix_invalid")
        aggregate.note_frame(timestamp)
    writer = SnapshotWriter(root, "unlocated-mission", "drone-01")
    assert writer.write_if_changed(aggregate.finalize()) == 1
    upload_files = files_for(root, 1, mission_id="unlocated-mission")
    response = client.post("/api/v1/snapshots", files=upload_files, headers=H)
    assert response.status_code == 201
    row = store._conn.execute(
        "SELECT person_id, confidence, reason FROM unlocated_sightings "
        "WHERE mission_id='unlocated-mission'"
    ).fetchone()
    assert row == ("U001", 0.7, "gnss_fix_invalid")


def test_missing_automated_report_is_generated_on_read(env):
    client, root, store = env
    response = client.post("/api/v1/snapshots", files=files_for(root, 1), headers=H)
    assert response.status_code == 201
    report_file = store.snapshot_dir / "m1" / "v000001" / "ai-report.json"
    report_file.unlink()

    pending = client.get("/api/v1/missions/m1/ai-report")
    assert pending.status_code == 200
    assert pending.json()["status"] == "pending"
    generated = client.get("/api/v1/missions/m1/ai-report").json()
    assert generated["generated_by"] == "deterministic"
    assert generated["snapshot_version"] == 1
    assert generated["report"]


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


def test_gps_and_sighting_tools_are_snapshot_scoped(env):
    from rescue_api.agent import make_tools

    client, root, store = env
    telemetry = {
        "schema_version": 1, "available": True, "observed_at": "2026-01-01T00:00:00Z",
        "source": "edge_telemetry",
        "position": {"lat": LAT, "lon": LON},
        "altitude_agl_m": 30, "heading_deg": 90, "pitch_deg": 1, "roll_deg": -1,
    }
    gps_root = root / "gps"
    make_snapshots(gps_root, (2,), telemetry=telemetry)
    client.post("/api/v1/snapshots", files=files_for(gps_root, 1), headers=H)
    row = store.snapshot_row("m1")
    tools = {tool.name: tool for tool in make_tools(store, "m1", row)}

    gps = tools["get_latest_drone_gps"].invoke({})
    assert gps["available"] is True
    assert gps["position"] == {"lat": LAT, "lon": LON}
    nearby = tools["get_sightings_near_drone"].invoke({"radius_m": 50})
    assert nearby["available"] is True and nearby["sightings"]
    recent = tools["get_recent_sightings"].invoke({"limit": 1})
    assert len(recent) == 1 and "lat" in recent[0] and "lon" in recent[0]
    assert tools["get_unlocated_sightings"].invoke({"limit": 1}) == []


def test_question_agent_calls_read_only_gps_tool(env):
    from langchain_core.messages import AIMessage

    from rescue_api.agent import answer_question

    client, root, store = env
    telemetry = {
        "schema_version": 1, "available": True, "observed_at": "2026-01-01T00:00:00Z",
        "source": "edge_telemetry", "position": {"lat": LAT, "lon": LON},
        "altitude_agl_m": 30, "heading_deg": 90, "pitch_deg": 1, "roll_deg": -1,
    }
    gps_root = root / "question-gps"
    make_snapshots(gps_root, (2,), telemetry=telemetry)
    client.post("/api/v1/snapshots", files=files_for(gps_root, 1), headers=H)

    class FakeLLM:
        def bind_tools(self, tools):
            self.tools = {tool.name for tool in tools}
            return self

        def invoke(self, messages):
            if not any(message.type == "tool" for message in messages):
                return AIMessage("", tool_calls=[
                    {"name": "get_latest_drone_gps", "args": {}, "id": "gps-1"}])
            tool_result = " ".join(message.content for message in messages if message.type == "tool")
            assert f'"lat": {LAT}' in tool_result
            return AIMessage("Latest drone fix is available.")

    result = answer_question(
        store, "m1", store.snapshot_row("m1"), "fallback", "Where is the drone?", llm=FakeLLM())
    assert result["generated_by"] == "gemini"
    assert result["answer"].startswith("Latest drone fix")
    assert result["tool_calls"][0]["tool"] == "get_latest_drone_gps"


def test_ai_status_and_question_fallback(env):
    client, root, _ = env
    client.post("/api/v1/snapshots", files=files_for(root, 1), headers=H)
    status = client.get("/api/v1/ai/status")
    assert status.status_code == 200 and status.json()["configured"] is False
    assert "get_latest_drone_gps" in status.json()["tools"]
    response = client.post("/api/v1/missions/m1/ask", json={"question": "Where is the drone?"})
    assert response.status_code == 200
    assert response.json()["generated_by"] == "deterministic"
    assert response.json()["human_review_required"] is True
    assert "deterministic mission report" in response.json()["answer"]
    assert client.post("/api/v1/missions/m1/ask", json={"question": "  "}).status_code == 422
    assert client.post("/api/v1/missions/m1/ask", json={"question": "x" * 1001}).status_code == 422


@pytest.fixture
def enc_env(tmp_path, monkeypatch):
    """Cloud API that requires compressed + encrypted uploads (RESCUE_PAYLOAD_KEY set)."""
    key = os.urandom(32)
    monkeypatch.setenv("INGEST_API_KEY", "secret")
    monkeypatch.setenv("RESCUE_PAYLOAD_KEY", base64.b64encode(key).decode())
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    store = Storage(f"sqlite:///{tmp_path}/cloud.db", tmp_path / "cloud")
    client = TestClient(create_app(store))
    edge_root = tmp_path / "edge"
    make_snapshots(edge_root)
    return client, edge_root, store, key


def sealed_files_for(root, version, key, tamper_sightings=False):
    out = []
    for field, (name, data, *rest) in files_for(root, version):
        blob = seal(data, key, name)
        if tamper_sightings and name == "sightings.csv":
            blob = blob[:-1]
        out.append((field, (name, blob, *rest)))
    return out


def test_encrypted_upload_is_opened_verified_and_stored_as_plaintext(enc_env):
    client, root, store, key = enc_env
    r = client.post("/api/v1/snapshots", files=sealed_files_for(root, 2, key), headers=H)
    assert r.status_code == 201 and r.json()["status"] == "accepted"
    stored = store.snapshot_dir / "m1" / "v000002"
    plain = snapshot_dir(root, "m1", 2)
    for name in ("sightings.csv", "heatmap.geojson", "report.md", "telemetry.json", "manifest.json"):
        assert (stored / name).read_bytes() == (plain / name).read_bytes()
    assert client.get("/api/v1/missions/m1/latest").json()["manifest"]["summary"]["estimated_people"] == 3
    again = client.post("/api/v1/snapshots", files=sealed_files_for(root, 2, key), headers=H)
    assert again.json()["status"] == "duplicate"  # fresh nonces, same content hash


def test_server_with_key_rejects_plaintext_tampered_and_wrong_key(enc_env):
    client, root, _, key = enc_env
    r = client.post("/api/v1/snapshots", files=files_for(root, 1), headers=H)
    assert r.status_code == 422 and "must be encrypted" in r.text
    r = client.post("/api/v1/snapshots", files=sealed_files_for(root, 1, key, tamper_sightings=True), headers=H)
    assert r.status_code == 422 and "could not open sightings.csv" in r.text
    r = client.post("/api/v1/snapshots", files=sealed_files_for(root, 1, os.urandom(32)), headers=H)
    assert r.status_code == 422
    assert client.get("/api/v1/missions/m1/latest").status_code == 404  # nothing was stored


def test_sealed_upload_to_server_without_key_is_rejected(env):
    client, root, _ = env
    r = client.post("/api/v1/snapshots", files=sealed_files_for(root, 1, os.urandom(32)), headers=H)
    assert r.status_code == 422 and "no RESCUE_PAYLOAD_KEY" in r.text


def test_encrypted_decompression_cap_is_enforced(enc_env, monkeypatch, tmp_path):
    _, root, _, key = enc_env
    # 20 kB comfortably fits the sealed manifest and small files; only the 1 MB report exceeds it
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "20000")
    small = TestClient(create_app(Storage(f"sqlite:///{tmp_path}/small.db", tmp_path / "small-cloud")))
    files = files_for(root, 1)
    files = [(f, (n, seal(b"A" * 1_000_000, key, n), *rest)) if n == "report.md" else (f, (n, seal(b, key, n), *rest))
             for f, (n, b, *rest) in files]
    r = small.post("/api/v1/snapshots", files=files, headers=H)
    assert r.status_code == 422 and "size limit" in r.text


def test_real_http_encrypted_uploader(tmp_path, monkeypatch):
    key = os.urandom(32)
    monkeypatch.setenv("INGEST_API_KEY", "secret")
    monkeypatch.setenv("RESCUE_PAYLOAD_KEY", base64.b64encode(key).decode())
    store = Storage(f"sqlite:///{tmp_path}/cloud.db", tmp_path / "cloud")
    server = uvicorn.Server(uvicorn.Config(create_app(store), host="127.0.0.1", port=8766, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    try:
        edge = tmp_path / "edge"
        make_snapshots(edge, (1, 2))
        up = Uploader(edge, "m1", "http://127.0.0.1:8766", api_key="secret", payload_key=key)
        assert up.upload_once(2)["status"] == "accepted"
        assert [m["latest_version"] for m in store.missions()] == [2]
        plaintext_up = Uploader(edge, "m1", "http://127.0.0.1:8766", api_key="secret")
        with pytest.raises(RuntimeError, match="HTTP 422"):  # server insists on encryption
            plaintext_up.upload_once(2)
    finally:
        server.should_exit = True


def test_uploader_sends_latest_after_outage(tmp_path, monkeypatch):
    """Real HTTP: outage -> versions pile up -> link returns -> only newest is sent, once."""
    monkeypatch.setenv("INGEST_API_KEY", "secret")
    monkeypatch.delenv("RESCUE_PAYLOAD_KEY", raising=False)
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
