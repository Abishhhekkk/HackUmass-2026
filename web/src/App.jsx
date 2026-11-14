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
  TileLayer,
  Tooltip,
  useMap,
} from "react-leaflet";
import L from "leaflet";
import { Command } from "cmdk";

const API = "/api/v1";
const EDGE_API = import.meta.env.DEV ? "/edge-api" : null;
const POLL_MS = 3000;
const WINDOWS = [
  { value: 0, label: "All activity" },
  { value: 60, label: "Last minute" },
  { value: 300, label: "Last 5 minutes" },
  { value: 900, label: "Last 15 minutes" },
];
const HEAT_COLORS = ["#9caf91", "#b4b17b", "#c99b65", "#b9785d", "#a9574e"];

const personIcon = L.divIcon({
  className: "sighting-icon",
  html: '<span class="sighting-core"></span>',
  iconSize: [20, 20],
  iconAnchor: [10, 10],
});

async function getJSON(path, options) {
  const response = await fetch(`${API}${path}`, options);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `${response.status} ${path}`);
  }
  return response.json();
}

async function getEdgeJSON(path, options) {
  const response = await fetch(`${EDGE_API}${path}`, options);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `${response.status} ${path}`);
  }
  return response.json();
}

function FitMission({ features }) {
  const map = useMap();
  useEffect(() => {
    const points = [];
    for (const feature of features) {
      if (feature.geometry?.type === "Point") {
        const [lon, lat] = feature.geometry.coordinates;
        points.push([lat, lon]);
      }
    }
    if (points.length) map.fitBounds(points, { padding: [50, 50], maxZoom: 18 });
  }, [features, map]);
  return null;
}

function ZoomControls() {
  const map = useMap();
  return (
    <div className="map-zoom">
      <button aria-label="Zoom in" onClick={() => map.zoomIn()}>+</button>
      <button aria-label="Zoom out" onClick={() => map.zoomOut()}>−</button>
    </div>
  );
}

function HeatCell({ feature, people, onSelect }) {
  const properties = feature.properties;
  const color = HEAT_COLORS[Math.min(4, Math.max(0, properties.count - 1))];
  const positions = feature.geometry.coordinates[0].map(([lon, lat]) => [lat, lon]);
  const included = (properties.person_ids || []).map((id) => people[id]).filter(Boolean);

  return (
    <Polygon
      positions={positions}
      pathOptions={{ color, fillColor: color, fillOpacity: 0.22, weight: 1.5 }}
      eventHandlers={{ click: () => onSelect({ type: "cell", properties, people: included }) }}
    >
      <Tooltip sticky>
        <strong>{properties.count} estimated sighting{properties.count === 1 ? "" : "s"}</strong>
        <br />
        Cell {properties.cell_id}
      </Tooltip>
    </Polygon>
  );
}

function MissionMap({ data, showCells, showPeople, showOsm, windowSeconds, onSelect }) {
  const features = data?.heatmap?.features || [];
  const people = useMemo(() => {
    const result = {};
    for (const feature of features) {
      if (feature.properties?.kind === "person") result[feature.properties.person_id] = feature;
    }
    return result;
  }, [features]);
  const end = Date.parse(data?.manifest?.data_window?.end || "") || Date.now();

  return (
    <MapContainer
      className="mission-map"
      center={[42.3893, -72.528]}
      zoom={16}
      scrollWheelZoom
      zoomControl={false}
      attributionControl={false}
      preferCanvas
    >
      {showOsm && (
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
          url="https://tile.openstreetmap.org/{z}/{x}/{y}.png"
          maxZoom={19}
        />
      )}
      <FitMission features={features} />
      <ZoomControls />
      {showCells &&
        features
          .filter((feature) => feature.properties?.kind === "cell")
        .map((feature) => (
            <HeatCell
              key={feature.properties.cell_id}
              feature={feature}
              people={people}
              onSelect={onSelect}
            />
          ))}
      {showPeople &&
        Object.values(people).map((feature) => {
          const properties = feature.properties;
          const [lon, lat] = feature.geometry.coordinates;
          const old =
            windowSeconds > 0 &&
            Date.parse(properties.last_seen_utc) < end - windowSeconds * 1000;
          return (
            <Fragment key={properties.person_id}>
              <Circle
                center={[lat, lon]}
                radius={properties.horizontal_error_m || 0}
                pathOptions={{
                  color: "#f0eee8",
                  dashArray: "5 5",
                  fillOpacity: 0.02,
                  opacity: old ? 0.2 : 0.6,
                  weight: 1,
                }}
                eventHandlers={{
                  click: () => onSelect({ type: "person", properties, lat, lon }),
                }}
              />
              <Marker
                position={[lat, lon]}
                icon={personIcon}
                opacity={old ? 0.3 : 1}
                eventHandlers={{
                  click: () => onSelect({ type: "person", properties, lat, lon }),
                }}
              >
                <Tooltip>
                  {properties.person_id} · confidence {properties.confidence}
                </Tooltip>
              </Marker>
            </Fragment>
          );
        })}
    </MapContainer>
  );
}

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
  const cloudTone = !uploader
    ? "not-configured"
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
        : data?.source === "cloud"
          ? "Not in cloud snapshot"
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
        : data?.source === "cloud"
          ? "Cloud copy"
          : "Not configured",
      note: uploader?.online === false ? "Offline · saved locally" : "Retry-safe upload",
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

