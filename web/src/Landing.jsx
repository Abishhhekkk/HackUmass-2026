// RescueSight landing page (#/story): what the system is, shown with the live mission data.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { contours } from "d3-contour";
import { ArrowRight, ArrowUpRight, Send } from "lucide-react";
import "@fontsource/instrument-serif/400.css";
import "@fontsource/instrument-serif/400-italic.css";
import "@fontsource-variable/hanken-grotesk";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./landing.css";
import { EDGE_API, MissionMap, describeReport, formatTime, getEdgeJSON, getJSON } from "./shared.jsx";

const REFRESH_MS = 5000;

// ----------------------------------------------------------------------------------- terrain
// A seeded, smooth height field turned into contour lines: the schematic "ground" of the hero.
function seeded(seed) {
  let t = seed >>> 0;
  return () => {
    t += 0x6d2b79f5;
    let r = Math.imul(t ^ (t >>> 15), 1 | t);
    r ^= r + Math.imul(r ^ (r >>> 7), 61 | r);
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
}

const GRID_W = 160;
const GRID_H = 90;

function terrainPaths() {
  const rand = seeded(20261114);
  const hills = Array.from({ length: 26 }, () => ({
    x: rand() * GRID_W, y: rand() * GRID_H, s: 60 + rand() * 520, a: (rand() - 0.35) * 1.6,
  }));
  const values = new Float64Array(GRID_W * GRID_H);
  for (let j = 0; j < GRID_H; j += 1) {
    for (let i = 0; i < GRID_W; i += 1) {
      let v = 0.35 * Math.sin(i / 23 + j / 31) + 0.25 * Math.cos(i / 41 - j / 17);
      for (const h of hills) v += h.a * Math.exp(-((i - h.x) ** 2 + (j - h.y) ** 2) / h.s);
      values[j * GRID_W + i] = v;
    }
  }
  const levels = contours().size([GRID_W, GRID_H]).thresholds(26)(values);
  return levels.map((level, index) => ({
    major: index % 5 === 0,
    d: level.coordinates
      .flatMap((polygon) => polygon.map((ring) =>
        `M${ring.map(([x, y]) => `${(x * 10).toFixed(1)},${(y * 10).toFixed(1)}`).join("L")}Z`))
      .join(""),
  }));
}

// ----------------------------------------------------------------------------------- field scene
const VIEW_W = 1600;
const VIEW_H = 900;
// data area: between the headline (left) and the floating cards (right) on wide screens
const BOX = { x0: 690, x1: 1090, y0: 180, y1: 780 };

function projector(points) {
  if (!points.length) return null;
  const lat0 = points.reduce((sum, p) => sum + p[1], 0) / points.length;
  const kx = Math.cos((lat0 * Math.PI) / 180) * 111320;
  const ky = 110540;
  const xs = points.map((p) => p[0] * kx);
  const ys = points.map((p) => p[1] * ky);
  const [minX, maxX, minY, maxY] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const spanX = Math.max(maxX - minX, 20);
  const spanY = Math.max(maxY - minY, 20);
  const scale = Math.min((BOX.x1 - BOX.x0) / spanX, (BOX.y1 - BOX.y0) / spanY);
  const offX = BOX.x0 + ((BOX.x1 - BOX.x0) - spanX * scale) / 2;
  const offY = BOX.y0 + ((BOX.y1 - BOX.y0) - spanY * scale) / 2;
  return {
    metresToPx: scale,
    project: ([lon, lat]) => [offX + (lon * kx - minX) * scale, offY + (maxY - lat * ky) * scale],
  };
}

function FieldScene({ data, flightPath, telemetry }) {
  const terrain = useMemo(terrainPaths, []);
  const people = useMemo(
    () => (data?.heatmap?.features || []).filter((f) => f.properties?.kind === "person"),
    [data],
  );
  const segments = useMemo(() => (flightPath?.features || []).map((f) => f.geometry.coordinates), [flightPath]);
  const drone = telemetry?.available && telemetry.position
    ? [telemetry.position.lon, telemetry.position.lat]
    : null;
  const geo = useMemo(() => projector([
    ...people.map((f) => f.geometry.coordinates),
    ...segments.flat(),
    ...(drone ? [drone] : []),
  ]), [people, segments, drone?.[0], drone?.[1]]); // eslint-disable-line react-hooks/exhaustive-deps

  const tiles = [];
  for (let r = 0; r < 5; r += 1) {
    for (let c = 0; c < 8; c += 1) tiles.push({ r, c });
  }
  const tileW = (BOX.x1 - BOX.x0 + 80) / 8;
  const tileH = (BOX.y1 - BOX.y0 + 80) / 5;

  return (
    <svg className="lp-scene" viewBox={`0 0 ${VIEW_W} ${VIEW_H}`} preserveAspectRatio="xMidYMid slice"
      role="img" aria-label="Schematic terrain with the latest estimated sightings and drone track">
      <defs>
        <radialGradient id="lp-glow" cx="68%" cy="48%" r="60%">
          <stop offset="0" stopColor="#3a463d" />
          <stop offset="1" stopColor="#151a17" />
        </radialGradient>
        <linearGradient id="lp-fade" x1="0" x2="1">
          <stop offset="0" stopColor="#121614" stopOpacity="0.92" />
          <stop offset="0.45" stopColor="#121614" stopOpacity="0.35" />
          <stop offset="1" stopColor="#121614" stopOpacity="0" />
        </linearGradient>
      </defs>
      <rect width={VIEW_W} height={VIEW_H} fill="url(#lp-glow)" />
      <g className="lp-contours">
        {terrain.map((level, i) => (
          <path key={i} d={level.d} className={level.major ? "major" : undefined} />
        ))}
      </g>
      <g className="lp-tiles">
        {tiles.map(({ r, c }) => (
          <rect key={`${r}-${c}`} x={BOX.x0 - 40 + c * tileW} y={BOX.y0 - 40 + r * tileH}
            width={tileW} height={tileH} style={{ "--i": r + c }} />
        ))}
      </g>
      <rect width={VIEW_W} height={VIEW_H} fill="url(#lp-fade)" />
      {geo && (
        <g className="lp-data">
          {segments.map((seg, i) => (
            <polyline key={i} className="lp-track" points={seg.map((p) => geo.project(p).join(",")).join(" ")} />
          ))}
          {people.map((f) => {
            const [x, y] = geo.project(f.geometry.coordinates);
            const ring = Math.min(70, Math.max(9, (f.properties.horizontal_error_m || 3) * geo.metresToPx));
            return (
              <g key={f.properties.person_id} className="lp-person" transform={`translate(${x} ${y})`}>
                <circle r={ring} className="ring" />
                <circle r="5" className="core" />
              </g>
            );
          })}
          {drone && (() => {
            const [x, y] = geo.project(drone);
            return (
              <g className="lp-drone" transform={`translate(${x} ${y}) rotate(${telemetry.heading_deg || 0})`}>
                <circle r="16" />
                <path d="M0,-11 L7,8 L0,4 L-7,8 Z" />
              </g>
            );
          })()}
        </g>
      )}
    </svg>
  );
}

// ----------------------------------------------------------------------------------- helpers
function useReveal() {
  useEffect(() => {
    const items = document.querySelectorAll(".lp [data-reveal]");
    if (!("IntersectionObserver" in window)) {
      items.forEach((el) => el.classList.add("in"));
      return undefined;
    }
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          observer.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -8% 0px" });
    items.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  });
}

const fmt = (value, digits = 0) =>
  value === null || value === undefined || Number.isNaN(value) ? "—" : Number(value).toFixed(digits);

const MEASURED = [
  { value: "5%", to: "47.5%", label: "of people found in aerial validation images", note: "Stock YOLOv8n on VisDrone val: whole frame at 640 px, then 320 px tiles + full frame" },
  { value: "8.8", unit: "fps", label: "tiled detection on the Jetson, 1080p", note: "Orin Nano Super, TensorRT FP16, 640 px tiles (9 engine runs per frame)" },
  { value: "55", unit: "fps", label: "whole-frame detection, same board", note: "18 ms per 1920×1080 frame" },
  { value: "12/12", label: "end-to-end checks against Tiger Data", note: "Upload → FastAPI → Postgres 18 + TimescaleDB hypertable" },
];

const STEPS = [
  { id: "capture", title: "Capture", text: "The drone's own camera feed and flight log. Nothing on the aircraft needs replacing." },
  { id: "detect", title: "Detect", text: "YOLOv8n on the Jetson's GPU, tiled so people a few pixels tall still register." },
  { id: "locate", title: "Locate", text: "Camera geometry plus position and heading from the log. No fix, no coordinate: never guessed." },
  { id: "keep", title: "Keep", text: "Every result becomes a versioned, checksummed snapshot stored on board." },
  { id: "sync", title: "Sync", text: "When any link appears, only the newest snapshot goes up. Kilobytes, never video." },
];

const QUESTIONS = [
  "Where are the densest estimated sightings?",
  "Which sightings have no location, and why?",
  "Summarise the mission for a coordinator.",
];

// ----------------------------------------------------------------------------------- page
export default function Landing({ onDashboard }) {
  const [status, setStatus] = useState(null);
  const [edgeStatus, setEdgeStatus] = useState(null);
  const [missions, setMissions] = useState([]);
  const [missionId, setMissionId] = useState("");
  const [data, setData] = useState(null);
  const [telemetry, setTelemetry] = useState(null);
  const [flightPath, setFlightPath] = useState(null);
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");
  const [demoBusy, setDemoBusy] = useState(false);
  const [demoMessage, setDemoMessage] = useState("");
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState([]);
  const [asking, setAsking] = useState(false);
  const [navSolid, setNavSolid] = useState(false);
  const chatLog = useRef(null);

  const refresh = useCallback(async () => {
    const [statusResult, missionsResult, edgeResult] = await Promise.allSettled([
      getJSON("/status"),
      getJSON("/missions"),
      EDGE_API ? getEdgeJSON("/api/v1/status") : Promise.resolve(null),
    ]);
    const nextStatus = statusResult.status === "fulfilled" ? statusResult.value : null;
    setStatus(nextStatus);
    setEdgeStatus(edgeResult.status === "fulfilled" && edgeResult.value?.role === "edge" ? edgeResult.value : null);
    if (missionsResult.status === "rejected") {
      setError("The mission service is not reachable right now.");
      return;
    }
    setError("");
    const list = missionsResult.value;
    setMissions(list);
    const chosen = list.some((m) => m.mission_id === missionId) ? missionId : list[0]?.mission_id || "";
    if (chosen !== missionId) {
      setMissionId(chosen);
      return;
    }
    if (!chosen) {
      setData(null);
      return;
    }
    const id = encodeURIComponent(chosen);
    const [latest, tel, path, ai] = await Promise.allSettled([
      getJSON(`/missions/${id}/latest`),
      getJSON(`/missions/${id}/files/telemetry.json`),
      getJSON(`/missions/${id}/flight-path`),
      nextStatus?.role === "cloud" ? getJSON(`/missions/${id}/ai-report`) : Promise.resolve(null),
    ]);
    if (latest.status === "fulfilled") {
      setData((current) =>
        current?.manifest?.mission_id === latest.value.manifest.mission_id &&
        current?.manifest?.version === latest.value.manifest.version ? current : latest.value);
    }
    setTelemetry(tel.status === "fulfilled" ? tel.value : null);
    if (path.status === "fulfilled") setFlightPath(path.value);
    setReport(ai.status === "fulfilled" ? ai.value : null);
  }, [missionId]);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    const onScroll = () => setNavSolid(window.scrollY > 40);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    // scroll the chat box only (scrollIntoView would also scroll the page on load)
    const log = chatLog.current;
    if (log && messages.length) log.scrollTop = log.scrollHeight;
  }, [messages, asking]);

  useReveal();

  const manifest = data?.manifest;
  const summary = manifest?.summary || {};
  const isEdge = status?.role === "edge";
  const isCloud = status?.role === "cloud";
  const edge = isEdge ? status : edgeStatus;
  const uploader = edge?.uploader;
  const local = uploader?.local_version ?? null;
  const cloud = isCloud ? manifest?.version ?? null : uploader?.acked_version ?? null;
  const pending = uploader ? Math.max(0, (uploader.local_version || 0) - (uploader.acked_version || 0)) : 0;
  const linkState = !uploader
    ? (isCloud ? "cloud" : "unknown")
    : uploader.online === false ? "offline" : uploader.pending ? "pending" : "online";
  const linkLabel = {
    online: "Link up · cloud in sync",
    pending: `Link up · ${pending} version${pending === 1 ? "" : "s"} to send`,
    offline: "No link · capturing on board",
    cloud: "Cloud copy",
    unknown: "Waiting for the edge",
  }[linkState];

  async function setLink(mode) {
    if (!edge || demoBusy) {
      setDemoMessage("Open this page from the Jetson (or run the mock demo) to drive the uplink.");
      return;
    }
    setDemoBusy(true);
    setDemoMessage("");
    const request = isEdge ? getJSON : getEdgeJSON;
    const prefix = isEdge ? "" : "/api/v1";
    try {
      if (mode === "fault") {
        await request(`${prefix}/demo/corrupt-next-upload`, { method: "POST" });
        setDemoMessage("The next upload will be cut in half. The cloud rejects it on checksum and the Jetson retries.");
      } else {
        await request(`${prefix}/network`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ offline: mode === "offline" }),
        });
        setDemoMessage(mode === "offline"
          ? "Link cut. Detection and snapshots carry on locally; uploads wait."
          : "Link restored. Only the newest snapshot is sent.");
      }
      await refresh();
    } catch (reason) {
      setDemoMessage(`Could not reach the edge controls: ${reason.message}`);
    } finally {
      setDemoBusy(false);
    }
  }

  async function ask(text) {
    const trimmed = (text ?? question).trim();
    if (!trimmed || !missionId || !isCloud || asking) return;
    setQuestion("");
    setMessages((items) => [...items, { role: "user", text: trimmed }]);
    setAsking(true);
    try {
      const response = await getJSON(`/missions/${encodeURIComponent(missionId)}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: trimmed }),
      });
      setMessages((items) => [...items, { role: "assistant", text: response.answer, by: response.generated_by, tools: response.tool_calls || [] }]);
    } catch (reason) {
      setMessages((items) => [...items, { role: "assistant", text: `No answer: ${reason.message}`, by: "error", tools: [] }]);
    } finally {
      setAsking(false);
    }
  }

  const stepValues = {
    capture: manifest ? `${fmt(summary.frames_processed)} frames` : "waiting",
    detect: manifest ? `${fmt(summary.detections_processed)} detections` : "waiting",
    locate: manifest ? `${fmt(summary.estimated_people)} placed · ${fmt(summary.unlocated_tracks)} not` : "waiting",
    keep: manifest ? `snapshot v${manifest.version}` : "waiting",
    sync: linkLabel,
  };

  return (
    <div className="lp">
      <header className={`lp-nav ${navSolid ? "solid" : ""}`}>
        <a className="lp-mark" href="#/story" aria-label="RescueSight home">
          <span className="lp-mark-glyph" aria-hidden="true" />
          Rescue<em>Sight</em>
        </a>
        <nav className="lp-links" aria-label="Sections">
          <a href="#lp-how">How it works</a>
          <a href="#lp-map">Live map</a>
          <a href="#lp-offline">No signal</a>
          <a href="#lp-numbers">Numbers</a>
          <a href="#lp-ask">Ask</a>
        </nav>
        <div className="lp-nav-right">
          <span className={`lp-live ${error ? "down" : ""}`}>
            <i aria-hidden="true" />
            {error ? "Offline" : isEdge ? "Edge · Jetson" : isCloud ? "Cloud" : "Connecting"}
          </span>
          <button className="lp-btn dark small" onClick={onDashboard}>
            Mission control <ArrowUpRight size={15} />
          </button>
        </div>
      </header>

      {/* ------------------------------------------------------------------ hero */}
      <section className="lp-hero" aria-labelledby="lp-title">
        <div className="lp-hero-frame">
          <FieldScene data={data} flightPath={flightPath} telemetry={telemetry} />
          <div className="lp-hero-copy">
            <p className="lp-kicker">Offline-first search &amp; rescue · Jetson Orin Nano</p>
            <h1 id="lp-title">
              Find people<br />from the air,<br /><em>signal or not.</em>
            </h1>
            <p className="lp-lede">
              A small box turns any camera drone into a search instrument. It spots people in the
              video, places them on a map and keeps every result on board, then sends a few
              kilobytes the moment any link appears.
            </p>
            <div className="lp-cta">
              <button className="lp-btn light" onClick={onDashboard}>Open mission control <ArrowRight size={16} /></button>
              <a className="lp-btn ghost" href="#lp-how">How it works</a>
            </div>
          </div>

          <aside className="lp-card lp-card-mission" aria-label="Latest mission">
            <div className="lp-card-top">
              <span className="lp-chip">{manifest ? `v${manifest.version}` : "no data"}</span>
              <span className="lp-mono">{manifest?.mission_id || "waiting for a snapshot"}</span>
            </div>
            <div className="lp-big">
              <span>{manifest ? fmt(summary.estimated_people) : "—"}</span>
              <small>estimated<br />people</small>
            </div>
            <dl className="lp-facts">
              <div><dt>Median uncertainty</dt><dd>{summary.median_error_m != null ? `± ${fmt(summary.median_error_m, 1)} m` : "—"}</dd></div>
              <div><dt>Without location</dt><dd>{fmt(summary.unlocated_tracks)}</dd></div>
              <div><dt>Last observation</dt><dd>{formatTime(manifest?.data_window?.end)}</dd></div>
            </dl>
            <p className="lp-fine">Estimates for human review, never confirmed people.</p>
          </aside>

          <aside className={`lp-card lp-card-link ${linkState}`} aria-label="Uplink">
            <div className="lp-link-row">
              <span className="lp-pulse" aria-hidden="true" />
              <strong>{linkLabel}</strong>
            </div>
            <div className="lp-versions">
              {linkState === "cloud" ? (
                <div><span>Received</span><b className="lp-time">{formatTime(data?.received_at)}</b></div>
              ) : (
                <div><span>On board</span><b>{local != null ? `v${local}` : "—"}</b></div>
              )}
              <div className="lp-versions-arrow" aria-hidden="true" />
              <div><span>Cloud</span><b>{cloud != null ? `v${cloud}` : "—"}</b></div>
            </div>
          </aside>

          <ul className="lp-hero-stats" aria-label="Measured on the Jetson">
            <li><b>0</b><span>ground stations needed</span></li>
            <li><b>8.8<small>fps</small></b><span>tiled detection, 1080p, measured</span></li>
            <li><b>KB</b><span>per sync, never video</span></li>
          </ul>
          <p className="lp-scene-note">Live estimates on a schematic terrain · tiles show the detector's scan pattern</p>
        </div>
      </section>

      {/* ------------------------------------------------------------------ how */}
      <section className="lp-section" id="lp-how" aria-labelledby="lp-how-title">
        <div className="lp-head" data-reveal>
          <p className="lp-index">01 — The add-on</p>
          <h2 id="lp-how-title">One small box.<br /><em>Any drone.</em></h2>
          <p className="lp-head-copy">
            Existing drones film well but think nothing of it, and drop everything when the link
            goes. RescueSight rides along on a Jetson Orin Nano and does the search on board.
          </p>
        </div>
        <ol className="lp-steps">
          {STEPS.map((step, i) => (
            <li key={step.id} data-reveal style={{ "--d": `${i * 70}ms` }}>
              <span className="lp-step-n">{String(i + 1).padStart(2, "0")}</span>
              <h3>{step.title}</h3>
              <p>{step.text}</p>
              <span className="lp-step-live"><i aria-hidden="true" />{stepValues[step.id]}</span>
            </li>
          ))}
        </ol>
      </section>

      {/* ------------------------------------------------------------------ live map */}
      <section className="lp-section lp-dark" id="lp-map" aria-labelledby="lp-map-title">
        <div className="lp-map-head" data-reveal>
          <div>
            <p className="lp-index">02 — Live map</p>
            <h2 id="lp-map-title">Every estimate,<br /><em>with its doubt drawn on.</em></h2>
          </div>
          <div className="lp-map-meta">
            {missions.length > 1 && (
              <label className="lp-select">
                <span>Mission</span>
                <select value={missionId} onChange={(e) => setMissionId(e.target.value)}>
                  {missions.map((m) => <option key={m.mission_id} value={m.mission_id}>{m.mission_id}</option>)}
                </select>
              </label>
            )}
            <ul className="lp-legend">
              <li><i className="dot" />Estimated person</li>
              <li><i className="ring" />Position uncertainty</li>
              <li><i className="cell" />Density cell</li>
              <li><i className="path" />Drone track</li>
            </ul>
          </div>
        </div>
        <div className="lp-map-frame" data-reveal>
          {data ? (
            <MissionMap data={data} showCells showPeople showOsm={false} windowSeconds={0}
              onSelect={() => {}} flightPath={flightPath} />
          ) : (
            <div className="lp-map-empty">No snapshot yet. Process a clip on the Jetson and it appears here.</div>
          )}
          <button className="lp-btn light lp-map-cta" onClick={onDashboard}>Inspect in mission control <ArrowUpRight size={15} /></button>
        </div>
      </section>

      {/* ------------------------------------------------------------------ offline */}
      <section className="lp-section" id="lp-offline" aria-labelledby="lp-off-title">
        <div className="lp-split">
          <div data-reveal>
            <p className="lp-index">03 — No signal</p>
            <h2 id="lp-off-title">Signal drops.<br /><em>The search doesn&apos;t.</em></h2>
            <p className="lp-body">
              Detection, mapping and storage never wait for the network. When a link returns,
              through a 4G stick, a hotspot or Wi-Fi at landing, the Jetson sends only its newest
              snapshot, checks it arrived intact, and moves on.
            </p>
            <div className="lp-seg" role="group" aria-label="Uplink demo">
              <button aria-pressed={linkState === "online" || linkState === "pending"} disabled={demoBusy} onClick={() => setLink("online")}>Link up</button>
              <button aria-pressed={linkState === "offline"} disabled={demoBusy} onClick={() => setLink("offline")}>Cut the link</button>
              <button disabled={demoBusy} onClick={() => setLink("fault")}>Corrupt next upload</button>
            </div>
            <p className="lp-demo-note" role="status">
              {demoMessage || (edge ? "These controls act on the real uploader." : "Controls act on the Jetson's uploader when this page is served by it.")}
            </p>
          </div>
          <div className={`lp-relay ${linkState}`} data-reveal aria-label="Store and forward diagram">
            <div className="lp-node">
              <span className="lp-node-k">Aircraft · Jetson</span>
              <b>{local != null ? `v${local}` : manifest ? `v${manifest.version}` : "—"}</b>
              <span className="lp-node-s">snapshots kept on board</span>
            </div>
            <div className="lp-wire" aria-hidden="true"><span /><em>×</em></div>
            <div className="lp-node">
              <span className="lp-node-k">Cloud · Tiger Data</span>
              <b>{cloud != null ? `v${cloud}` : "—"}</b>
              <span className="lp-node-s">{report?.latest_snapshot_version ? "indexed and reported" : "rows, files, flight track"}</span>
            </div>
            <div className="lp-queue">
              {Array.from({ length: Math.min(pending, 12) }, (_, i) => <i key={i} />)}
              <span>{pending ? `${pending} waiting · only the newest will be sent` : "nothing waiting"}</span>
            </div>
          </div>
        </div>
      </section>

      {/* ------------------------------------------------------------------ numbers */}
      <section className="lp-section lp-numbers" id="lp-numbers" aria-labelledby="lp-num-title">
        <div className="lp-head" data-reveal>
          <p className="lp-index">04 — Measured</p>
          <h2 id="lp-num-title">Numbers we measured,<br /><em>not promised.</em></h2>
          <p className="lp-head-copy">From this weekend's runs. Field accuracy over real terrain is still future work.</p>
        </div>
        <div className="lp-stats">
          {MEASURED.map((m, i) => (
            <figure key={m.label} data-reveal style={{ "--d": `${i * 80}ms` }}>
              <div className="lp-stat-value">
                {m.to ? <><span className="from">{m.value}</span><ArrowRight size={28} /><span>{m.to}</span></> : m.value}
                {m.unit && <small>{m.unit}</small>}
              </div>
              <figcaption>
                <strong>{m.label}</strong>
                <span>{m.note}</span>
              </figcaption>
            </figure>
          ))}
        </div>
      </section>

      {/* ------------------------------------------------------------------ ask */}
      <section className="lp-section" id="lp-ask" aria-labelledby="lp-ask-title">
        <div className="lp-split ask">
          <div data-reveal>
            <p className="lp-index">05 — Ask the mission</p>
            <h2 id="lp-ask-title">Questions in,<br /><em>sourced answers out.</em></h2>
            <p className="lp-body">
              A Gemini assistant reads only this mission's data through a few read-only tools,
              names nearby landmarks from OpenStreetMap, and states its uncertainty. It never
              assigns rescues. Without a key it falls back to the plain mission report.
            </p>
            <div className="lp-report">
              <span className="lp-index">Mission report</span>
              <p>{isCloud ? describeReport(report) : "Reports are written in the cloud copy of the mission."}</p>
              {report?.report && (
                <details>
                  <summary>Read the report</summary>
                  <pre>{report.report}</pre>
                </details>
              )}
            </div>
          </div>
          <div className="lp-chat" data-reveal>
            <div className="lp-chat-log" aria-live="polite" ref={chatLog}>
              {!messages.length && (
                <div className="lp-chat-empty">
                  <p>{isCloud ? "Try one of these:" : "The assistant answers from the cloud copy of the mission."}</p>
                  {isCloud && QUESTIONS.map((q) => (
                    <button key={q} onClick={() => ask(q)} disabled={!missionId || asking}>{q}</button>
                  ))}
                </div>
              )}
              {messages.map((m, i) => (
                <div key={i} className={`lp-msg ${m.role}`}>
                  <p>{m.text}</p>
                  {m.role === "assistant" && (
                    <span className="lp-msg-meta">
                      {m.by === "gemini" ? "Gemini · mission tools" : m.by === "deterministic" ? "Plain report (no Gemini)" : m.by}
                      {m.tools?.length ? ` · ${m.tools.map((t) => t.tool).join(", ")}` : ""}
                    </span>
                  )}
                </div>
              ))}
              {asking && <div className="lp-msg assistant thinking"><p>Reading the mission…</p></div>}
            </div>
            <form className="lp-chat-input" onSubmit={(e) => { e.preventDefault(); ask(); }}>
              <input value={question} onChange={(e) => setQuestion(e.target.value)} maxLength={1000}
                placeholder={isCloud ? "Ask about this mission…" : "Available on the cloud copy"}
                disabled={!isCloud || !missionId} aria-label="Question about the mission" />
              <button type="submit" disabled={!isCloud || !question.trim() || asking} aria-label="Send">
                <Send size={17} />
              </button>
            </form>
            <p className="lp-fine">Human review required · estimates only</p>
          </div>
        </div>
      </section>

      {/* ------------------------------------------------------------------ footer */}
      <footer className="lp-footer">
        <div className="lp-footer-row">
          <p>
            Decision support for search and rescue teams. Positions are estimates with a stated
            error; counts are estimated sightings, not verified people; nothing here ranks who is
            rescued first.
          </p>
          <button className="lp-btn light" onClick={onDashboard}>Open mission control <ArrowUpRight size={16} /></button>
        </div>
        <div className="lp-footer-mark" aria-hidden="true">Rescue<em>Sight</em></div>
        <div className="lp-footer-meta">
          <span>HackUMass 2026</span>
          <span>Jetson Orin Nano · FastAPI · Tiger Data · Gemini</span>
        </div>
      </footer>
    </div>
  );
}
