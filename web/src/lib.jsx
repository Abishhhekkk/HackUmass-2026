// Vantage data layer: one polling hook used by both the home page and mission control.
//
// Where data comes from:
// * Built and served by the Jetson (edge) or the cloud API: same origin, /api/v1.
// * `npm run dev`: /api -> cloud API on :8000 and /edge-api -> edge on :8080 (vite.config.js).
//   If the cloud API is not running, everything falls back to the edge, so the dev site works
//   with only the edge demo running.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

const PRIMARY = "/api/v1";
const EDGE = import.meta.env.DEV ? "/edge-api/api/v1" : null;
export const POLL_MS = 3000;

async function request(base, path, options) {
  const response = await fetch(`${base}${path}`, options);
  if (!response.ok) {
    const body = await response.text();
    let detail = body;
    try {
      detail = JSON.parse(body).detail || body;
    } catch {
      /* plain text */
    }
    const error = new Error(typeof detail === "string" && detail ? detail : `${response.status} ${path}`);
    error.status = response.status;
    throw error;
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("json") ? response.json() : response.text();
}

// Minimal RFC 4180 parser (the edge writes plain CSV; quotes handled anyway).
export function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        field += '"';
        i += 1;
      } else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") {
      row.push(field);
      field = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i += 1;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else field += c;
  }
  if (field || row.length) {
    row.push(field);
    rows.push(row);
  }
  const [header, ...body] = rows.filter((r) => r.some((v) => v !== ""));
  return header ? body.map((r) => Object.fromEntries(header.map((k, i) => [k, r[i] ?? ""]))) : [];
}

export const UNLOCATED_REASONS = {
  no_telemetry: "No telemetry",
  telemetry_stale: "Telemetry too old",
  gnss_fix_invalid: "No valid GPS fix",
  altitude_invalid: "Invalid altitude",
  attitude_exceeds_limit: "Drone tilted too far",
  ray_too_oblique: "Too far off-nadir",
  camera_resolution_mismatch: "Frame size ≠ calibration",
  heading_unknown: "Drone heading unknown",
};