function formatTime(value) {
  if (!value) return "Unknown";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Unknown"
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function CanvasTextAction({ onClick, label }) {
  const canvasRef = useRef(null);
  const [canvasReady, setCanvasReady] = useState(false);

  useEffect(() => {
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d");
    if (!canvas || !context) return undefined;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let frame = 0;
    let animationId;
    let disposed = false;
    let hasDrawn = false;

    function draw(time = 0) {
      if (disposed) return;
      const bounds = canvas.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.round(bounds.width * ratio));
      canvas.height = Math.max(1, Math.round(bounds.height * ratio));
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, bounds.width, bounds.height);
      const fontSize = Math.min(13, Math.max(10, bounds.height * 0.32));
      context.font = `650 ${fontSize}px "Cascadia Code", Consolas, monospace`;
      context.textBaseline = "middle";
      context.fillStyle = "#315d49";
      context.fillText(label, 1, bounds.height / 2);
      context.save();
      context.globalCompositeOperation = "source-atop";
      const colors = ["#6e9972", "#b4874b", "#9baa86"];
      colors.forEach((color, index) => {
        context.beginPath();
        context.strokeStyle = color;
        context.lineWidth = 1.2;
        const phase = reducedMotion ? 0 : time * 0.001 + index * 1.8;
        for (let x = 0; x <= bounds.width; x += 3) {
          const y = bounds.height / 2
            + Math.sin(x * 0.045 + phase) * (3 + index)
            + Math.cos(x * 0.018 - phase) * 2;
          if (x === 0) context.moveTo(x, y);
          else context.lineTo(x, y);
        }
        context.stroke();
      });
      context.restore();
      if (!hasDrawn) {
        hasDrawn = true;
        setCanvasReady(true);
      }
      if (!reducedMotion) animationId = window.requestAnimationFrame(draw);
    }

    const observer = new ResizeObserver(() => draw());
    observer.observe(canvas);
    draw();
    return () => {
      disposed = true;
      observer.disconnect();
      window.cancelAnimationFrame(animationId);
    };
  }, [label]);

  return (
    <button className={`canvas-text-action ${canvasReady ? "canvas-ready" : ""}`} onClick={onClick} aria-label={label}>
      <span>{label}</span>
      <canvas ref={canvasRef} aria-hidden="true" />
      <ArrowUpRight size={15} aria-hidden="true" />
    </button>
  );
}

function MatrixTrail() {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d");
    if (!canvas || !context) return undefined;

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const cellWidth = 19;
    const cellHeight = 18;
    const glyphs = "01";
    const fontFamily = window.getComputedStyle(document.documentElement).getPropertyValue("--mono") || "monospace";
    const pointerTrail = [];
    let columns = [];
    let width = 0;
    let height = 0;
    let ratio = 1;
    let animationId = 0;
    let lastFrame = 0;

    function resize() {
      width = window.innerWidth;
      height = window.innerHeight;
      ratio = Math.min(window.devicePixelRatio || 1, 1.5);
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      columns = Array.from({ length: Math.ceil(width / cellWidth) }, () => ({
        head: Math.random() * (height / cellHeight),
        speed: 0.18 + Math.random() * 0.28,
        length: 3 + Math.floor(Math.random() * 5),
      }));
      render();
    }

    function render(time = 0) {
      if (!reducedMotion && time - lastFrame < 48) {
        animationId = window.requestAnimationFrame(render);
        return;
      }
      lastFrame = time;
      context.clearRect(0, 0, width, height);
      context.font = `10px ${fontFamily}`;
      context.textAlign = "center";
      context.textBaseline = "middle";

      columns.forEach((column, index) => {
        if (!reducedMotion) {
          column.head += column.speed;
          if (column.head - column.length > height / cellHeight) column.head = -Math.random() * 18;
        }
        const x = index * cellWidth + cellWidth / 2;
        const head = Math.floor(column.head);
        for (let offset = 0; offset < column.length; offset += 1) {
          const y = (head - offset) * cellHeight;
          if (y < 0 || y > height) continue;
          const opacity = 0.018 + (1 - offset / column.length) * 0.055;
          context.fillStyle = `rgba(49, 93, 73, ${opacity})`;
          context.fillText(glyphs[(index + head - offset) % glyphs.length], x, y);
        }
      });

      if (!reducedMotion) {
        for (let index = pointerTrail.length - 1; index >= 0; index -= 1) {
          const point = pointerTrail[index];
          const age = time - point.time;
          if (age > 720) {
            pointerTrail.splice(index, 1);
            continue;
          }
          const opacity = (1 - age / 720) * 0.2;
          for (let row = -2; row <= 2; row += 1) {
            for (let column = -3; column <= 3; column += 1) {
              const distance = Math.hypot(column * cellWidth, row * cellHeight);
              if (distance > 68) continue;
              context.fillStyle = `rgba(49, 93, 73, ${opacity * (1 - distance / 90)})`;
              context.fillText(
                glyphs[(Math.abs(row * 3 + column) + index) % glyphs.length],
                point.x + column * cellWidth,
                point.y + row * cellHeight,
              );
            }
          }
        }
        animationId = window.requestAnimationFrame(render);
      }
    }

    function onPointerMove(event) {
      if (reducedMotion) return;
      pointerTrail.push({ x: event.clientX, y: event.clientY, time: performance.now() });
      if (pointerTrail.length > 10) pointerTrail.shift();
    }

    function onVisibilityChange() {
      window.cancelAnimationFrame(animationId);
      if (!document.hidden && !reducedMotion) animationId = window.requestAnimationFrame(render);
    }

    resize();
    if (!reducedMotion) animationId = window.requestAnimationFrame(render);
    window.addEventListener("resize", resize);
    window.addEventListener("pointermove", onPointerMove, { passive: true });
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.cancelAnimationFrame(animationId);
      window.removeEventListener("resize", resize);
      window.removeEventListener("pointermove", onPointerMove);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, []);

  return <canvas className="story-matrix-trail" ref={canvasRef} aria-hidden="true" />;
}

