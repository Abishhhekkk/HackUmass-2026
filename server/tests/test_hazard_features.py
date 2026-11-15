"""Hazard-relevant features (water bodies etc.): OSM fallback, Gemini Maps path, tool wiring."""
import sys
import types

import pytest

from rescue_api import agent

LAT, LON = 42.0, -72.5
M_PER_DEG_LAT = 111_195.0


def north(m):  # a latitude `m` metres north of the search point
    return LAT + m / M_PER_DEG_LAT


def settings(key=""):
    return lambda: {"GEMINI_API_KEY": key, "GEMINI_MODEL": "gm"}


# ---------- OSM fallback ----------

def test_lake_distance_is_to_nearest_shore_vertex_not_centre():
    lake = {"type": "way", "tags": {"natural": "water", "name": "Mill Pond"},
            "center": {"lat": north(2000), "lon": LON},
            "geometry": [{"lat": north(500), "lon": LON}, {"lat": north(3500), "lon": LON}]}
    f = agent._nearest_features([lake], "water", LAT, LON)[0]
    assert f["name"] == "Mill Pond" and abs(f["distance_m"] - 500) < 5 and f["direction"] == "north"
    assert f["distance_is_to"] == "nearest mapped point"


def test_relation_members_geometry_and_dedupe_by_name_and_cap():
    river = lambda m: {"type": "way", "tags": {"waterway": "river", "name": "Fort River"},  # noqa: E731
                       "geometry": [{"lat": north(m), "lon": LON}]}
    rel = {"type": "relation", "tags": {"natural": "water", "name": "Big Lake"},
           "members": [{"geometry": [{"lat": LAT, "lon": LON + 0.01}]}]}
    unnamed = {"type": "node", "lat": north(4000), "lon": LON, "tags": {"natural": "water"}}
    out = agent._nearest_features([river(900), river(300), rel, unnamed], "water", LAT, LON)
    assert len(out) == agent.PER_CATEGORY
    assert [f["name"] for f in out][0] == "Fort River" and out[0]["distance_m"] < 400
    assert out[1]["name"] == "Big Lake" and out[1]["direction"] == "east"


class _Resp:
    def __init__(self, elements):
        self._e = elements

    def raise_for_status(self):
        pass

    def json(self):
        return {"elements": self._e}


def test_osm_profile_by_hazard_and_missing_categories(monkeypatch):
    queries = []

    def fake_post(url, data, **kw):
        queries.append(data["data"])
        if "natural=water" in data["data"]:
            return _Resp([{"type": "node", "lat": north(700), "lon": LON,
                           "tags": {"natural": "water", "name": "Orchard Pond"}}])
        return _Resp([])

    monkeypatch.setattr(agent.requests, "post", fake_post)
    fire = agent.fetch_hazard_features_osm(LAT, LON, "fire")
    assert len(queries) == 3 and fire["hazard"] == "fire"       # water, open ground, roads
    assert fire["features"][0]["name"] == "Orchard Pond"
    assert set(fire["none_mapped_within_radius"]) == {"open_ground", "major_road"}
    queries.clear()
    flood = agent.fetch_hazard_features_osm(LAT, LON, "flood")
    assert len(queries) == 4 and "man_made=bridge" in "".join(queries)
    assert agent.fetch_hazard_features_osm(LAT, LON, "volcano")["hazard"] == "general"


# ---------- Gemini Maps ----------

def _fake_genai(monkeypatch):
    mk = lambda n: type(n, (), {"__init__": lambda self, *a, **k: self.__dict__.update(k)})  # noqa: E731
    types_mod = types.ModuleType("google.genai.types")
    for n in ("GenerateContentConfig", "Tool", "GoogleMaps", "ToolConfig", "RetrievalConfig", "LatLng"):
        setattr(types_mod, n, mk(n))
    genai_mod = types.ModuleType("google.genai")
    genai_mod.types = types_mod
    google_mod = types.ModuleType("google")
    google_mod.genai = genai_mod
    for name, mod in (("google", google_mod), ("google.genai", genai_mod), ("google.genai.types", types_mod)):
        monkeypatch.setitem(sys.modules, name, mod)


class _FakeClient:
    def __init__(self, text, chunks=()):
        self.calls = []
        objs = [types.SimpleNamespace(maps=types.SimpleNamespace(title=t, uri=u)) for t, u in chunks]
        self._resp = types.SimpleNamespace(text=text, candidates=[types.SimpleNamespace(
            grounding_metadata=types.SimpleNamespace(grounding_chunks=objs))])
        self.models = types.SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls.append(kw)
        return self._resp


