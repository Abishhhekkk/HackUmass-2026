"""Compress-then-encrypt for uploads. Deliberately free of fastapi so it runs anywhere."""
import base64
import json
import os
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from rescue_api import wire as server_wire
from rescue_edge import wire
from rescue_edge.aggregate import Aggregator
from rescue_edge.geolocate import offset_to_latlon
from rescue_edge.snapshot import SnapshotWriter, snapshot_dir
from rescue_edge.uploader import Uploader

REPO = Path(__file__).resolve().parents[2]
KEY = os.urandom(32)
SAMPLE = (b"person_id,status,lat,lon\n" + b"P001,estimated_sighting,42.3893000,-72.5283000\n" * 200)


def test_server_copy_is_identical():
    # edge and cloud deploy separately, so each carries a copy; they must not drift apart
    assert (REPO / "edge/rescue_edge/wire.py").read_bytes() == (REPO / "server/rescue_api/wire.py").read_bytes()


def test_round_trip_and_actually_compresses():
    blob = wire.seal(SAMPLE, KEY, "sightings.csv")
    assert wire.is_sealed(blob) and blob != SAMPLE
    assert len(blob) < len(SAMPLE) / 5
    assert wire.open_sealed(blob, KEY, "sightings.csv") == SAMPLE


def test_empty_payload_round_trips():
    assert wire.open_sealed(wire.seal(b"", KEY, "x"), KEY, "x") == b""


def test_fresh_nonce_each_time():
    assert wire.seal(SAMPLE, KEY, "a") != wire.seal(SAMPLE, KEY, "a")


