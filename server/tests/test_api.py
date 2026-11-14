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


def test_snapshot_missing_required_file_is_422_not_500(env):
    """A consistent manifest that simply omits sightings.csv used to crash ingest (500)."""
    client, root, store = env
    files = files_for(root, 1)
    m = json.loads(files[0][1][1])
    m["files"] = [f for f in m["files"] if f["name"] != "sightings.csv"]
    files = [("manifest", ("manifest.json", json.dumps(m).encode(), "application/json"))] + [
        f for f in files[1:] if f[1][0] != "sightings.csv"]
    r = client.post("/api/v1/snapshots", files=files, headers=H)
    assert r.status_code == 422 and "sightings.csv" in r.text
    assert store.missions() == []


def test_postgres_url_without_driver_gives_clear_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)  # simulate the driver not installed
    with pytest.raises(RuntimeError, match="requirements-postgres.txt"):
        Storage("postgresql://user:pw@localhost:5432/tsdb", tmp_path / "cloud")


def test_cloud_dashboard_explains_missing_build_then_serves_it(tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    monkeypatch.setenv("WEB_DIR", str(web))
    client = TestClient(create_app(Storage(f"sqlite:///{tmp_path}/c.db", tmp_path / "cloud")))
    r = client.get("/")
    assert r.status_code == 503 and "npm run build" in r.text
    assert client.get("/api/v1/health").status_code == 200  # API routes unaffected
    (web / "dist").mkdir()
    (web / "dist" / "index.html").write_text("<p>built dashboard</p>")
    r = client.get("/")
    assert r.status_code == 200 and "built dashboard" in r.text


@pytest.mark.parametrize("bad_id", [".", ".."])
def test_dot_mission_id_rejected_and_nothing_written(env, tmp_path, bad_id):
    """'..' matches the ID character set but would write outside SNAPSHOT_DIR."""
    client, root, store = env
    files = files_for(root, 1)
    m = json.loads(files[0][1][1])
    m["mission_id"] = bad_id
    files[0] = ("manifest", ("manifest.json", json.dumps(m).encode(), "application/json"))
    r = client.post("/api/v1/snapshots", files=files, headers=H)
    assert r.status_code == 422
    assert not (tmp_path / "v000001").exists() and not (store.snapshot_dir / "v000001").exists()
    assert store.missions() == []


def test_people_near_and_reports(env, monkeypatch):
    client, root, store = env
    client.post("/api/v1/snapshots", files=files_for(root, 2), headers=H)
    near = client.get("/api/v1/missions/m1/people/near", params={"lat": LAT, "lon": LON, "radius_m": 30}).json()
    assert [p["distance_m"] < 30 for p in near] == [True]
    assert "Mission report" in client.get("/api/v1/missions/m1/report").text
    # Upload alone does not create a report (rule: periodic or on demand only).
    pending = client.get("/api/v1/missions/m1/ai-report").json()
    assert pending["report"] is None and pending["latest_snapshot_version"] == 2
    automated = client.post("/api/v1/missions/m1/ai-report").json()  # the dashboard button
    assert automated["generated_by"] == "deterministic"
    assert automated["human_review_required"] is True
    assert automated["snapshot_version"] == 2
    assert automated["report"]
    stored = client.get("/api/v1/missions/m1/ai-report").json()
    assert stored["report"] == automated["report"] and stored["stale"] is False
    assert store._conn.execute("SELECT COUNT(*) FROM heatmap_cells WHERE mission_id='m1'").fetchone()[0] > 0
    assert store._conn.execute("SELECT COUNT(*) FROM people WHERE mission_id='m1'").fetchone()[0] == 3
    r = client.post("/api/v1/missions/m1/ai-report")  # within REPORT_MIN_GAP_S
    assert r.status_code == 429 and r.json()["retry_after_s"] >= 1


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


def test_uploads_and_reads_never_generate_reports(env):
    client, root, store = env
    for v in (1, 2):
        assert client.post("/api/v1/snapshots", files=files_for(root, v), headers=H).status_code == 201
    for _ in range(3):
        body = client.get("/api/v1/missions/m1/ai-report").json()
        assert body["report"] is None and body["status"] == "pending"
    assert not list((store.snapshot_dir / "m1").glob("v*/ai-report.json"))


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _manager(store, interval_s, min_gap_s=20):
    from rescue_api.report_manager import ReportManager

    calls = []

    def fake_generate(store_, mission_id, version):
        calls.append((mission_id, version))
        d = store_.snapshot_dir / mission_id / f"v{version:06d}"
        result = {"mission_id": mission_id, "snapshot_version": version, "status": "ready",
                  "generated_by": "deterministic", "report": f"report v{version}",
                  "human_review_required": True}
        (d / "ai-report.json").write_text(json.dumps(result))
        return result

    clock = FakeClock()
    return ReportManager(store, interval_s=interval_s, min_gap_s=min_gap_s,
                         generate=fake_generate, clock=clock), calls, clock


def test_periodic_reports_follow_the_interval(env):
    client, root, store = env
    mgr, calls, clock = _manager(store, interval_s=300)
    client.post("/api/v1/snapshots", files=files_for(root, 1), headers=H)
    assert mgr.tick() == [("m1", 1)]           # first report for the mission: right away
    assert mgr.tick() == []                    # nothing new
    client.post("/api/v1/snapshots", files=files_for(root, 2), headers=H)
    clock.t += 120
    assert mgr.tick() == []                    # new data, but within the interval
    assert mgr.latest_report("m1")["snapshot_version"] == 1
    clock.t += 200                             # 320 s since the last report
    assert mgr.tick() == [("m1", 2)]
    assert calls == [("m1", 1), ("m1", 2)]


def test_on_demand_reports_are_rate_limited(env):
    from rescue_api.report_manager import ReportRateLimited

    client, root, store = env
    mgr, calls, clock = _manager(store, interval_s=0, min_gap_s=20)
    client.post("/api/v1/snapshots", files=files_for(root, 1), headers=H)
    assert mgr.periodic is False
    clock.t += 100
    assert mgr.generate("m1", 1)["report"] == "report v1"
    clock.t += 5
    with pytest.raises(ReportRateLimited) as exc:
        mgr.generate("m1", 1)
    assert exc.value.retry_after_s == 15
    assert mgr.status("m1")["on_demand_available_in_s"] == 15
    clock.t += 15
    mgr.generate("m1", 1)
    assert calls == [("m1", 1), ("m1", 1)]


def test_report_interval_zero_starts_no_thread(env, monkeypatch):
    from rescue_api.report_manager import ReportManager

    _, _, store = env
    monkeypatch.setenv("REPORT_INTERVAL_S", "0")
    mgr = ReportManager(store)
    mgr.start()
    assert mgr.periodic is False and mgr._thread is None
    monkeypatch.setenv("REPORT_INTERVAL_S", "-1")
    with pytest.raises(ValueError):
        ReportManager(store)


def test_stale_report_is_flagged(env):
    client, root, store = env
    client.post("/api/v1/snapshots", files=files_for(root, 1), headers=H)
    client.post("/api/v1/missions/m1/ai-report")
    client.post("/api/v1/snapshots", files=files_for(root, 2), headers=H)
    body = client.get("/api/v1/missions/m1/ai-report").json()
    assert body["snapshot_version"] == 1 and body["latest_snapshot_version"] == 2
    assert body["stale"] is True


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


def test_uploader_parks_409_conflict_instead_of_retrying(tmp_path):
    """A 409 can never succeed on retry: send it once, flag it, then wait for a newer version."""
    from types import SimpleNamespace

    import rescue_edge.uploader as upmod

    edge = tmp_path / "edge"
    w = make_snapshots(edge, (1, 2))  # v1, v2
    calls = []

    def fake_post(url, files, headers, timeout):
        version = json.loads(files[0][1][1])["version"]
        calls.append(version)
        if version == 2:
            return SimpleNamespace(status_code=409, text="same version with different content")
        return SimpleNamespace(status_code=201, json=lambda: {"status": "accepted"})

    up = Uploader(edge, "m1", "http://unused", base_backoff_s=0.01, poll_s=0.05)
    orig_post = upmod.requests.post
    upmod.requests.post = fake_post
    try:
        up.start()
        time.sleep(0.5)
        st = up.status()
        assert calls == [2]  # sent once, not retried
        assert st["conflict_version"] == 2 and st["needs_attention"] is True
        assert "Needs a human" in st["last_error"] and st["acked_version"] == 0

        # A newer local version is still uploaded and clears the attention flag.
        agg = Aggregator()
        lat, lon = offset_to_latlon(LAT, LON, 500, 0)
        for i in range(5):
            agg.add(900 + i * 0.1, 99, 0.8, lat, lon, 3.0, None)
            agg.note_frame(900 + i * 0.1)
        assert w.write_if_changed(agg.finalize()) == 3
        up.poke()
        for _ in range(40):
            if up.status()["acked_version"] == 3:
                break
            time.sleep(0.05)
        st = up.status()
        assert calls == [2, 3]
        assert st["acked_version"] == 3 and st["needs_attention"] is False
    finally:
        up.stop()
        upmod.requests.post = orig_post


@pytest.mark.parametrize("code,hint", [(401, "INGEST_API_KEY"), (413, "MAX_UPLOAD_BYTES")])
def test_uploader_flags_config_errors_and_recovers(tmp_path, code, hint):
    """401/413 keep retrying (a server-side fix makes them succeed) but are flagged meanwhile."""
    from types import SimpleNamespace

    import rescue_edge.uploader as upmod

    edge = tmp_path / "edge"
    make_snapshots(edge, (1,))
    fixed = threading.Event()

    def fake_post(url, files, headers, timeout):
        if fixed.is_set():
            return SimpleNamespace(status_code=201, json=lambda: {"status": "accepted"})
        return SimpleNamespace(status_code=code, text="nope")

    up = Uploader(edge, "m1", "http://unused", base_backoff_s=0.01, max_backoff_s=0.05, poll_s=0.05)
    orig_post = upmod.requests.post
    upmod.requests.post = fake_post
    try:
        up.start()
        time.sleep(0.3)
        st = up.status()
        assert st["needs_attention"] is True and hint in st["last_error"] and st["acked_version"] == 0
        fixed.set()  # e.g. the server's key or size limit was corrected
        for _ in range(40):
            if up.status()["acked_version"] == 1:
                break
            time.sleep(0.05)
        st = up.status()
        assert st["acked_version"] == 1 and st["needs_attention"] is False and st["last_error"] is None
    finally:
        up.stop()
        upmod.requests.post = orig_post


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