def test_gemini_features_computed_locally_filtered_and_sourced(monkeypatch):
    _fake_genai(monkeypatch)
    monkeypatch.setattr(agent, "_ai_settings", settings("k"))
    text = ('```json\n{"features":['
            f'{{"category":"water","name":"Mill Pond","latitude":{north(800)},"longitude":{LON}}},'
            f'{{"category":"water","name":"Far Lake","latitude":{north(60000)},"longitude":{LON}}},'
            f'{{"category":"major_road","name":"Route 9","latitude":{LAT},"longitude":{LON - 0.0126}}},'
            '{"category":"casino","name":"Nope","latitude":42.0,"longitude":-72.5},'
            '{"category":"water","name":"Bad","latitude":"x","longitude":null}]}\n```')
    client = _FakeClient(text, [("Mill Pond", "https://maps.google.com/?cid=1"),
                                ("dup", "https://maps.google.com/?cid=1")])
    out = agent.fetch_hazard_features_via_gemini_maps(LAT, LON, "fire", 3000, client=client)
    assert out["available"] and out["grounded"] and len(out["google_maps_sources"]) == 1
    names = [f["name"] for f in out["features"]]
    assert names == ["Mill Pond", "Route 9"]                       # far/unknown/bad dropped
    assert out["dropped_implausible"] == 3
    assert abs(out["features"][0]["distance_m"] - 800) < 5 and out["features"][1]["direction"] == "west"
    assert out["none_found_within_radius"] == ["open_ground"]
    call = client.calls[0]
    assert call["model"] == "gm" and call["config"].tools[0].google_maps is not None
    assert call["config"].tool_config.retrieval_config.lat_lng.latitude == LAT
    assert "water bodies" in call["contents"] and "bridges" not in call["contents"]


def test_gemini_unusable_answers_and_errors_never_raise(monkeypatch):
    _fake_genai(monkeypatch)
    monkeypatch.setattr(agent, "_ai_settings", settings("secret-key"))
    for text in ("no json", '{"features": []}', '{"features": "oops"}'):
        out = agent.fetch_hazard_features_via_gemini_maps(LAT, LON, client=_FakeClient(text))
        assert out["available"] is False

    class Boom:
        models = types.SimpleNamespace(
            generate_content=lambda **kw: (_ for _ in ()).throw(RuntimeError("bad key secret-key")))

    out = agent.fetch_hazard_features_via_gemini_maps(LAT, LON, client=Boom())
    assert out["available"] is False and "secret-key" not in out["reason"]


def test_gemini_skipped_without_key(monkeypatch):
    monkeypatch.setattr(agent, "_ai_settings", settings(""))
    assert agent.fetch_hazard_features_via_gemini_maps(LAT, LON)["available"] is False


# ---------- tool wiring ----------

def _tools(monkeypatch, tmp_path):
    fake = types.ModuleType("langchain_core.tools")
    fake.tool = lambda fn: fn
    monkeypatch.setitem(sys.modules, "langchain_core", types.ModuleType("langchain_core"))
    monkeypatch.setitem(sys.modules, "langchain_core.tools", fake)
    return {t.__name__: t for t in agent.make_tools(types.SimpleNamespace(snapshot_dir=tmp_path),
                                                    "m1", {"version": 1})}


def test_tool_prefers_gemini_and_never_touches_osm(monkeypatch, tmp_path):
    tools = _tools(monkeypatch, tmp_path)
    assert "find_hazard_relevant_features" in tools and "find_nearest_fire_station" not in tools
    monkeypatch.setattr(agent, "fetch_hazard_features_via_gemini_maps",
                        lambda *a, **k: {"available": True, "features": [{"name": "Mill Pond"}]})
    monkeypatch.setattr(agent, "fetch_hazard_features_osm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("OSM called")))
    out = tools["find_hazard_relevant_features"](LAT, LON, "fire")
    assert out["source_used"] == "gemini_google_maps" and out["features"][0]["name"] == "Mill Pond"


def test_tool_falls_back_to_osm_clamps_args_and_contains_errors(monkeypatch, tmp_path):
    tools = _tools(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(agent, "fetch_hazard_features_via_gemini_maps",
                        lambda lat, lon, h, r: seen.update(maps=(lat, lon, h, r)) or
                        {"available": False, "reason": "no GEMINI_API_KEY"})
    monkeypatch.setattr(agent, "fetch_hazard_features_osm",
                        lambda lat, lon, h, r: seen.update(osm=(lat, lon, h, r)) or {"features": []})
    out = tools["find_hazard_relevant_features"](999, -999, "fire", 10**7)
    assert seen["maps"] == (90, -180, "fire", 10000) == seen["osm"]
    assert out["source_used"] == "openstreetmap_fallback"
    assert out["gemini_unavailable_reason"] == "no GEMINI_API_KEY"

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(agent, "fetch_hazard_features_osm", boom)
    out = tools["find_hazard_relevant_features"](LAT, LON)
    assert out["features"] == [] and "RuntimeError" in out["error"]


def test_mission_hazard_setting(monkeypatch):
    monkeypatch.setenv("HAZARD_TYPE", "Fire")
    assert agent._mission_hazard() == "fire"
    monkeypatch.setenv("HAZARD_TYPE", "tornado")
    assert agent._mission_hazard() == "general"