function PointerHighlight({ children }) {
  function updateHighlight(event) {
    const bounds = event.currentTarget.getBoundingClientRect();
    const progress = Math.max(0, Math.min(100, ((event.clientX - bounds.left) / bounds.width) * 100));
    event.currentTarget.style.setProperty("--pointer-progress", `${progress}%`);
  }

  return <span className="pointer-highlight" onPointerMove={updateHighlight}>{children}</span>;
}

function CompareSlider() {
  const [position, setPosition] = useState(48);

  return (
    <div className="compare-slider">
      <div className="compare-scene">
        <div className="compare-scene-art" aria-hidden="true">
          <div className="compare-side compare-network-side">
            <div className="compare-side-heading"><span>PROBLEM: REVIEW WAITS</span><i>GROUND PIPELINE</i></div>
            <div className="compare-network-content">
              <div className="compare-feed">
                <span>LIVE CAMERA FEED</span>
                <div className="compare-feed-frame"><i /><i /><i /><b>RAW VIDEO</b></div>
                <small>No link. No ground review.</small>
              </div>
              <div className="compare-link-break"><span /><WifiOff size={20} /><span /></div>
              <div className="compare-ground">
                <div className="compare-ground-map">
                  <span /><span /><span />
                  <i className="compare-ground-pin"><MapPin size={13} /></i>
                </div>
                <b>GROUND REVIEW</b>
                <small>Analysis follows when data arrives.</small>
              </div>
            </div>
            <div className="compare-side-foot"><span>IF THE LINK IS UNAVAILABLE</span><b>UPLOAD &amp; REVIEW MAY WAIT</b></div>
          </div>

          <div className="compare-side compare-edge-side" style={{ clipPath: `inset(0 0 0 ${position}%)` }}>
            <div className="compare-side-heading"><span>SOLUTION: MAP BUILT LOCALLY</span><i>ONBOARD</i></div>
            <div className="compare-edge-content">
              <div className="compare-edge-map">
                <svg viewBox="0 0 460 260" role="presentation">
                  <path d="M56 202C70 148 103 151 121 114s42-51 74-31 45 4 58-28 43-34 61-5 41 35 89 18" />
                  <path d="M58 212c26-38 44-15 72-49s41-48 69-36 49-7 63-35 45-31 64-7 39 28 72 8" />
                  <path d="M71 225c26-24 48-8 71-35s42-35 61-29 54-3 69-28 38-25 55-7 39 16 66-1" />
                  <path className="compare-flight-path" d="M38 56 160 88 223 122 345 160 419 201" />
                  <circle cx="160" cy="88" r="8" /><circle cx="223" cy="122" r="8" /><circle cx="345" cy="160" r="8" />
                  <circle className="compare-drone-point" cx="38" cy="56" r="5" />
                </svg>
                <span className="compare-map-tag">LOCAL HEAT GRID</span>
                <span className="compare-map-coordinates">GPS + FRAME TIME ALIGNED</span>
              </div>
              <div className="compare-edge-package">
                <span className="compare-package-mark"><PackageCheck size={18} /></span>
                <span><b>MISSION PACKAGE</b><small>HEATMAP · COORDINATES · CHECKSUM</small></span>
                <strong>LOCAL</strong>
              </div>
            </div>
            <div className="compare-side-foot"><span>WITH OR WITHOUT UPLINK</span><b>BUILD LOCALLY · SYNC LATER</b></div>
          </div>

          <div className="compare-split" style={{ left: `${position}%` }}><span><i /><i /></span></div>
        </div>
        <input
          className="compare-range-input"
          type="range"
          min="0"
          max="100"
          value={position}
          aria-label="Compare network-dependent and RescueSight workflows"
          aria-valuetext={`${position}% RescueSight onboard workflow visible`}
          onChange={(event) => setPosition(Number(event.target.value))}
        />
      </div>
      <div className="compare-range-label" aria-hidden="true">
        <span>THE BOTTLENECK</span>
        <span>THE ONBOARD FIX</span>
      </div>
      <div className="compare-summary">
        <div><span>THE PROBLEM</span><p>Without a connection, ground review waits for video to arrive.</p></div>
        <div><span>THE RESCUESIGHT SOLUTION</span><p>The drone finds possible people and estimates their locations onboard. It syncs a compact heatmap and coordinate file later.</p></div>
      </div>
      <p className="compare-caveat">Illustrates workflow differences; other systems vary. Coordinates and heatmaps are estimates for human review.</p>
    </div>
  );
}