export function clock(value) {
  if (!value) return "—";
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

export function num(value) {
  return value === null || value === undefined || value === "" ? "—" : Number(value).toLocaleString();
}

export function coord(lat, lon) {
  if (lat === undefined || lon === undefined) return "—";
  const ns = lat >= 0 ? "N" : "S";
  const ew = lon >= 0 ? "E" : "W";
  return `${Math.abs(lat).toFixed(4)}°${ns}  ${Math.abs(lon).toFixed(4)}°${ew}`;
}

export function describeReport(report) {
  if (!report) return "No report yet.";
  const schedule = report.schedule || {};
  const every = schedule.interval_s ? Math.max(1, Math.round(schedule.interval_s / 60)) : null;
  if (report.status === "failed") return report.error || "The last report attempt failed.";
  if (!report.report) {
    return schedule.mode === "on_demand" || report.status === "none"
      ? "Reports are on demand."
      : `Next scheduled report within ${every} min.`;
  }
  const source = report.generated_by === "gemini" ? "Gemini with mission tools" : "Deterministic fallback";
  const newer =
    report.stale && report.latest_snapshot_version
      ? ` · v${report.latest_snapshot_version} not summarised yet`
      : "";
  return `${source} · snapshot v${report.snapshot_version}${newer}`;
}

/** Everything both pages show, refreshed every POLL_MS. */
export function useMission() {
  const [base, setBase] = useState(PRIMARY);
  const [status, setStatus] = useState(null);
  const [edgeStatus, setEdgeStatus] = useState(null); // dev only: the edge next to a cloud API
  const [missions, setMissions] = useState([]);
  const [missionId, setMissionId] = useState("");
  const [data, setData] = useState(null);
  const [flightPath, setFlightPath] = useState(null);
  const [aiReport, setAiReport] = useState(null);
  const [sightings, setSightings] = useState(null);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const pinned = useRef(false); // the user picked a mission; don't follow the newest one

  const refresh = useCallback(async () => {
    let api = base;
    let nextStatus;
    try {
      nextStatus = await request(api, "/status");
    } catch (reason) {
      if (EDGE && api !== EDGE) {
        try {
          nextStatus = await request(EDGE, "/status");
          api = EDGE;
          setBase(EDGE);
        } catch {
          /* report the original error below */
        }
      }
      if (!nextStatus) {
        setError(reason.message || "The mission service is not reachable.");
        setLoaded(true);
        return;
      }
    }
    setStatus(nextStatus);
    if (EDGE && api !== EDGE && nextStatus.role === "cloud") {
      request(EDGE, "/status")
        .then((s) => setEdgeStatus(s?.role === "edge" ? s : null))
        .catch(() => setEdgeStatus(null));
    }
    try {
      const list = await request(api, "/missions");
      setMissions(list);
      const chosen =
        pinned.current && list.some((m) => m.mission_id === missionId)
          ? missionId
          : nextStatus.mission_id && list.some((m) => m.mission_id === nextStatus.mission_id)
            ? nextStatus.mission_id
            : list.some((m) => m.mission_id === missionId)
              ? missionId
              : list[0]?.mission_id || "";
      if (chosen !== missionId) setMissionId(chosen);
      if (!chosen) {
        setData(null);
        setError("");
        setLoaded(true);
        return;
      }
      const id = encodeURIComponent(chosen);
      const [latest, ai] = await Promise.allSettled([
        request(api, `/missions/${id}/latest`),
        nextStatus.role === "cloud" ? request(api, `/missions/${id}/ai-report`) : Promise.resolve(null),
      ]);
      if (latest.status === "rejected") throw latest.reason;
      setData((current) =>
        current?.manifest?.mission_id === latest.value.manifest.mission_id &&
        current?.manifest?.version === latest.value.manifest.version
          ? current
          : latest.value,
      );
      setAiReport(ai.status === "fulfilled" ? ai.value : null);
      setError("");
    } catch (reason) {
      setError(reason.message || "Mission data unavailable.");
    }
    setLoaded(true);
  }, [base, missionId]);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const manifest = data?.manifest;
  const snapshotKey = manifest ? `${manifest.mission_id}@${manifest.version}` : "";

  // Per snapshot version: the drone track and the sightings CSV (unlocated rows live only there).
  useEffect(() => {
    if (!manifest) {
      setFlightPath(null);
      setSightings(null);
      return undefined;
    }
    let cancelled = false;
    const id = encodeURIComponent(manifest.mission_id);
    request(base, `/missions/${id}/flight-path`)
      .then((r) => !cancelled && setFlightPath(r))
      .catch(() => !cancelled && setFlightPath(null));
    request(base, `/missions/${id}/files/sightings.csv?version=${manifest.version}`)
      .then((text) => !cancelled && setSightings(parseCsv(String(text))))
      .catch(() => !cancelled && setSightings([]));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshotKey, base]);

  const people = useMemo(
    () => (data?.heatmap?.features || []).filter((f) => f.properties?.kind === "person"),
    [data],
  );
  const cells = useMemo(
    () => (data?.heatmap?.features || []).filter((f) => f.properties?.kind === "cell"),
    [data],
  );
  const unlocated = useMemo(
    () =>
      sightings === null
        ? null
        : sightings
            .filter((r) => r.status === "unlocated_sighting")
            .sort((a, b) => (b.last_seen_utc || "").localeCompare(a.last_seen_utc || "")),
    [sightings],
  );

  const role = status?.role || null;
  const edge = role === "edge" ? status : edgeStatus;
  const uploader = edge?.uploader || null;
  const edgeBase = role === "edge" ? base : edgeStatus ? EDGE : null;

  let link = "unknown";
  if (uploader) link = uploader.online === false ? "offline" : uploader.pending ? "pending" : "online";
  else if (role === "cloud") link = "cloud";
  else if (role === "edge") link = "local";

  const files = (name) =>
    missionId ? `${base}/missions/${encodeURIComponent(missionId)}/files/${name}` : undefined;

  const actions = {
    refresh,
    chooseMission(id) {
      pinned.current = true;
      setMissionId(id);
      setData(null);
    },
    async setOffline(offline) {
      if (!edgeBase) throw new Error("No edge connection: open Vantage from the Jetson or run the edge demo.");
      await request(edgeBase, "/network", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ offline }),
      });
      await refresh();
    },
    async corruptNextUpload() {
      if (!edgeBase) throw new Error("No edge connection.");
      await request(edgeBase, "/demo/corrupt-next-upload", { method: "POST" });
      await refresh();
    },
    async generateReport() {
      const result = await request(base, `/missions/${encodeURIComponent(missionId)}/ai-report`, {
        method: "POST",
      });
      setAiReport(result);
      return result;
    },
    async ask(question) {
      return request(base, `/missions/${encodeURIComponent(missionId)}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
    },
  };

  return {
    loaded,
    error,
    role,
    status,
    edge,
    uploader,
    link,
    canDriveLink: Boolean(edgeBase && uploader && edge?.network_demo_available !== false),
    canCorrupt: Boolean(edgeBase && uploader),
    missions,
    missionId,
    data,
    manifest,
    summary: manifest?.summary || {},
    people,
    cells,
    unlocated,
    flightPath,
    aiReport,
    files,
    actions,
  };
}

export const LINK_LABEL = {
  online: "Link up · cloud in sync",
  pending: "Link up · sending",
  offline: "No link · holding on board",
  cloud: "Cloud copy",
  local: "On board · no uplink configured",
  unknown: "Connecting",
};
