"""Mission reports (Gemini via LangChain, deterministic fallback): periodic or on demand.

Reports are never generated per upload: during a live mission the edge sends a snapshot
every ~10 s, and one LLM call per snapshot would be slow, costly and mostly redundant.

* Periodic: every ``REPORT_INTERVAL_S`` (default 300 s) a background thread reports on
  each mission whose newest snapshot has no report yet. ``0`` disables it (on demand only).
* On demand: ``generate()`` (the dashboard button) runs immediately, rate-limited per
  mission by ``REPORT_MIN_GAP_S`` (default 20 s).

Each report is stored in the ``ai_reports`` table under the snapshot version it describes,
so it always cites the exact data it was written from.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 300.0
DEFAULT_MIN_GAP_S = 20.0


class ReportRateLimited(Exception):
    def __init__(self, retry_after_s: float, reason: str):
        super().__init__(reason)
        self.retry_after_s = max(1, int(retry_after_s + 0.999))
        self.reason = reason


def generate_and_store(store, mission_id: str, version: int) -> dict:
    """Write the report for one snapshot version and return it (never raises)."""
    row = store.snapshot_row(mission_id, version)
    if not row:
        raise LookupError(f"no snapshot {mission_id} v{version}")
    try:
        from .agent import generate_report

        deterministic = (store.read_file(mission_id, version, "report.md") or b"").decode("utf-8")
        result = generate_report(store, mission_id, row, deterministic)
        result = {**result, "status": "ready", "generated_at": _now_iso()}
    except Exception:
        log.exception("report generation failed for %s v%d", mission_id, version)
        result = {
            "mission_id": mission_id, "snapshot_version": version, "status": "failed",
            "generated_by": None, "report": None, "generated_at": _now_iso(),
            "error": "Report generation failed; check cloud service logs.",
            "human_review_required": True,
        }
    store.save_ai_report(mission_id, version, result)
    log.info("report for %s v%d: %s", mission_id, version, result.get("generated_by") or "failed")
    return result


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ReportManager:
    def __init__(self, store, interval_s: float | None = None, min_gap_s: float | None = None,
                 generate: Callable[[object, str, int], dict] = generate_and_store,
                 clock: Callable[[], float] = time.monotonic):
        env_interval = os.environ.get("REPORT_INTERVAL_S")
        env_gap = os.environ.get("REPORT_MIN_GAP_S")
        self.store = store
        self.interval_s = float(interval_s if interval_s is not None
                                else env_interval if env_interval not in (None, "") else DEFAULT_INTERVAL_S)
        self.min_gap_s = float(min_gap_s if min_gap_s is not None
                               else env_gap if env_gap not in (None, "") else DEFAULT_MIN_GAP_S)
        if self.interval_s < 0 or self.min_gap_s < 0:
            raise ValueError("REPORT_INTERVAL_S and REPORT_MIN_GAP_S must be >= 0")
        self._generate, self._clock = generate, clock
        self._last_run: dict[str, float] = {}   # mission -> clock() of last generation start
        self._busy: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def periodic(self) -> bool:
        return self.interval_s > 0

    # ------------------------------------------------------------------ reads
    def latest_report(self, mission_id: str) -> dict | None:
        """Newest stored report for the mission, whichever snapshot version it describes."""
        return self.store.latest_ai_report(mission_id)

    def status(self, mission_id: str) -> dict:
        with self._lock:
            last = self._last_run.get(mission_id)
            busy = mission_id in self._busy
        now = self._clock()
        return {
            "mode": "periodic" if self.periodic else "on_demand",
            "interval_s": self.interval_s,
            "min_gap_s": self.min_gap_s,
            "generating": busy,
            "on_demand_available_in_s": 0 if last is None else max(0, int(self.min_gap_s - (now - last) + 0.999)),
        }

    # ------------------------------------------------------------------ generation
    def _claim(self, mission_id: str, min_gap_s: float) -> None:
        with self._lock:
            if mission_id in self._busy:
                raise ReportRateLimited(5, "a report for this mission is already being generated")
            last = self._last_run.get(mission_id)
            now = self._clock()
            if last is not None and now - last < min_gap_s:
                raise ReportRateLimited(min_gap_s - (now - last),
                                        "a report was generated for this mission moments ago")
            self._busy.add(mission_id)
            self._last_run[mission_id] = now

    def _release(self, mission_id: str) -> None:
        with self._lock:
            self._busy.discard(mission_id)

    def generate(self, mission_id: str, version: int) -> dict:
        """On demand (dashboard button). Raises ReportRateLimited if called too soon."""
        self._claim(mission_id, self.min_gap_s)
        try:
            return self._generate(self.store, mission_id, version)
        finally:
            self._release(mission_id)

    def tick(self) -> list[tuple[str, int]]:
        """One periodic pass. Returns the (mission, version) pairs reported on."""
        done = []
        for mission in self.store.missions():
            mission_id, version = mission["mission_id"], mission["latest_version"]
            latest = self.latest_report(mission_id)
            if latest and latest.get("snapshot_version", 0) >= version:
                continue  # already describes the newest data
            try:
                self._claim(mission_id, self.interval_s)
            except ReportRateLimited:
                continue  # reported within the interval; the next pass will pick it up
            try:
                self._generate(self.store, mission_id, version)
                done.append((mission_id, version))
            except Exception:
                log.exception("periodic report failed for %s v%d", mission_id, version)
            finally:
                self._release(mission_id)
        return done

    # ------------------------------------------------------------------ thread
    def start(self) -> None:
        if not self.periodic or self._thread is not None:
            return
        poll_s = max(1.0, min(self.interval_s, 30.0))

        def loop() -> None:
            while not self._stop.wait(poll_s):
                try:
                    self.tick()
                except Exception:  # e.g. database briefly unavailable; try next pass
                    log.exception("periodic report pass failed")

        self._thread = threading.Thread(target=loop, name="report-manager", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