function ProductScrollReveal({ missionData, missionId, missions, onMissionChange, status, telemetry, summary }) {
  const sectionRef = useRef(null);
  const aerialRef = useRef(null);
  const productRef = useRef(null);
  const reduceMotion = useReducedMotion();
  const { scrollYProgress } = useScroll({
    target: sectionRef,
    offset: ["start start", "end end"],
  });
  const aerialScale = useTransform(scrollYProgress, [0, 0.58], reduceMotion ? [1, 1] : [1, 1.26]);
  const productScale = useTransform(scrollYProgress, [0.16, 0.78], reduceMotion ? [1, 1] : [0.82, 1]);
  const productY = useTransform(scrollYProgress, [0.16, 0.78], reduceMotion ? [0, 0] : [115, 0]);
  const dronePosition = telemetry?.available ? telemetry.position : null;

  useMotionValueEvent(scrollYProgress, "change", (progress) => {
    if (reduceMotion) return;
    const aerialProgress = Math.max(0, Math.min(1, progress / 0.52));
    const productProgress = Math.max(0, Math.min(1, (progress - 0.2) / 0.48));
    if (aerialRef.current) aerialRef.current.style.opacity = String(1 - aerialProgress);
    if (productRef.current) productRef.current.style.opacity = String(productProgress);
  });

  return (
    <section className={`story-scroll-product ${reduceMotion ? "reduce-motion" : ""}`} ref={sectionRef} aria-label="Onboard processing reveal">
      <div className="story-scroll-sticky">
        <div className="story-reveal-copy">
          <div className="story-kicker"><span>RESCUESIGHT / ONBOARD MIDDLEWARE</span></div>
          <h1>No link.<br /><em>No pause.</em></h1>
          <p>Jetson Orin Nano processes the camera feed on the aircraft. ESP32 telemetry supplies the timestamped position. Together, they produce a compact heatmap and coordinate package—without waiting for a ground station.</p>
          <CanvasTextAction onClick={() => document.getElementById("story-pipeline")?.scrollIntoView({ behavior: "smooth" })} label="FOLLOW THE DATA" />
          <div className="story-hardware-specs">
            <span><b>01</b><strong>JETSON ORIN NANO</strong><small>ONBOARD INFERENCE &amp; AGGREGATION</small></span>
            <span><b>02</b><strong>ESP32 TELEMETRY</strong><small>GPS / POSE / FRAME-TIME ALIGNMENT</small></span>
          </div>
        </div>

        <div className="story-reveal-stage">
          <motion.img
            ref={aerialRef}
            className="story-aerial-image"
            src="/aerialshot.svg"
            alt="Illustrative aerial schematic of a search sector and drone path"
            style={{ scale: aerialScale }}
          />
          <motion.div
            ref={productRef}
            className="story-product-assembly"
            style={{ scale: productScale, y: productY }}
            aria-label="Live mission middleware output preview"
          >
            <div className="story-product-topbar">
              <span><i /> FLIGHT MIDDLEWARE</span>
              <label>
                <span>MISSION</span>
                <select value={missionId} onChange={(event) => onMissionChange(event.target.value)} aria-label="Select story mission">
                  {!missions.length && <option value="">Waiting for mission data</option>}
                  {missions.map((mission) => <option key={mission.mission_id} value={mission.mission_id}>{mission.mission_id}</option>)}
                </select>
                <ChevronDown size={12} />
              </label>
            </div>
            <div className="story-product-window">
              <div className="story-product-sidebar">
                <span>ONBOARD STATUS</span>
                <b className={status?.running ? "running" : ""}><i />{status?.running ? "PROCESSING" : "READY"}</b>
                <small>EDGE COMPUTE</small>
                <strong>Jetson Orin Nano</strong>
                <small>POSE SOURCE</small>
                <strong>{dronePosition ? "ESP32 / GPS FIX" : "Awaiting telemetry"}</strong>
                <div className="story-product-artifact"><Database size={14} /><span><b>mission-package</b><small>heatmap + coordinates</small></span></div>
              </div>
              <div className="story-product-map">
                {missionData?.heatmap ? (
                  <MissionMap data={missionData} showCells showPeople showOsm={false} windowSeconds={0} onSelect={() => {}} />
                ) : (
                  <div className="story-map-empty"><Crosshair size={24} /><b>Waiting for onboard output</b><span>Local heatmap and coordinates appear here after frames are processed.</span></div>
                )}
                <div className="story-product-map-label"><Crosshair size={11} /> LOCAL HEATMAP</div>
              </div>
            </div>
            <div className="story-product-output">
              <div><small>TRACKS MAPPED</small><b>{summary.estimated_people ?? "—"}</b></div>
              <div><small>UNLOCATED</small><b>{summary.unlocated_tracks ?? "—"}</b></div>
              <div><small>FRAME COUNT</small><b>{summary.frames_processed ?? "—"}</b></div>
              <div className="story-product-export"><PackageCheck size={14} /><span><b>COMPACT PACKAGE</b><small>{missionData?.manifest ? `v${missionData.manifest.version} · ready to sync` : "built on aircraft"}</small></span></div>
            </div>
          </motion.div>
          <div className="story-reveal-caption"><span>AERIAL INPUT</span><i /><span>ONBOARD HEATMAP + COORDINATES</span></div>
        </div>
      </div>
    </section>
  );
}

