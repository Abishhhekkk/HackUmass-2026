import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion, useMotionValueEvent, useReducedMotion, useScroll, useTransform } from "motion/react";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  ArrowDownToLine,
  ArrowUpRight,
  Bot,
  Camera,
  ChevronDown,
  ChevronRight,
  Clock3,
  Command as CommandIcon,
  Cpu,
  Crosshair,
  Database,
  ExternalLink,
  Layers3,
  MapPin,
  Network,
  PackageCheck,
  Radio,
  RefreshCw,
  Send,
  Satellite,
  ShieldAlert,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import {
  Circle,
  MapContainer,
  Marker,
  Polygon,
  Polyline,
  TileLayer,
  Tooltip,
  useMap,
} from "react-leaflet";
import L from "leaflet";
import { Command } from "cmdk";
import Landing from "./Landing.jsx";
import {
  API,
  EDGE_API,
  FitMission,
  HEAT_COLORS,
  HeatCell,
  MissionMap,
  POLL_MS,
  WINDOWS,
  ZoomControls,
  describeReport,
  errorDetail,
  formatTime,
  getEdgeJSON,
  getJSON,
  personIcon,
} from "./shared.jsx";

function StatusPill({ tone = "neutral", children, icon: Icon }) {
  return (
    <span className={`status-pill ${tone}`}>
      {Icon && <Icon size={13} strokeWidth={2.2} />}
      {children}
    </span>
  );
}

function Metric({ label, value, suffix, icon: Icon, tone }) {
  return (
    <div className={`metric-card ${tone || ""}`}>
      <div className="metric-top">
        <span>{label}</span>
        <Icon size={15} />
      </div>
      <div className="metric-value">
        {value ?? "—"}
        {suffix && <small>{suffix}</small>}
      </div>
    </div>
  );
}

const UNLOCATED_REASONS = {
  no_telemetry: "No telemetry",
  telemetry_stale: "Telemetry too old",
  gnss_fix_invalid: "No valid GPS fix",
  altitude_invalid: "Invalid altitude",
  attitude_exceeds_limit: "Drone tilted too far",
  ray_too_oblique: "Too far off-nadir",
  camera_resolution_mismatch: "Frame size ≠ calibration",
  heading_unknown: "Drone heading unknown",
};

// Minimal RFC 4180 parser: the edge writes plain CSV, but quoted fields are handled anyway.
function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i += 1; }
      else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i += 1;
      row.push(field); rows.push(row); row = []; field = "";
    } else field += c;
  }
  if (field || row.length) { row.push(field); rows.push(row); }
  const [header, ...body] = rows.filter((r) => r.some((value) => value !== ""));
  return header ? body.map((r) => Object.fromEntries(header.map((key, i) => [key, r[i] ?? ""]))) : [];
}

function UnlocatedList({ rows }) {
  if (rows === null) return <p className="muted unlocated-note">Loading…</p>;
  if (!rows.length) {
    return <p className="muted unlocated-note">Every sighting in this snapshot has an estimated position.</p>;
  }
  const shown = rows.slice(0, 25);
  return (
    <div className="cell-people">
      {shown.map((row) => (
        <div className="unlocated-row" key={row.person_id} title={`Tracks: ${row.track_ids}`}>
          <span className="mini-dot unlocated-dot" />
          <b>{row.person_id}</b>
          <span>{UNLOCATED_REASONS[row.unlocated_reason] || row.unlocated_reason || "Unknown"}</span>
          <span>{formatTime(row.last_seen_utc)}</span>
        </div>
      ))}
      {rows.length > shown.length && (
        <p className="muted unlocated-note">+{rows.length - shown.length} more in the sightings CSV</p>
      )}
    </div>
  );
}

