"""Background uploader: sends the NEWEST snapshot whenever a link is available.

* Runs in its own thread, so a dead network never slows detection.
* Skips intermediate versions: if v3..v9 piled up offline, only v9 is sent.
* The server verifies every file's sha256 and keeps the highest version, so a cut-off
  or repeated upload is harmless; we simply try again.
* Bounded exponential backoff with jitter.
* A 409 (server holds this version number with different content, e.g. edge storage was
  reset mid-mission) is NOT retried: resending the same bytes can never succeed. The
  version is parked as a conflict that needs a human; a newer local version is still sent.
* 401 (bad API key) and 413 (snapshot too large) are configuration problems: they are
  still retried with backoff (fixing the server side makes them succeed), but flagged as
  needing attention with a message saying what to fix.
"""
from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path

import requests

from .snapshot import latest_version, snapshot_dir


class UploadRejected(RuntimeError):
    def __init__(self, status_code: int, text: str):
        super().__init__(f"HTTP {status_code}: {text[:200]}")
        self.status_code = status_code


CONFIG_ERRORS = {
    401: "server rejected the API key (401): check INGEST_API_KEY matches on the Jetson and server",
    413: "snapshot is larger than the server allows (413): raise MAX_UPLOAD_BYTES on the server",
}


class Uploader(threading.Thread):
    def __init__(self, root: str | Path, mission_id: str, server_url: str, api_key: str | None = None,
                 offline_flag_file: str | None = None, base_backoff_s: float = 2.0,
                 max_backoff_s: float = 60.0, poll_s: float = 2.0):
        super().__init__(daemon=True)
        self.mission_dir = Path(root) / mission_id
        self.root, self.mission_id = Path(root), mission_id
        self.url = server_url.rstrip("/") + "/api/v1/snapshots"
        self.api_key = api_key
        self.offline_flag = Path(offline_flag_file) if offline_flag_file else None
        self.base, self.cap, self.poll = base_backoff_s, max_backoff_s, poll_s
        self.state_path = self.mission_dir / "upload_state.json"
        self.state = self._load_state()
        self.online: bool | None = None
        self.last_error: str | None = None
        self.corrupt_next = False  # demo hook: send a truncated file once
        self.conflict_version: int | None = None  # version the server rejected with 409
        self.config_error: str | None = None      # 401/413: retried, but a human must fix config
        self._stop = threading.Event()
        self._wake = threading.Event()

    # ------------------------------------------------------------------ state
    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except ValueError:
                pass
        return {"acked_version": 0, "acked_at": None}

    def _save_state(self) -> None:
        self.mission_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        tmp.replace(self.state_path)

    def status(self) -> dict:
        local = latest_version(self.mission_dir)
        return {"local_version": local, "acked_version": self.state["acked_version"],
                "pending": local > self.state["acked_version"], "online": self.online,
                "last_error": self.last_error, "acked_at": self.state["acked_at"],
                "conflict_version": self.conflict_version,
                "needs_attention": self.config_error is not None
                or (self.conflict_version is not None and local <= self.conflict_version)}

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def poke(self) -> None:
        """Called by the pipeline after writing a new snapshot."""
        self._wake.set()

    # ------------------------------------------------------------------ upload
    def upload_once(self, version: int) -> dict:
        d = snapshot_dir(self.root, self.mission_id, version)
        manifest = json.loads((d / "manifest.json").read_text())
        files = [("manifest", ("manifest.json", (d / "manifest.json").read_bytes(), "application/json"))]
        for f in manifest["files"]:
            data = (d / f["name"]).read_bytes()
            if self.corrupt_next:
                data, self.corrupt_next = data[: len(data) // 2], False
            files.append(("files", (f["name"], data, "application/octet-stream")))
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        r = requests.post(self.url, files=files, headers=headers, timeout=(5, 30))
        if r.status_code >= 400:
            raise UploadRejected(r.status_code, r.text)
        return r.json()

    def run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            if self.offline_flag and self.offline_flag.exists():
                self.online = False
                self._wait(self.poll)
                continue
            local = latest_version(self.mission_dir)
            if local <= self.state["acked_version"] or local == self.conflict_version:
                self._wait(self.poll)
                continue
            try:
                res = self.upload_once(local)
                self.online, self.last_error, self.config_error, failures = True, None, None, 0
                # accepted / duplicate / stale all mean "server has >= this version"
                self.state = {"acked_version": local, "acked_at": time.time(),
                              "server_status": res.get("status")}
                self._save_state()
            except UploadRejected as e:
                if e.status_code != 409:  # 422 checksum mismatch, 401/413 config: retry with backoff
                    self.config_error = CONFIG_ERRORS.get(e.status_code)
                    failures += 1
                    self._backoff(e, failures)
                    continue
                self.online, self.conflict_version, failures = True, local, 0
                self.last_error = (f"v{local} conflicts with a different v{local} already on the "
                                   "server (edge storage reset?). Needs a human: start a new "
                                   "mission ID. Not retrying this version.")
                self._wait(self.poll)
            except Exception as e:  # network down, timeout...
                failures += 1
                self._backoff(e, failures)

    def _backoff(self, e: Exception, failures: int) -> None:
        self.online = not isinstance(e, requests.ConnectionError)
        self.last_error = self.config_error or str(e)[:300]
        delay = min(self.cap, self.base * 2 ** (failures - 1))
        self._wait(delay * random.uniform(0.7, 1.0))

    def _wait(self, s: float) -> None:
        self._wake.wait(s)
        self._wake.clear()