function RescueStoryPage({ onDashboard }) {
  const [status, setStatus] = useState(null);
  const [edgeStatus, setEdgeStatus] = useState(null);
  const [aiStatus, setAiStatus] = useState(null);
  const [automatedReport, setAutomatedReport] = useState(null);
  const [missions, setMissions] = useState([]);
  const [missionId, setMissionId] = useState("");
  const [missionData, setMissionData] = useState(null);
  const [telemetry, setTelemetry] = useState(null);
  const [error, setError] = useState("");
  const [selectedNode, setSelectedNode] = useState(null);
  const [demoMode, setDemoMode] = useState("normal");
  const [demoBusy, setDemoBusy] = useState(false);
  const [demoMessage, setDemoMessage] = useState("");
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState([]);
  const [asking, setAsking] = useState(false);
  const [promptCopyStatus, setPromptCopyStatus] = useState("");
  const [promptText, setPromptText] = useState("");

  const refreshStory = useCallback(async () => {
    const [statusResult, missionsResult, aiResult, edgeResult] = await Promise.allSettled([
      getJSON("/status"),
      getJSON("/missions"),
      getJSON("/ai/status"),
      EDGE_API ? getEdgeJSON("/api/v1/status") : Promise.resolve(null),
    ]);
    if (statusResult.status === "fulfilled") setStatus(statusResult.value);
    if (aiResult.status === "fulfilled") setAiStatus(aiResult.value);
    setEdgeStatus(edgeResult.status === "fulfilled" && edgeResult.value?.role === "edge"
      ? edgeResult.value
      : null);
    const nextStatus = statusResult.status === "fulfilled" ? statusResult.value : null;
    const availableMissions = missionsResult.status === "fulfilled" ? missionsResult.value : [];
    setMissions(availableMissions);
    if (missionsResult.status === "rejected") {
      setError(missionsResult.reason.message || "Mission service is unavailable.");
      return;
    }
    setError("");
    const selected = availableMissions.some((item) => item.mission_id === missionId)
      ? missionId
      : availableMissions[0]?.mission_id || "";
    if (selected !== missionId) {
      setMissionId(selected);
      return;
    }
    if (!selected) {
      setMissionData(null);
      setTelemetry(null);
      setAutomatedReport(null);
      return;
    }
    const encodedId = encodeURIComponent(selected);
    const [latestResult, telemetryResult, reportResult] = await Promise.allSettled([
      getJSON(`/missions/${encodedId}/latest`),
      getJSON(`/missions/${encodedId}/files/telemetry.json`),
      nextStatus?.role === "cloud"
        ? getJSON(`/missions/${encodedId}/ai-report`)
        : Promise.resolve(null),
    ]);
    if (latestResult.status === "fulfilled") {
      setMissionData((current) =>
        current?.manifest?.mission_id === latestResult.value.manifest.mission_id &&
        current?.manifest?.version === latestResult.value.manifest.version
          ? current
          : latestResult.value,
      );
    }
    else setError(latestResult.reason.message || "Mission snapshot is unavailable.");
    setTelemetry(telemetryResult.status === "fulfilled" ? telemetryResult.value : null);
    setAutomatedReport(reportResult.status === "fulfilled" ? reportResult.value : null);
  }, [missionId]);

  useEffect(() => {
    refreshStory();
    const interval = window.setInterval(refreshStory, 5000);
    return () => window.clearInterval(interval);
  }, [refreshStory]);

  const manifest = missionData?.manifest;
  const summary = manifest?.summary || {};
  const uploader = status?.uploader;
  const isEdge = status?.role === "edge";
  const connectedEdgeStatus = isEdge ? status : edgeStatus;
  const connectedEdge = connectedEdgeStatus?.role === "edge";
  const connectedUploader = connectedEdgeStatus?.uploader;
  const dronePosition = telemetry?.available ? telemetry.position : null;
  const detectorName = status?.detector?.name || "YOLOv8n with TensorRT";
  const pipelineNodes = [
    { id: "capture", label: "Drone video", value: `${summary.frames_processed ?? 0} frames`, icon: Camera, eyebrow: "01 / INPUT" },
    { id: "detection", label: "Onboard inference", value: `${summary.detections_processed ?? 0} detections`, icon: Cpu, eyebrow: "02 / JETSON ORIN NANO" },
    { id: "geolocation", label: "ESP32 GPS + pose", value: dronePosition ? "Frame-aligned fix" : "Telemetry status included", icon: Satellite, eyebrow: "03 / SENSOR DATA" },
    { id: "snapshot", label: "Local mission package", value: manifest ? `Version ${manifest.version}` : "Waiting for output", icon: PackageCheck, eyebrow: "04 / HEATMAP + COORDINATES" },
    { id: "sync", label: "Sync when connected", value: uploader?.pending ? "Waiting for link" : "No live link required", icon: Wifi, eyebrow: "05 / STORE & FORWARD" },
  ];
  const nodeDescriptions = {
    capture: { text: "The camera feed enters the onboard pipeline directly. Frame timestamps are the common clock used to align observations with aircraft telemetry.", facts: ["Source: camera or replay input", "Result: time-stamped frames", "Ground station: not in the processing path"] },
    detection: { text: `The Jetson Orin Nano runs person detection and per-pass tracking locally. The flight configuration targets ${detectorName}; replay demos may use the mock detector.`, facts: ["Compute: Jetson Orin Nano", "Flight target: YOLOv8n TensorRT", "Output: detection boxes, confidence, temporary track IDs"] },
    geolocation: { text: "The ESP32 supplies GPS and aircraft pose. TelemetryBuffer aligns those samples to each processed frame before camera geometry projects a detection onto the ground. Invalid or stale fixes do not become guessed coordinates.", facts: ["Input: GPS fix, altitude, heading, pitch, roll", "Alignment: frame timestamp", "Data gaps: remain explicitly unlocated"] },
    snapshot: { text: "Tracks are aggregated on the aircraft into estimated sightings, a heat grid, and a compact coordinate file. Each version is checksummed and stored locally; raw video is not part of the transfer package.", facts: ["Onboard outputs: heatmap, sightings CSV, report, telemetry", "Transfer: versioned snapshot, retry-safe", "Estimates include horizontal error"] },
    sync: { text: "The drone continues generating and storing mission packages with no uplink. When connectivity returns, the uploader sends the newest package and the cloud can index it for optional map and assistant workflows.", facts: ["Offline capture and package generation continue", "Newest complete snapshot is sent after reconnect", "Cloud is downstream, not required for onboard operation"] },
  };

  async function setDemoState(mode) {
    if (demoBusy) return;
    if (!connectedEdge) {
      setDemoMode(mode);
      setDemoMessage(
        `${mode === "normal" ? "Normal operation" : mode === "outage" ? "Simulated outage" : "Upload fault"} preview only. This browser has no edge API connection, so the drone uploader is unchanged.`,
      );
      return;
    }
    if (mode === "anomaly" && !connectedEdgeStatus.demo_controls_available) return;
    if ((mode === "normal" || mode === "outage") && !connectedEdgeStatus.network_demo_available) return;
    setDemoBusy(true);
    setDemoMessage("");
    try {
      if (mode === "normal" || mode === "outage") {
        const request = isEdge ? getJSON : getEdgeJSON;
        const prefix = isEdge ? "" : "/api/v1";
        await request(`${prefix}/network`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ offline: mode === "outage" }),
        });
        setDemoMode(mode);
        setDemoMessage(mode === "outage"
          ? "Outage simulation is on. Edge capture continues; cloud sync pauses."
          : "Normal uplink restored. Pending snapshots can retry.");
      } else {
        const request = isEdge ? getJSON : getEdgeJSON;
        const prefix = isEdge ? "" : "/api/v1";
        await request(`${prefix}/demo/corrupt-next-upload`, { method: "POST" });
        setDemoMode("anomaly");
        setDemoMessage("Integrity test armed for the next upload. The server should reject it and the uploader will retry.");
      }
      await refreshStory();
    } catch (reason) {
      setDemoMessage(`Demo control failed: ${reason.message}`);
    } finally {
      setDemoBusy(false);
    }
  }

  async function askAgent(event) {
    event.preventDefault();
    const trimmed = question.trim();
    if (!trimmed || !missionId || isEdge || asking) return;
    setQuestion("");
    setMessages((items) => [...items, { role: "user", answer: trimmed }]);
    setAsking(true);
    try {
      const response = await getJSON(`/missions/${encodeURIComponent(missionId)}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: trimmed }),
      });
      setMessages((items) => [...items, {
        role: "assistant",
        answer: response.answer,
        tools: response.tool_calls || [],
        generatedBy: response.generated_by,
      }]);
    } catch (reason) {
      setMessages((items) => [...items, {
        role: "assistant",
        answer: `The mission assistant could not answer: ${reason.message}`,
        tools: [],
        generatedBy: "unavailable",
      }]);
    } finally {
      setAsking(false);
    }
  }

  async function copyMissionPrompt() {
    const missionScope = missionId ? `For mission ${missionId}` : "For the latest available mission";
    const prompt = `${missionScope}, summarize the latest drone GPS fix, the densest estimated sightings, and any unlocated tracks. Cite the snapshot version and explain uncertainty.`;
    setPromptText(prompt);
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(prompt);
        setPromptCopyStatus("copied");
        return;
      }
    } catch { /* Use the selection-based fallback when clipboard permission is denied. */ }

    const temporaryInput = document.createElement("textarea");
    temporaryInput.value = prompt;
    temporaryInput.setAttribute("readonly", "");
    temporaryInput.style.position = "fixed";
    temporaryInput.style.opacity = "0";
    document.body.appendChild(temporaryInput);
    let copied = false;
    try {
      temporaryInput.select();
      copied = document.execCommand("copy");
    } catch { /* The visible textarea below remains available for manual copying. */ }
    finally { temporaryInput.remove(); }
    setPromptCopyStatus(copied
      ? "copied"
      : `Could not access the clipboard. Select the prompt below and copy it manually.`);
  }

  const suggestedQuestions = [
    "What is the latest drone GPS fix?",
    "Where are the densest estimated sightings?",
    "Are there unlocated sightings or data gaps?",
  ];

  return (
    <div className="story-shell">
      <MatrixTrail />
      <header className="story-nav">
        <a className="story-brand" href="#/story" aria-label="RescueSight overview">
          <span className="story-brand-mark"><Crosshair size={19} /></span>
          <span>rescue<span className="brand-accent">sight</span></span>
        </a>
        <div className="story-nav-right">
          <span className={`story-live ${error ? "offline" : ""}`}><i />{error ? "API unreachable" : status?.running ? "Mission running" : "Mission system"}</span>
          <button className="story-console-link" onClick={onDashboard}>Open mission control <ArrowUpRight size={15} /></button>
        </div>
      </header>

      <ProductScrollReveal
        missionData={missionData}
        missionId={missionId}
        missions={missions}
        onMissionChange={(nextMission) => {
          setMissionId(nextMission);
          setMessages([]);
          setPromptCopyStatus("");
        }}
        status={status}
        telemetry={telemetry}
        summary={summary}
      />

      <main>

        <section className="story-ribbon" aria-label="System values">
          <span>ONBOARD WORKFLOW</span>
          <b>Capture</b><i />
          <b>Infer</b><i />
          <b>Geolocate</b><i />
          <b>Package locally</b><i />
          <b>Sync when connected</b>
        </section>

        <section className="story-pipeline-section" id="story-pipeline">
          <div className="story-section-heading">
            <div><span className="story-section-index">01 — THE ONBOARD DATA PATH</span><h2>The aircraft computes first. The cloud catches up later.</h2></div>
            <p>Expand a stage for the actual hardware role, generated data, and behavior when a connection is unavailable.</p>
          </div>
          <div className="story-pipeline" aria-label="Interactive data pipeline">
            {pipelineNodes.map((node, index) => {
              const NodeIcon = node.icon;
              const expanded = selectedNode === node.id;
              return (
                <article className={`story-node ${expanded ? "selected" : ""}`} key={node.id}>
                  <button className="story-node-toggle" onClick={() => setSelectedNode(expanded ? null : node.id)} aria-expanded={expanded}>
                    <span className="story-node-overline"><span>{node.eyebrow}</span><NodeIcon size={16} /></span>
                    <strong>{node.label}</strong>
                    <small>{node.value}</small>
                    <span className="story-node-expand">{expanded ? "Close details −" : "Explore stage +"}</span>
                  </button>
                  <AnimatePresence initial={false}>
                    {expanded && (
                      <motion.div
                        className="story-node-expanded"
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: "auto", opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        transition={{ duration: 0.28, ease: "easeOut" }}
                      >
                        <p>{nodeDescriptions[node.id].text}</p>
                        <ul>{nodeDescriptions[node.id].facts.map((fact) => <li key={fact}>{fact}</li>)}</ul>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </article>
              );
            })}
          </div>
        </section>

        <section className="story-compare-section" aria-labelledby="story-compare-title">
          <div className="story-section-heading">
            <div>
              <span className="story-section-index">02 — THE PROBLEM / THE FIX</span>
              <h2 id="story-compare-title">No signal? <PointerHighlight>The drone still maps the search.</PointerHighlight></h2>
            </div>
            <p>Instead of waiting to upload video, RescueSight finds possible people and estimates their locations onboard. Only the compact map and coordinate file need to sync.</p>
          </div>
          <CompareSlider />
        </section>

        <section className="story-demo-section">
          <div className="story-demo-copy">
            <span className="story-section-index">03 — RESILIENCE, BY DESIGN</span>
            <h2>No network? No pause<br />in onboard processing.</h2>
            <p>Detection, location estimates, heatmap generation, and compact package storage run on the aircraft. The uplink is only needed to transfer the package—not to make it.</p>
              <div className="story-demo-controls" role="group" aria-label="Edge demo controls">
                <button className={demoMode === "normal" ? "active" : ""} onClick={() => setDemoState("normal")} disabled={demoBusy || (connectedEdge && !connectedEdgeStatus?.network_demo_available)}><Wifi size={15} /> Normal operation</button>
                <button className={demoMode === "outage" ? "active warning" : ""} onClick={() => setDemoState("outage")} disabled={demoBusy || (connectedEdge && !connectedEdgeStatus?.network_demo_available)}><WifiOff size={15} /> Simulate outage</button>
                <button className={demoMode === "anomaly" ? "active warning" : ""} onClick={() => setDemoState("anomaly")} disabled={demoBusy || (connectedEdge && !connectedEdgeStatus?.demo_controls_available)}><AlertTriangle size={15} /> Inject upload fault</button>
            </div>
              <p className="story-demo-feedback" role="status">{demoMessage || (connectedEdge && connectedEdgeStatus?.demo_controls_available ? connectedEdgeStatus.network_demo_available ? "Controls affect the connected edge uploader; onboard processing continues locally." : "Upload integrity testing is available. Configure the edge offline flag to enable the network controls." : connectedEdge ? "The edge service is connected without an uploader; demo controls are unavailable." : "Preview mode only: start/connect the edge service to control its real uploader.")}</p>
          </div>
            <div className={`story-resilience-graphic ${(connectedEdge ? connectedUploader?.online === false : demoMode === "outage") ? "offline" : ""}`}>
              <div className="resilience-label"><span>EDGE BUFFER</span><span>{(connectedEdge ? connectedUploader?.online === false : demoMode === "outage") ? "UPLINK PAUSED" : demoMode === "anomaly" && !connectedEdge ? "FAULT TEST PREVIEW" : "SYNC MONITOR"}</span></div>
            <div className="resilience-flow">
              <div className="resilience-box"><Camera size={19} /><b>Frame</b><small>edge capture</small></div>
              <div className="resilience-wire"><i /></div>
              <div className="resilience-box"><PackageCheck size={19} /><b>Snapshot</b><small>{connectedUploader?.pending ? "queued locally" : "versioned locally"}</small></div>
              <div className="resilience-wire"><i /></div>
              <div className="resilience-box"><Database size={19} /><b>Cloud</b><small>{(connectedEdge ? connectedUploader?.online === false : demoMode === "outage") ? "waiting for link" : demoMode === "anomaly" && !connectedEdge ? "retry test preview" : "sync when available"}</small></div>
            </div>
            <div className="resilience-foot"><span>LOCAL VERSION <b>{connectedUploader?.local_version ?? manifest?.version ?? "—"}</b></span><span>ACKNOWLEDGED <b>{connectedUploader?.acked_version ?? "—"}</b></span></div>
          </div>
        </section>

        <section className="story-assistant-section">
          <div className="story-assistant-intro">
            <span className="story-section-index">04 — AFTER THE UPLINK RETURNS</span>
            <h2>Then the mission package can reach the ground.</h2>
            <p>Once a link is available, the compact snapshot syncs to the cloud. The optional Gemini assistant can then query its GPS, sightings, data gaps, and nearby landmarks. None of that is required for onboard detection or map generation.</p>
            <div className={`story-agent-state ${aiStatus?.configured && aiStatus?.integration_installed ? "ready" : ""}`}>
              <span className="agent-state-dot" />
              {aiStatus?.configured && aiStatus?.integration_installed
                ? `Gemini ready · ${aiStatus.model}`
                : aiStatus?.integration_installed
                  ? "Deterministic fallback · Gemini key not configured"
                  : "Agent requires the cloud API and LangChain integration"}
            </div>
            {isEdge ? (
              <div className="automated-report-note" role="status">
                <b>Automated mission report</b>
                <span>Generated by the cloud agent after this aircraft syncs a snapshot. Reporting is not required for onboard processing.</span>
              </div>
            ) : missionId && (
              <div className="automated-report-note" role="status">
                <b>Post-upload report · {automatedReport?.status === "pending" ? "preparing" : automatedReport?.status === "failed" ? "failed" : automatedReport?.generated_by || "waiting"}</b>
                <span>{automatedReport?.report || automatedReport?.error || "A new report is created automatically after each accepted snapshot."}</span>
              </div>
            )}
            <div className="story-tool-list">
              {["Latest drone GPS", "Sightings & data gaps", "Nearby landmarks", "Mission summary"].map((item, index) => (
                <span key={item}><b>0{index + 1}</b>{item}</span>
              ))}
            </div>
            <div className="pixel-prompt-control">
              <button
                type="button"
                className={`pixel-prompt-button ${promptCopyStatus === "copied" ? "copied" : ""}`}
                onClick={copyMissionPrompt}
                aria-label="Copy a mission-specific assistant prompt"
              >
                <span className="pixel-prompt-grid" aria-hidden="true">
                  {Array.from({ length: 48 }, (_, index) => (
                    <i key={index} style={{ gridColumn: (index % 12) + 1, gridRow: Math.floor(index / 12) + 1, "--pixel-index": index }} />
                  ))}
                </span>
                <span className="pixel-prompt-label">{promptCopyStatus === "copied" ? "PROMPT COPIED" : "COPY A MISSION PROMPT"}</span>
                <ArrowUpRight size={14} aria-hidden="true" />
              </button>
              <span className={`pixel-prompt-status ${promptCopyStatus.startsWith("Could not") ? "error" : ""}`} role="status" aria-live="polite">
                {promptCopyStatus.startsWith("Could not") ? promptCopyStatus : promptCopyStatus === "copied" ? "Paste it into the assistant when ready." : missionId ? "Copies a scoped question for this mission." : "Copies a prompt for the latest available mission."}
              </span>
              {promptCopyStatus.startsWith("Could not") && (
                <textarea
                  className="prompt-copy-fallback"
                  aria-label="Select and copy the mission prompt"
                  value={promptText}
                  readOnly
                  onFocus={(event) => event.currentTarget.select()}
                />
              )}
            </div>
          </div>
          <div className="story-chat">
            <div className="story-chat-header">
              <div><span className="chat-led" /><b>MISSION ASSISTANT</b></div>
              <span>{missionId || "NO MISSION SELECTED"}</span>
            </div>
            <div className="story-chat-messages" aria-live="polite">
              {!messages.length && (
                <div className="story-chat-welcome">
                  <span className="chat-glyph"><Bot size={19} /></span>
                  <b>Ask a question about this mission.</b>
                  <p>{isEdge
                    ? "Mission questions require the cloud API. Edge capture remains local and available."
                    : "Answers are scoped to the current snapshot and show the tools that were called."}</p>
                  <div className="story-suggestions">
                    {suggestedQuestions.map((item) => <button key={item} onClick={() => setQuestion(item)} disabled={isEdge}>{item}<ChevronRight size={13} /></button>)}
                  </div>
                </div>
              )}
              {messages.map((item, index) => (
                <div className={`story-message ${item.role}`} key={`${item.role}-${index}`}>
                  <span>{item.role === "user" ? "YOU" : item.generatedBy === "gemini" ? "GEMINI / LANGCHAIN" : "MISSION ASSISTANT"}</span>
                  <p>{item.answer}</p>
                  {item.tools?.length > 0 && <div className="story-tool-calls"><b>TOOLS CALLED</b>{item.tools.map((tool, i) => <code key={`${tool.tool}-${i}`}>{tool.tool}</code>)}</div>}
                </div>
              ))}
              {asking && <div className="story-thinking" role="status"><i /><i /><i /> Querying mission tools…</div>}
            </div>
            <form className="story-chat-form" onSubmit={askAgent}>
              <label className="sr-only" htmlFor="mission-question">Ask about this mission</label>
              <input id="mission-question" value={question} onChange={(event) => setQuestion(event.target.value)} maxLength={1000} placeholder={isEdge ? "Mission Q&A requires the cloud API…" : missionId ? "Ask about GPS, sightings, or data quality…" : "Waiting for mission data…"} disabled={!missionId || isEdge || asking} />
              <button type="submit" disabled={!missionId || isEdge || !question.trim() || asking} aria-label="Send question">{asking ? <RefreshCw className="spin" size={16} /> : <Send size={16} />}</button>
            </form>
            <div className="story-chat-foot"><span><ShieldAlert size={13} /> Estimates only · human review required</span><span>{question.length}/1000</span></div>
          </div>
        </section>
      </main>
      <footer className="story-footer">
        <a className="story-brand" href="#/story"><span className="story-brand-mark"><Crosshair size={17} /></span><span>rescue<span className="brand-accent">sight</span></span></a>
        <span>Decision support for field teams. Not a replacement for human judgment.</span>
        <button onClick={onDashboard}>Return to operations <ArrowUpRight size={14} /></button>
      </footer>
    </div>
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
      setReport(
        await getJSON(`/missions/${encodeURIComponent(missionId)}/ai-report`, { method: "POST" }),
      );
    } catch (reason) {
      setReport({ report: `Could not generate the report: ${reason.message}` });
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
                <div className="health-row">
                  <span className="health-icon"><Satellite size={14} /></span>
                  <span>Telemetry samples</span>
                  <b className="health-value">{status?.telemetry_samples ?? "—"}</b>
                </div>
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
                    <b>Automatic report</b>
                    <span>{automatedReport?.status === "pending"
                      ? "Generating from the latest accepted snapshot…"
                      : automatedReport?.status === "failed"
                        ? automatedReport.error
                      : automatedReport?.report
                        ? `${automatedReport.generated_by === "gemini" ? "Gemini + mission tools" : "Deterministic fallback"} · snapshot v${automatedReport.snapshot_version}`
                        : "Created after each snapshot is accepted by the cloud."}</span>
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

function App() {
  const [storyPage, setStoryPage] = useState(window.location.hash === "#/story");

  useEffect(() => {
    document.body.classList.toggle("story-mode", storyPage);
    return () => document.body.classList.remove("story-mode");
  }, [storyPage]);

  useEffect(() => {
    function syncPage() {
      setStoryPage(window.location.hash === "#/story");
    }
    window.addEventListener("hashchange", syncPage);
    return () => window.removeEventListener("hashchange", syncPage);
  }, []);

  function openDashboard() {
    window.location.hash = "/dashboard";
  }

  return storyPage
    ? <RescueStoryPage onDashboard={openDashboard} />
    : <MissionControl />;
}

export default App;