@pytest.mark.parametrize("mutate", [
    lambda b: b[:-1],                                   # truncated
    lambda b: b[: len(b) // 2],                         # cut mid-transfer
    lambda b: b[:30] + bytes([b[30] ^ 1]) + b[31:],     # one flipped bit in the ciphertext
    lambda b: b[:5] + bytes([b[5] ^ 1]) + b[6:],        # nonce edited
    lambda b: b[:4] + bytes([b[4] ^ 3]) + b[5:],        # codec byte edited
])
def test_tampering_is_rejected(mutate):
    blob = wire.seal(SAMPLE, KEY, "sightings.csv")
    with pytest.raises(wire.WireError):
        wire.open_sealed(mutate(blob), KEY, "sightings.csv")


def test_wrong_key_and_wrong_name_are_rejected():
    blob = wire.seal(SAMPLE, KEY, "sightings.csv")
    with pytest.raises(wire.WireError):
        wire.open_sealed(blob, os.urandom(32), "sightings.csv")
    with pytest.raises(wire.WireError):  # a part cannot be passed off under another file name
        wire.open_sealed(blob, KEY, "report.md")


def test_plaintext_is_not_mistaken_for_sealed():
    assert not wire.is_sealed(SAMPLE)
    with pytest.raises(wire.WireError):
        wire.open_sealed(SAMPLE, KEY, "sightings.csv")


def test_decompressed_size_cap():
    blob = wire.seal(b"A" * 1_000_000, KEY, "big")
    assert len(blob) < 10_000                      # highly compressible
    with pytest.raises(wire.WireError, match="size limit"):
        wire.open_sealed(blob, KEY, "big", max_out=1000)
    assert len(wire.open_sealed(blob, KEY, "big", max_out=2_000_000)) == 1_000_000


def test_zlib_fallback_still_opens(monkeypatch):
    monkeypatch.setattr(wire, "zstandard", None)    # a Jetson without the zstandard package
    blob = wire.seal(SAMPLE, KEY, "sightings.csv")
    assert blob[len(wire.MAGIC)] == wire.CODEC_ZLIB
    assert wire.open_sealed(blob, KEY, "sightings.csv") == SAMPLE


def test_zlib_truncated_stream_inside_valid_envelope_is_rejected():
    body = zlib.compress(SAMPLE)[:-8]
    prefix = wire.MAGIC + bytes([wire.CODEC_ZLIB])
    nonce = os.urandom(wire.NONCE_LEN)
    blob = prefix + nonce + wire.AESGCM(KEY).encrypt(nonce, body, wire._aad(prefix, "x"))
    with pytest.raises(wire.WireError):
        wire.open_sealed(blob, KEY, "x")


def test_zstd_codec_round_trip():
    pytest.importorskip("zstandard")
    blob = wire.seal(SAMPLE, KEY, "sightings.csv")
    assert blob[len(wire.MAGIC)] == wire.CODEC_ZSTD
    assert wire.open_sealed(blob, KEY, "sightings.csv") == SAMPLE
    with pytest.raises(wire.WireError, match="size limit"):
        wire.open_sealed(blob, KEY, "sightings.csv", max_out=100)


def test_load_key():
    assert wire.load_key(None) is None and wire.load_key("") is None and wire.load_key("  ") is None
    assert wire.load_key(base64.b64encode(KEY).decode()) == KEY
    with pytest.raises(wire.WireError):
        wire.load_key("not base64 !!")
    with pytest.raises(wire.WireError):
        wire.load_key(base64.b64encode(b"too short").decode())


# ------------------------------------------------------------------ uploader integration
def _write_snapshot(root):
    agg = Aggregator()
    lat, lon = offset_to_latlon(42.3893, -72.5283, 20, 0)
    for i in range(5):
        agg.add(i * 0.1, 1, 0.8, lat, lon, 3.0, None)
        agg.note_frame(i * 0.1)
    assert SnapshotWriter(root, "m1", "drone-01").write_if_changed(agg.finalize()) == 1


def _capture_post(monkeypatch):
    sent = {}

    def fake_post(url, files, headers, timeout):
        sent["files"] = files
        return SimpleNamespace(status_code=201, text="", json=lambda: {"status": "accepted"})

    monkeypatch.setattr("rescue_edge.uploader.requests.post", fake_post)
    return sent


def test_uploader_seals_every_part_and_server_side_can_open_them(tmp_path, monkeypatch):
    _write_snapshot(tmp_path)
    sent = _capture_post(monkeypatch)
    up = Uploader(tmp_path, "m1", "http://cloud.invalid", api_key="k", payload_key=KEY)
    assert up.upload_once(1)["status"] == "accepted" and up.status()["encrypted"] is True

    d = snapshot_dir(tmp_path, "m1", 1)
    parts = {name: blob for _field, (name, blob, *_rest) in sent["files"]}
    manifest_files = {f["name"] for f in json.loads((d / "manifest.json").read_text())["files"]}
    assert set(parts) == {"manifest.json"} | manifest_files
    for name, blob in parts.items():
        assert wire.is_sealed(blob)
        # the server module opens what the edge module sealed, and the bytes match the files on disk
        assert server_wire.open_sealed(blob, KEY, name) == (d / name).read_bytes()


def test_uploader_without_key_sends_plaintext_unchanged(tmp_path, monkeypatch):
    _write_snapshot(tmp_path)
    sent = _capture_post(monkeypatch)
    up = Uploader(tmp_path, "m1", "http://cloud.invalid")
    up.upload_once(1)
    d = snapshot_dir(tmp_path, "m1", 1)
    for _field, (name, blob, *_rest) in sent["files"]:
        assert blob == (d / name).read_bytes() and not wire.is_sealed(blob)
    assert up.status()["encrypted"] is False


def test_corrupt_next_hook_still_breaks_an_encrypted_transfer(tmp_path, monkeypatch):
    _write_snapshot(tmp_path)
    sent = _capture_post(monkeypatch)
    up = Uploader(tmp_path, "m1", "http://cloud.invalid", payload_key=KEY)
    up.corrupt_next = True
    up.upload_once(1)
    failures = 0
    for field, (name, blob, *_rest) in sent["files"]:
        try:
            wire.open_sealed(blob, KEY, name)
        except wire.WireError:
            failures += 1
    assert failures == 1 and up.corrupt_next is False  # exactly one cut part, hook re-armed off
