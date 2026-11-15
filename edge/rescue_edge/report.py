"""Deterministic mission report. Always available, needs no network or LLM."""
from __future__ import annotations

import statistics
from datetime import datetime, timezone

from .aggregate import HEAT_FORMULA, MissionState


def iso(t: float | None) -> str | None:
    if t is None:
        return None
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def error_stats(state: MissionState) -> tuple[float | None, float | None]:
    errs = sorted(p.horizontal_error_m for p in state.people)
    if not errs:
        return None, None
    p90 = errs[min(len(errs) - 1, int(round(0.9 * (len(errs) - 1))))]
    return round(statistics.median(errs), 2), round(p90, 2)


def build_report(state: MissionState, mission_id: str, drone_id: str, version: int) -> str:
    med, p90 = error_stats(state)
    lines = [
        f"# Mission report: {mission_id}",
        "",
        f"Drone `{drone_id}` · snapshot v{version} · data {iso(state.data_start)} → {iso(state.data_end)}",
        "",
        "> **Estimates for human review.** Positions are computed from camera geometry and drone "
        "GPS; they are not confirmed people or surveyed locations.",
        "",
        "## Summary",
        f"- Estimated people (after de-duplication): **{len(state.people)}**",
        f"- Tracks without a usable location: {len(state.unlocated)}",
        f"- Frames processed: {state.frames_processed}; raw detections: {state.detections_processed}",
        f"- Horizontal position error (model): median {med} m, 90th percentile {p90} m",
        f"- Grid: {state.cell_size_m:g} m cells, {len(state.cells)} occupied",
    ]
    if state.location_failures:
        fails = ", ".join(f"{k}: {v}" for k, v in sorted(state.location_failures.items()))
        lines.append(f"- Detections with no position (by reason): {fails}")
    top = sorted(state.cells, key=lambda c: (-c.count, -c.score))[:5]
    if top:
        lines += ["", "## Densest cells", "", "| Cell | Est. people | Score | Centre (lat, lon) |",
                  "|---|---|---|---|"]
        for c in top:
            lats = [p[1] for p in c.polygon[:4]]
            lons = [p[0] for p in c.polygon[:4]]
            lines.append(f"| {c.cell_id} | {c.count} | {c.score:.2f} | "
                         f"{statistics.fmean(lats):.6f}, {statistics.fmean(lons):.6f} |")
    lines += ["", f"Heat score: {HEAT_FORMULA}.", ""]
    return "\n".join(lines)