function SelectionPanel({ selected, onClose }) {
  if (!selected) {
    return (
      <div className="selection-empty">
        <Crosshair size={19} />
        <span>Select a sighting or grid cell to inspect its details.</span>
      </div>
    );
  }
  if (selected.type === "cell") {
    const { properties, people } = selected;
    return (
      <div className="selection-content">
        <div className="selection-heading">
          <div>
            <span className="eyebrow">GRID CELL</span>
            <h3>{properties.cell_id}</h3>
          </div>
          <button className="icon-button" aria-label="Close selection" onClick={onClose}>
            <X size={15} />
          </button>
        </div>
        <p className="detail-emphasis">
          {properties.count} <span>estimated sighting{properties.count === 1 ? "" : "s"}</span>
        </p>
        <div className="cell-people">
          {people.length ? (
            people.map((person) => (
              <div className="person-row" key={person.person_id}>
                <span className="mini-dot" />
                <b>{person.person_id}</b>
                <span>±{person.horizontal_error_m} m</span>
                <span>{Math.round((person.confidence || 0) * 100)}%</span>
              </div>
            ))
          ) : (
            <p className="muted">No individual sightings are available for this cell.</p>
          )}
        </div>
      </div>
    );
  }
  const { properties, lat, lon } = selected;
  return (
    <div className="selection-content">
      <div className="selection-heading">
        <div>
          <span className="eyebrow">ESTIMATED SIGHTING</span>
          <h3>{properties.person_id}</h3>
        </div>
        <button className="icon-button" aria-label="Close selection" onClick={onClose}>
          <X size={15} />
        </button>
      </div>
      <div className="detail-grid">
        <span>Position</span>
        <b>{lat.toFixed(5)}, {lon.toFixed(5)}</b>
        <span>Estimated error</span>
        <b>±{properties.horizontal_error_m} m</b>
        <span>Confidence</span>
        <b>{Math.round((properties.confidence || 0) * 100)}%</b>
        <span>Last seen</span>
        <b>{formatTime(properties.last_seen_utc)}</b>
      </div>
    </div>
  );
}

function PipelineVisualizer({ status, manifest, data, onNodeSelect }) {
  const uploader = status?.uploader;
  const people = (data?.heatmap?.features || []).filter(
    (feature) => feature.properties?.kind === "person",
  );
  const meanConfidence = people.length
    ? Math.round(
        (people.reduce((sum, feature) => sum + (feature.properties.confidence || 0), 0) /
          people.length) *
          100,
      )
    : null;
  const onCloud = status?.role === "cloud" || data?.source === "cloud";
  const summary = manifest?.summary || {};
  const cloudTone = !uploader
    ? onCloud && manifest
      ? "ready"
      : "not-configured"
    : uploader.online === false
      ? "offline"
      : uploader.pending
        ? "waiting"
        : "ready";
  const nodes = [
    {
      id: "capture",
      title: "Video input",
      icon: Camera,
      state: status?.running ? "running" : manifest ? "ready" : "waiting",
      value: `${status?.frames ?? manifest?.summary?.frames_processed ?? 0} frames`,
      note: status?.running ? "Processing frames" : "Replay or camera",
    },
    {
      id: "detection",
      title: "Detect + track",
      icon: Cpu,
      state: status?.running ? "running" : manifest ? "ready" : "waiting",
      value: `${status?.detections ?? manifest?.summary?.detections_processed ?? 0} detections`,
      note: status?.scheduler?.mode ? `Scheduler · ${status.scheduler.mode}` : "Person detections",
    },
    {
      id: "geolocation",
      title: "Locate + aggregate",
      icon: Crosshair,
      state: status?.telemetry_samples ? "ready" : manifest ? "ready" : "waiting",
      value: status?.role === "edge"
        ? `${status.telemetry_samples ?? 0} telemetry samples`
        : manifest
          ? `${summary.estimated_people ?? 0} located · ${summary.unlocated_tracks ?? 0} unlocated`
          : "Telemetry unavailable",
      note: manifest?.position_method || "Waiting for telemetry",
      confidence: meanConfidence,
    },
    {
      id: "storage",
      title: "Local snapshot",
      icon: Database,
      state: manifest ? "ready" : "waiting",
      value: manifest ? `Version ${manifest.version}` : "No snapshot",
      note: "Saved on edge",
    },
    {
      id: "cloud",
      title: "Cloud sync",
      icon: PackageCheck,
      state: cloudTone,
      value: uploader
        ? uploader.pending
          ? `v${uploader.local_version} pending`
          : `v${uploader.acked_version} acknowledged`
        : onCloud && manifest
          ? `v${manifest.version} received`
          : "Not configured",
      note: uploader?.online === false
        ? "Offline · saved locally"
        : !uploader && onCloud && data?.received_at
          ? `Received ${formatTime(data.received_at)}`
          : "Retry-safe upload",
    },
  ];

  return (
    <section className="pipeline-panel" aria-label="Mission data pipeline">
      <div className="pipeline-heading">
        <div>
          <span className="eyebrow">HOW THIS MISSION FLOWS</span>
          <h2>Live data pipeline</h2>
        </div>
        <span className="pipeline-updated">
          <span className={`state-square ${status?.running ? "running" : ""}`} />
          {status?.running ? "Processing" : manifest ? "Latest mission state" : "Waiting for input"}
        </span>
      </div>
      <div className="pipeline-track">
        {nodes.map((node, index) => {
          const Icon = node.icon;
          return (
            <Fragment key={node.id}>
              <button
                type="button"
                className={`pipeline-node ${node.state}`}
                onClick={() => onNodeSelect(node.id)}
                aria-label={`${node.title}: ${node.value}. ${node.note}`}
              >
                <span className="pipeline-node-top">
                  <Icon size={16} />
                  <span className="pipeline-node-state">{node.state.replace("-", " ")}</span>
                </span>
                <strong>{node.title}</strong>
                <span className="pipeline-value">{node.value}</span>
                <span className="pipeline-note">{node.note}</span>
                {node.confidence !== null && node.confidence !== undefined && (
                  <span
                    className="confidence-meter"
                    role="meter"
                    aria-label="Mean sighting confidence"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={node.confidence}
                  >
                    <i style={{ width: `${node.confidence}%` }} />
                    <small>Mean sighting confidence {node.confidence}%</small>
                  </span>
                )}
              </button>
              {index < nodes.length - 1 && (
                <div
                  className={`pipeline-connector ${index === 3 && cloudTone === "offline" ? "paused" : ""}`}
                  aria-hidden="true"
                >
                  {!(index === 3 && cloudTone === "offline") && (
                    <span
                      key={`${manifest?.version ?? 0}-${index}`}
                      className="flow-packet"
                      style={{ animationDelay: `${index * 180}ms` }}
                    />
                  )}
                  <ArrowRight size={14} />
                </div>
              )}
            </Fragment>
          );
        })}
      </div>
      <div className="pipeline-caption">
        <span><i className="legend-key ready" /> Available</span>
        <span><i className="legend-key waiting" /> Waiting / queued</span>
        <span><i className="legend-key offline" /> Offline</span>
        <button type="button" className="pipeline-help" onClick={() => onNodeSelect("safety")}>
          <AlertTriangle size={13} /> Estimates require human review
        </button>
      </div>
    </section>
  );
}

function MissionControl() {
  const [status, setStatus] = useState(null);
  const [missions, setMissions] = useState([]);
  const [missionId, setMissionId] = useState("");
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(null);
  const [windowSeconds, setWindowSeconds] = useState(0);
  const [showCells, setShowCells] = useState(true);
  const [showPeople, setShowPeople] = useState(true);
  const [showOsm, setShowOsm] = useState(false);
  const [report, setReport] = useState(null);
  const [automatedReport, setAutomatedReport] = useState(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [now, setNow] = useState(new Date());
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [demoMode, setDemoMode] = useState("normal");
  const [demoBusy, setDemoBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [pipelineDetail, setPipelineDetail] = useState(null);
  const [unlocated, setUnlocated] = useState(null);
  const [flightPath, setFlightPath] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const [nextStatus, nextMissions] = await Promise.all([
        getJSON("/status"),
        getJSON("/missions"),
      ]);
      setStatus(nextStatus);
      setMissions(nextMissions);
      const chosen =
        missionId && nextMissions.some((mission) => mission.mission_id === missionId)
          ? missionId
          : nextStatus.mission_id && nextMissions.some((m) => m.mission_id === nextStatus.mission_id)
            ? nextStatus.mission_id
            : nextMissions[0]?.mission_id || "";
      if (chosen !== missionId) setMissionId(chosen);
      if (chosen) {
        const encodedMission = encodeURIComponent(chosen);
        const [latestResult, reportResult] = await Promise.allSettled([
          getJSON(`/missions/${encodedMission}/latest`),
          nextStatus.role === "cloud"
            ? getJSON(`/missions/${encodedMission}/ai-report`)
            : Promise.resolve(null),
        ]);
        if (latestResult.status === "fulfilled") {
          const latest = latestResult.value;
          setData((current) =>
            current?.manifest?.version === latest.manifest.version &&
            current?.manifest?.mission_id === latest.manifest.mission_id
              ? current
              : latest,
          );
        } else {
          throw latestResult.reason;
        }
        setAutomatedReport(
          reportResult.status === "fulfilled" ? reportResult.value : null,
        );
      } else {
        setData(null);
        setAutomatedReport(null);
      }
      setError("");
    } catch (reason) {
      setError(reason.message || "Unable to connect to the mission API.");
    }
  }, [missionId]);

  useEffect(() => {
    refresh();
    const poll = window.setInterval(refresh, POLL_MS);
    const clock = window.setInterval(() => setNow(new Date()), 1000);
    return () => {
      window.clearInterval(poll);
      window.clearInterval(clock);
    };
  }, [refresh]);

  useEffect(() => {
    function onKeyDown(event) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    if (!notice) return undefined;
    const timer = window.setTimeout(() => setNotice(""), 3800);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const manifest = data?.manifest;
  const summary = manifest?.summary || {};
  const edge = status?.role === "edge";

  // Unlocated sightings are only in sightings.csv (the map GeoJSON has located people only).
  // Fetch the exact snapshot version on screen, once per version.
  const snapshotKey = manifest ? `${manifest.mission_id}@${manifest.version}` : "";
  useEffect(() => {
    if (!manifest) {
      setUnlocated(null);
      return undefined;
    }
    let cancelled = false;
    fetch(`${API}/missions/${encodeURIComponent(manifest.mission_id)}/files/sightings.csv?version=${manifest.version}`)
      .then((response) => (response.ok ? response.text() : Promise.reject(new Error(response.status))))
      .then((text) => {
        if (cancelled) return;
        setUnlocated(
          parseCsv(text)
            .filter((row) => row.status === "unlocated_sighting")
            .sort((a, b) => (b.last_seen_utc || "").localeCompare(a.last_seen_utc || "")),
        );
      })
      .catch(() => !cancelled && setUnlocated([]));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshotKey]);

  // Drone track (cloud: drone_telemetry time-series table; edge: the snapshot's flight_path.csv).
  useEffect(() => {
    if (!manifest) {
      setFlightPath(null);
      return undefined;
    }
    let cancelled = false;
    getJSON(`/missions/${encodeURIComponent(manifest.mission_id)}/flight-path`)
      .then((result) => !cancelled && setFlightPath(result))
      .catch(() => !cancelled && setFlightPath(null));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshotKey]);
  const uploader = status?.uploader;

  const reportUrl = missionId
    ? `${API}/missions/${encodeURIComponent(missionId)}/files/report.md`
    : "#";
  const exportUrl = missionId
    ? `${API}/missions/${encodeURIComponent(missionId)}/files/sightings.csv`
    : "#";
  const geoJsonUrl = missionId
    ? `${API}/missions/${encodeURIComponent(missionId)}/files/heatmap.geojson`
    : "#";

  async function setUplink(offline) {
    if (!edge || demoBusy) return;
    setDemoBusy(true);
    setDemoMode(offline ? "outage" : "normal");
    try {
      await getJSON("/network", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ offline }),
      });
      setNotice(offline ? "Outage simulation enabled. Edge capture continues locally." : "Normal operation restored.");
      await refresh();
    } catch (reason) {
      setError(reason.message);
      setNotice(`Demo control failed: ${reason.message}`);
    } finally {
      setDemoBusy(false);
    }
  }

  async function injectUploadFault() {
    if (!edge || demoBusy) return;
    setDemoBusy(true);
    try {
      await getJSON("/demo/corrupt-next-upload", { method: "POST" });
      setDemoMode("fault-armed");
      setNotice(
        uploader?.pending
          ? "Upload integrity test armed. The next upload attempt will be rejected; retry is automatic."
          : "Upload integrity test armed for the next snapshot upload.",
      );
    } catch (reason) {
      setError(reason.message);
      setNotice(`Upload test unavailable: ${reason.message}`);
    } finally {
      setDemoBusy(false);
    }
  }

  function runPaletteAction(action) {
    setPaletteOpen(false);
    action();
  }

  const pipelineDetails = {
    capture: "Video or replay frames enter the edge pipeline. Counts are reported by the running edge service.",
    detection: "The configured detector and tracker run on the edge. Counts reflect detections reported by the pipeline; this is not a person identity system.",
    geolocation: "Timestamped telemetry is combined with detections to estimate positions. Missing or invalid fixes remain unlocated rather than being assigned a guessed coordinate.",
    storage: "Mission summaries are written as versioned local snapshots so the edge map remains available without a network.",
    cloud: "The uploader sends snapshots when a link is available. The outage mode demonstrates local capture and retry after reconnection.",
    safety: "Coordinates are approximate, counts are estimates, and the system does not assign rescue priority. A human must review every result.",
  };
  const pipelineNode = pipelineDetail === "safety"
    ? "Safety note"
    : pipelineDetail
      ? pipelineDetail.charAt(0).toUpperCase() + pipelineDetail.slice(1)
      : "";

  async function requestAiReport() {
    if (!missionId) return;
    setReportLoading(true);
    setReport(null);
    try {
      const result = await getJSON(`/missions/${encodeURIComponent(missionId)}/ai-report`, { method: "POST" });
      setReport(result);
      setAutomatedReport(result);
    } catch (reason) {
      // 429 = generated moments ago (REPORT_MIN_GAP_S); the message says when to retry.
      setNotice(`Report not generated: ${errorDetail(reason)}`);
    } finally {
      setReportLoading(false);
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#/story" aria-label="RescueSight project overview">
          <span className="brand-mark"><Crosshair size={19} /></span>
          <span>rescue<span className="brand-accent">sight</span></span>
          <span className="brand-divider" />
          <span className="brand-context">MISSION CONTROL</span>
        </a>
        <div className="topbar-right">
          <span className={`connection-state ${error ? "danger" : edge && uploader?.online === false ? "warning" : "success"}`}>
            <i />
            {error ? "API unavailable" : edge ? (uploader?.online === false ? "Offline capture" : "Edge connected") : "Cloud connected"}
          </span>
          <div className="topbar-clock">
            <Clock3 size={14} />
            <span>{now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span>
            <span className="clock-zone">LOCAL</span>
          </div>
          <span className="operator-avatar">RS</span>
        </div>
      </header>

      <div className="command-strip">
        <div className="command-title">
          <div className="breadcrumb"><span>OPERATIONS</span><span>/</span><b>LIVE OVERVIEW</b></div>
          <h1>Search area overview</h1>
          <p>{manifest ? `Mission ${manifest.mission_id} · last updated ${formatTime(manifest.created_at)}` : "Mission data will appear here when the edge pipeline starts."}</p>
        </div>
        <div className="command-controls">
          <label className="mission-select">
            <span>ACTIVE MISSION</span>
            <span className="select-wrap">
              <select
                value={missionId}
                onChange={(event) => {
                  setMissionId(event.target.value);
                  setData(null);
                  setSelected(null);
                }}
                aria-label="Select mission"
              >
                {!missions.length && <option value="">Waiting for mission data</option>}
                {missions.map((mission) => (
                  <option value={mission.mission_id} key={mission.mission_id}>
                    {mission.mission_id}
                  </option>
                ))}
              </select>
              <ChevronDown size={15} />
            </span>
          </label>
          <button className="button button-quiet" onClick={refresh} title="Refresh mission data">
            <RefreshCw size={15} />
            <span>Refresh</span>
          </button>
          <button className="button button-quiet palette-trigger" onClick={() => setPaletteOpen(true)} title="Open command menu">
            <CommandIcon size={14} /><span>Actions</span><kbd>Ctrl K</kbd>
          </button>
        </div>
      </div>

      {edge && uploader && (
        <div className="chaos-dock" role="group" aria-label="One-click demo controls">
          <span className="dock-label"><Network size={14} /> DEMO CONTROLS</span>
          <button
            type="button"
            className={`dock-action ${demoMode === "normal" ? "selected" : ""}`}
            onClick={() => setUplink(false)}
            disabled={demoBusy || !status?.network_demo_available}
          >
            <Wifi size={14} /> Normal operation
          </button>
          <button
            type="button"
            className={`dock-action ${demoMode === "outage" || uploader.online === false ? "selected warning" : ""}`}
            onClick={() => setUplink(true)}
            disabled={demoBusy || !status?.network_demo_available || uploader.online === false}
          >
            <WifiOff size={14} /> Simulate outage
          </button>
          <button
            type="button"
            className={`dock-action ${demoMode === "fault-armed" ? "selected warning" : ""}`}
            onClick={injectUploadFault}
            disabled={demoBusy}
            title="The next upload is deliberately corrupted to demonstrate integrity rejection and retry."
          >
            <AlertTriangle size={14} /> Test upload integrity
          </button>
          {!status?.network_demo_available && <span className="dock-help">Network toggles need an offline flag; upload integrity test remains available.</span>}
        </div>
      )}

      {notice && <div className="notice-toast" role="status">{notice}</div>}

      {error && (
        <motion.div className="error-banner" initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }}>
          <AlertTriangle size={16} />
          <span>Mission API unavailable: {error}</span>
          <button onClick={refresh}>Retry</button>
        </motion.div>
      )}

      <main className="dashboard">
        <section className="overview-grid" aria-label="Mission metrics">
          <Metric label="Estimated sightings" value={summary.estimated_people} icon={MapPin} tone="mint" />
          <Metric label="Without location" value={summary.unlocated_tracks} icon={Satellite} tone="amber" />
          <Metric label="Median estimated uncertainty" value={summary.median_error_m} suffix="m" icon={Crosshair} />
          <Metric label="90th pct. estimated uncertainty" value={summary.p90_error_m} suffix="m" icon={Crosshair} tone="amber" />
          <Metric label="Occupied grid cells" value={summary.occupied_cells} icon={Layers3} />
          <Metric label="Mission status" value={status?.running ? "Active" : manifest ? "Ready" : "Waiting"} icon={Activity} tone={status?.running ? "mint" : ""} />
        </section>

        <PipelineVisualizer status={status} manifest={manifest} data={data} onNodeSelect={setPipelineDetail} />

        <section className="workspace">
          <div className="map-panel">
            <div className="map-toolbar">
              <div className="map-heading">
                <span className="live-indicator"><i /> {status?.running ? "LIVE FEED" : "MISSION MAP"}</span>
                <span className="map-subtitle">
                  {manifest ? `${manifest.drone_id} · snapshot v${manifest.version}` : "No mission snapshot yet"}
                </span>
              </div>
              <div className="map-tools">
                <label className="filter-control">
                  <Clock3 size={14} />
                  <select value={windowSeconds} onChange={(event) => setWindowSeconds(Number(event.target.value))} aria-label="Filter sightings by last seen time">
                    {WINDOWS.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}
                  </select>
                  <ChevronDown size={13} />
                </label>
                <button className={`map-tool ${showCells ? "active" : ""}`} onClick={() => setShowCells(!showCells)} aria-pressed={showCells}>
                  <Layers3 size={14} /><span>Heat grid</span>
                </button>
                <button className={`map-tool ${showOsm ? "active" : ""}`} onClick={() => setShowOsm(!showOsm)} aria-pressed={showOsm} title="OpenStreetMap tiles require internet">
                  <MapPin size={14} /><span>Basemap</span>
                </button>
              </div>
            </div>
            <div className="map-frame">
              {data?.heatmap ? (
                <MissionMap
                  data={data}
                  showCells={showCells}
                  showPeople={showPeople}
                  showOsm={showOsm}
                  windowSeconds={windowSeconds}
                  onSelect={setSelected}
                  flightPath={flightPath}
                />
              ) : (
                <div className="map-empty">
                  <div className="empty-radar"><Crosshair size={25} /></div>
                  <h2>{error ? "Connection interrupted" : "Waiting for mission data"}</h2>
                  <p>{error ? "The local map will return when the API is reachable." : "Start the edge pipeline or upload a mission snapshot to populate this map."}</p>
                  {error && <button className="button button-quiet" onClick={refresh}><RefreshCw size={14} /> Retry connection</button>}
                </div>
              )}
              <div className="map-coordinate"><Crosshair size={12} /> WGS84 · {manifest?.position_method || "Awaiting position fix"}</div>
              {showOsm && <div className="map-basemap-note">© OpenStreetMap contributors · online tiles</div>}
              <div className="map-legend">
                <span><i className="legend-dot" /> Estimated sighting</span>
                <span><i className="legend-ring" /> Position error</span>
                {flightPath?.features?.length > 0 && (
                  <span title={`${flightPath.properties.samples} drone GPS samples, ${flightPath.properties.gap_samples} without a valid fix (gaps)`}>
                    <i className="legend-path" /> Drone path (not sightings)
                  </span>
                )}
                {showCells && <span className="legend-scale"><i /> Density</span>}
              </div>
            </div>
            <div className="map-footer">
              <button
                type="button"
                role="switch"
                aria-checked={showPeople}
                className={`switch-control ${showPeople ? "on" : ""}`}
                onClick={() => setShowPeople((visible) => !visible)}
              >
                <i />
                <span>Show sighting markers</span>
              </button>
              <span>{manifest ? `${summary.frames_processed || 0} frames processed · ${manifest.cell_size_m} m grid` : "Local grid is available offline"}</span>
            </div>
          </div>

          <aside className="side-panel">
            <div className="panel-section mission-health">
              <div className="section-heading">
                <div><span className="eyebrow">MISSION TELEMETRY</span><h2>System status</h2></div>
                <Radio size={17} />
              </div>
              <div className="health-list">
                <div className="health-row">
                  <span className="health-icon"><Activity size={14} /></span>
                  <span>Detection pipeline</span>
                  <StatusPill tone={status?.running ? "success" : manifest ? "neutral" : "warning"}>{status?.running ? "Running" : manifest ? "Ready" : "Idle"}</StatusPill>
                </div>
                <div className="health-row">
                  <span className="health-icon"><Wifi size={14} /></span>
                  <span>Cloud sync</span>
                  <StatusPill tone={!edge ? "success" : uploader?.pending ? "warning" : uploader?.online ? "success" : "neutral"}>
                    {!edge ? "Connected" : uploader?.pending ? "Pending" : uploader?.online === false ? "Offline" : "Synced"}
                  </StatusPill>
                </div>
                {edge ? (
                  <div className="health-row">
                    <span className="health-icon"><Satellite size={14} /></span>
                    <span>Telemetry samples</span>
                    <b className="health-value">{status?.telemetry_samples ?? "—"}</b>
                  </div>
                ) : (
                  <div className="health-row">
                    <span className="health-icon"><PackageCheck size={14} /></span>
                    <span>Last snapshot received</span>
                    <b className="health-value">
                      {manifest ? `v${manifest.version} · ${formatTime(data?.received_at)}` : "—"}
                    </b>
                  </div>
                )}
                <div className="health-row">
                  <span className="health-icon"><Clock3 size={14} /></span>
                  <span>Last observation</span>
                  <b className="health-value">{formatTime(manifest?.data_window?.end)}</b>
                </div>
              </div>
              {uploader?.needs_attention ? (
                <div className="sync-callout"><AlertTriangle size={14} /><span>Needs attention: {uploader.last_error}</span></div>
              ) : uploader?.pending && (
                <div className="sync-callout"><AlertTriangle size={14} /><span>Local v{uploader.local_version}; cloud v{uploader.acked_version}. Upload will retry automatically.</span></div>
              )}
            </div>

            <div className="panel-section">
              <div className="section-heading">
                <div><span className="eyebrow">REVIEW DETAILS</span><h2>Map selection</h2></div>
                <button className="text-button" onClick={() => setSelected(null)} disabled={!selected}>Clear</button>
              </div>
              <SelectionPanel selected={selected} onClose={() => setSelected(null)} />
            </div>

            <div className="panel-section">
              <div className="section-heading">
                <div><span className="eyebrow">NOT ON THE MAP</span><h2>Sightings without location ({unlocated ? unlocated.length : "…"})</h2></div>
              </div>
              <p className="muted unlocated-note">
                Kept with the reason no position was estimated. Never placed at the drone&apos;s
                position; may be the same people as mapped sightings.
              </p>
              <UnlocatedList rows={unlocated} />
            </div>

            <div className="panel-section report-section">
              <div className="section-heading">
                <div><span className="eyebrow">MISSION INTELLIGENCE</span><h2>Reports &amp; exports</h2></div>
                <Bot size={17} />
              </div>
              <p>Download auditable mission data or request an optional AI-generated summary.</p>
              <div className="export-links">
                <a href={exportUrl} download><ArrowDownToLine size={14} /> Sightings CSV</a>
                <a href={geoJsonUrl} download><ArrowDownToLine size={14} /> GeoJSON</a>
                <a href={reportUrl} target="_blank" rel="noreferrer"><ExternalLink size={14} /> Deterministic report</a>
              </div>
              {status?.role === "cloud" && (
                <>
                  <div className="automated-report-status" role="status" aria-live="polite">
                    <b>Mission report</b>
                    <span>{describeReport(automatedReport)}</span>
                    {automatedReport?.report && <button className="text-button" onClick={() => setReport(automatedReport)}>Read report</button>}
                  </div>
                  <button className="button button-ai" onClick={requestAiReport} disabled={!missionId || reportLoading}>
                    {reportLoading ? <RefreshCw className="spin" size={15} /> : <Bot size={15} />}
                    {reportLoading ? "Preparing report…" : "Generate fresh summary"}
                  </button>
                </>
              )}
            </div>
          </aside>
        </section>
      </main>

      <footer className="safety-footer">
        <ShieldAlert size={14} />
        <span>Decision support only · locations are estimates · human review required</span>
        <span className="footer-source">{data?.source === "edge" ? "EDGE NODE" : "MISSION CLOUD"} {manifest ? `· UPDATED ${formatTime(manifest.created_at)}` : ""}</span>
      </footer>

      {pipelineDetail && (
        <div className="modal-backdrop" onClick={() => setPipelineDetail(null)}>
          <section className="report-modal pipeline-modal" role="dialog" aria-modal="true" aria-labelledby="pipeline-modal-title" onClick={(event) => event.stopPropagation()}>
            <div className="report-modal-heading">
              <div><span className="eyebrow">PIPELINE COMPONENT</span><h2 id="pipeline-modal-title">{pipelineNode}</h2></div>
              <button className="icon-button" onClick={() => setPipelineDetail(null)} aria-label="Close details"><X size={17} /></button>
            </div>
            <p>{pipelineDetails[pipelineDetail]}</p>
          </section>
        </div>
      )}

      <Command.Dialog
        open={paletteOpen}
        onOpenChange={setPaletteOpen}
        label="Mission actions"
        className="command-dialog"
      >
        <div className="command-dialog-panel">
          <div className="command-dialog-search">
            <CommandIcon size={17} />
            <Command.Input autoFocus placeholder="Search mission actions…" />
            <kbd>ESC</kbd>
          </div>
          <Command.List>
            <Command.Empty>No matching actions.</Command.Empty>
            <Command.Group heading="Mission">
                <Command.Item onSelect={() => runPaletteAction(refresh)}>
                  <RefreshCw size={15} /> Refresh mission data
              </Command.Item>
              <Command.Item onSelect={() => runPaletteAction(() => setShowCells((visible) => !visible))}>
                <Layers3 size={15} /> Toggle heat grid
              </Command.Item>
              <Command.Item onSelect={() => runPaletteAction(() => setShowPeople((visible) => !visible))}>
                <MapPin size={15} /> Toggle sighting markers
              </Command.Item>
              <Command.Item onSelect={() => runPaletteAction(() => setShowOsm((visible) => !visible))}>
                <Crosshair size={15} /> Toggle online basemap
              </Command.Item>
            </Command.Group>
            {edge && uploader && (
              <Command.Group heading="Demo controls">
                <Command.Item onSelect={() => runPaletteAction(() => setUplink(false))}>
                  <Wifi size={15} /> Normal operation
                </Command.Item>
                <Command.Item onSelect={() => runPaletteAction(() => setUplink(true))}>
                  <WifiOff size={15} /> Simulate network outage
                </Command.Item>
                <Command.Item onSelect={() => runPaletteAction(injectUploadFault)}>
                  <AlertTriangle size={15} /> Test upload integrity
                </Command.Item>
              </Command.Group>
            )}
            <Command.Group heading="Data">
              <Command.Item onSelect={() => runPaletteAction(() => window.location.assign(exportUrl))}>
                <ArrowDownToLine size={15} /> Download sightings CSV
              </Command.Item>
              <Command.Item onSelect={() => runPaletteAction(() => window.location.assign(geoJsonUrl))}>
                <ArrowDownToLine size={15} /> Download GeoJSON
              </Command.Item>
              {status?.role === "cloud" && (
                <Command.Item onSelect={() => runPaletteAction(requestAiReport)}>
                  <Bot size={15} /> Generate AI summary
                </Command.Item>
              )}
            </Command.Group>
          </Command.List>
          <div className="command-dialog-footer"><span>Navigate with ↑ ↓</span><span>Run with Enter</span><span>Close with Esc</span></div>
        </div>
      </Command.Dialog>

      <AnimatePresence>
        {report && (
          <motion.div className="modal-backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={() => setReport(null)}>
            <motion.section className="report-modal" initial={{ opacity: 0, y: 20, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 12 }} onClick={(event) => event.stopPropagation()}>
              <div className="report-modal-heading">
                <div><span className="eyebrow">MISSION REPORT</span><h2>{report.generated_by === "gemini" ? "AI-assisted summary" : "Deterministic summary"}</h2></div>
                <button className="icon-button" onClick={() => setReport(null)} aria-label="Close report"><X size={17} /></button>
              </div>
              {report.fallback_reason && <StatusPill tone="warning">AI unavailable: {report.fallback_reason}</StatusPill>}
              <pre className="report-copy">{report.report}</pre>
              <p className="report-safety"><ShieldAlert size={14} /> Estimated observations only. Human review required.</p>
            </motion.section>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// The landing page is the home page; mission control lives at #/dashboard.
const isDashboard = () => window.location.hash.startsWith("#/dashboard");

function App() {
  const [storyPage, setStoryPage] = useState(!isDashboard());

  useEffect(() => {
    document.body.classList.toggle("story-mode", storyPage);
    return () => document.body.classList.remove("story-mode");
  }, [storyPage]);

  useEffect(() => {
    function syncPage() {
      setStoryPage(!isDashboard());
    }
    window.addEventListener("hashchange", syncPage);
    return () => window.removeEventListener("hashchange", syncPage);
  }, []);

  function openDashboard() {
    window.location.hash = "/dashboard";
  }

  return storyPage
    ? <Landing onDashboard={openDashboard} />
    : <MissionControl />;
}

export default App;
