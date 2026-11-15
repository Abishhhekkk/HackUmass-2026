// Vantage mission control: one screen, map first.
import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Command } from "cmdk";
import {
  ArrowDownToLine,
  ChevronDown,
  Command as CommandIcon,
  ExternalLink,
  FileText,
  Layers,
  Map as MapIcon,
  RefreshCw,
  Route,
  Search,
  Users,
  X,
} from "lucide-react";
import { LINK_LABEL, NO_SERVICE, UNLOCATED_REASONS, clock, coord, describeReport, num, useMission } from "./lib.jsx";
import { BASEMAPS, HEAT, MapView, Wordmark } from "./ui.jsx";

const WINDOWS = [
  { value: 0, label: "All" },
  { value: 60, label: "1 min" },
  { value: 300, label: "5 min" },
  { value: 900, label: "15 min" },
];

function useClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(t);
  }, []);
  return now;
}

function Reading({ label, value, unit, tone, big }) {
  return (
    <div className={`reading ${big ? "big" : ""} ${tone || ""}`}>
      <span className="label">{label}</span>
      <span className="reading-value">
        {value ?? "—"}
        {unit && value !== "—" && value !== undefined && <small>{unit}</small>}
      </span>
    </div>
  );
}

/** Sightings over the mission's data window, as a strip of bars, with the time filter shown. */
function Timeline({ people, manifest, windowSeconds }) {
  const start = Date.parse(manifest?.data_window?.start || "");
  const end = Date.parse(manifest?.data_window?.end || "");
  const bins = 60;
  const counts = useMemo(() => {
    const c = new Array(bins).fill(0);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return c;
    for (const f of people) {
      const t = Date.parse(f.properties.first_seen_utc);
      if (!Number.isFinite(t)) continue;
      const i = Math.min(bins - 1, Math.max(0, Math.floor(((t - start) / (end - start)) * bins)));
      c[i] += 1;
    }
    return c;
  }, [people, start, end]);
  const max = Math.max(1, ...counts);
  const span = end - start;
  const cut = windowSeconds > 0 && span > 0 ? Math.max(0, 1 - (windowSeconds * 1000) / span) : 0;

  return (
    <div className="timeline" aria-label="Estimated people by time first seen">
      <span className="label timeline-edge">{clock(manifest?.data_window?.start)}</span>
      <div className="timeline-bars">
        {counts.map((c, i) => (
          <i key={i} style={{ height: `${c ? 18 + (c / max) * 82 : 4}%` }} className={c ? "has" : ""} />
        ))}
        {cut > 0 && <span className="timeline-cut" style={{ width: `${cut * 100}%` }} />}
      </div>
      <span className="label timeline-edge">{clock(manifest?.data_window?.end)}</span>
    </div>
  );
}

function Inspect({ selected, people, onSelect }) {
  if (selected?.type === "person") {
    const p = selected.properties;
    return (
      <div className="inspect">
        <div className="inspect-head">
          <span className="label">Estimated sighting</span>
          <button type="button" className="icon-btn" onClick={() => onSelect(null)} aria-label="Clear selection">
            <X size={15} />
          </button>
        </div>
        <h3 className="inspect-id mono">{p.person_id}</h3>
        <dl className="facts">
          <dt>Position</dt>
          <dd className="mono">{coord(selected.lat, selected.lon)}</dd>
          <dt>Search radius</dt>
          <dd className="mono">±{p.horizontal_error_m} m</dd>
          <dt>Confidence</dt>
          <dd className="mono">{Math.round((p.confidence || 0) * 100)}%</dd>
          <dt>First seen</dt>
          <dd className="mono">{clock(p.first_seen_utc)}</dd>
          <dt>Last seen</dt>
          <dd className="mono">{clock(p.last_seen_utc)}</dd>
          <dt>Tracks merged</dt>
          <dd className="mono">{p.n_tracks ?? "—"}</dd>
          <dt>Grid cell</dt>
          <dd className="mono">{p.cell_id ?? "—"}</dd>
        </dl>
        <p className="fine">
          The true position is somewhere inside the ring, most likely near the centre. Confirm before
          sending anyone.
        </p>
      </div>
    );
  }
  if (selected?.type === "cell") {
    const p = selected.properties;
    return (
      <div className="inspect">
        <div className="inspect-head">
          <span className="label">Grid cell</span>
          <button type="button" className="icon-btn" onClick={() => onSelect(null)} aria-label="Clear selection">
            <X size={15} />
          </button>
        </div>
        <h3 className="inspect-id mono">{p.cell_id}</h3>
        <p className="inspect-count">
          <b>{p.count}</b> estimated {p.count === 1 ? "person" : "people"}
        </p>
        <PeopleList people={selected.people.map((props) => ({ properties: props.properties || props, geometry: props.geometry }))} onSelect={onSelect} />
      </div>
    );
  }
  return (
    <div className="inspect">
      <p className="fine inspect-hint">Select a person or a cell on the map, or pick from the list.</p>
      <PeopleList people={people} onSelect={onSelect} />
    </div>
  );
}

function PeopleList({ people, onSelect }) {
  const sorted = useMemo(
    () => [...people].sort((a, b) => (b.properties.last_seen_utc || "").localeCompare(a.properties.last_seen_utc || "")),
    [people],
  );
  if (!sorted.length) return <p className="fine">No placed sightings in this snapshot.</p>;
  return (
    <ol className="rows">
      <li className="rows-head label">
        <span>ID</span>
        <span>Radius</span>
        <span>Conf.</span>
        <span>Last seen</span>
      </li>
      {sorted.map((f) => {
        const p = f.properties;
        const coords = f.geometry?.coordinates;
        return (
          <li key={p.person_id}>
            <button
              type="button"
              onClick={() =>
                coords && onSelect({ type: "person", properties: p, lat: coords[1], lon: coords[0] })
              }
            >
              <span className="mono"><i className="pip" />{p.person_id}</span>
              <span className="mono">±{p.horizontal_error_m} m</span>
              <span className="mono">{Math.round((p.confidence || 0) * 100)}%</span>
              <span className="mono">{clock(p.last_seen_utc)}</span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

function Unplaced({ rows }) {
  if (rows === null) return <p className="fine">Loading…</p>;
  return (
    <div className="inspect">
      <p className="fine">
        Seen, but no position could be estimated. Kept with the reason — never moved to the drone&rsquo;s
        own position. Some may be the same people as placed sightings.
      </p>
      {rows.length ? (
        <ol className="rows rows-unplaced">
          <li className="rows-head label">
            <span>ID</span>
            <span>Reason</span>
            <span>Last seen</span>
          </li>
          {rows.slice(0, 80).map((r) => (
            <li key={r.person_id} title={`Tracks: ${r.track_ids}`}>
              <div>
                <span className="mono"><i className="pip hollow" />{r.person_id}</span>
                <span>{UNLOCATED_REASONS[r.unlocated_reason] || r.unlocated_reason || "Unknown"}</span>
                <span className="mono">{clock(r.last_seen_utc)}</span>
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <p className="fine strong">Every sighting in this snapshot has an estimated position.</p>
      )}
      {rows.length > 80 && <p className="fine">+{rows.length - 80} more in the sightings CSV.</p>}
    </div>
  );
}

function Reports({ m, onRead, busy, onGenerate }) {
  return (
    <div className="inspect">
      <span className="label">Exports · from this snapshot</span>
      <div className="exports">
        <a href={m.files("sightings.csv")} download>
          <ArrowDownToLine size={15} /> Sightings <span className="mono">CSV</span>
        </a>
        <a href={m.files("heatmap.geojson")} download>
          <ArrowDownToLine size={15} /> Map layers <span className="mono">GeoJSON</span>
        </a>
        <a href={m.files("report.md")} target="_blank" rel="noreferrer">
          <ExternalLink size={15} /> Snapshot report <span className="mono">MD</span>
        </a>
      </div>
      <span className="label section-gap">Mission report</span>
      {m.role === "cloud" || m.role === "demo" ? (
        <>
          <p className="fine">{describeReport(m.aiReport)}</p>
          <div className="report-actions">
            {m.aiReport?.report && (
              <button type="button" className="btn btn-ghost" onClick={() => onRead(m.aiReport)}>
                <FileText size={15} /> Read report
              </button>
            )}
            {m.role === "cloud" && (
              <button type="button" className="btn btn-signal" onClick={onGenerate} disabled={busy || !m.missionId}>
                <RefreshCw size={15} className={busy ? "spin" : ""} /> {busy ? "Writing…" : "Write a fresh one"}
              </button>
            )}
          </div>
          <p className="fine">
            Written by Gemini from the mission tools on a schedule or on request; a deterministic
            report is used if Gemini is unavailable.
          </p>
        </>
      ) : (
        <p className="fine">
          AI reports are written by the cloud service. On the drone, the snapshot report above is
          always available.
        </p>
      )}
    </div>
  );
}

export default function Console() {
  const m = useMission();
  const now = useClock();
  const { manifest, summary, people, cells, unlocated, flightPath, status, uploader, link } = m;
  const [tab, setTab] = useState("inspect");
  const [selected, setSelected] = useState(null);
  const [windowSeconds, setWindowSeconds] = useState(0);
  const [layers, setLayers] = useState({ cells: true, people: true, path: true });
  const [basemap, setBasemap] = useState("off");
  const nextBasemap = () => setBasemap((b) => (b === "off" ? "satellite" : b === "satellite" ? "streets" : "off"));
  const [palette, setPalette] = useState(false);
  const [toast, setToast] = useState("");
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState(null);

  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPalette((v) => !v);
      }
      if (e.key === "Escape") setSelected(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (!toast) return undefined;
    const t = window.setTimeout(() => setToast(""), 4200);
    return () => window.clearTimeout(t);
  }, [toast]);

  const end = Date.parse(manifest?.data_window?.end || "") || Date.now();
  const cutoffMs = windowSeconds > 0 ? end - windowSeconds * 1000 : null;
  const toggle = (k) => setLayers((l) => ({ ...l, [k]: !l[k] }));

  function select(item) {
    setSelected(item);
    if (item) setTab("inspect");
  }

  async function run(fn, ok) {
    setBusy(true);
    try {
      await fn();
      if (ok) setToast(ok);
    } catch (reason) {
      setToast(reason.message);
    } finally {
      setBusy(false);
    }
  }

  const generate = () =>
    run(async () => {
      const r = await m.actions.generateReport();
      if (r?.report) setReading(r);
    });

  const sync = uploader
    ? `local v${uploader.local_version ?? 0} · cloud v${uploader.acked_version ?? 0}`
    : (m.role === "cloud" || m.role === "demo") && manifest
      ? `received v${manifest.version} · ${clock(m.data?.received_at)}`
      : "—";

  return (
    <div className="console">
      <header className="c-top">
        <Wordmark label="Vantage home" />
        <span className="c-top-sep" />
        <label className="c-mission">
          <span className="label">Mission</span>
          <span className="c-select">
            <select
              value={m.missionId}
              onChange={(e) => {
                m.actions.chooseMission(e.target.value);
                setSelected(null);
              }}
              aria-label="Mission"
            >
              {!m.missions.length && <option value="">No missions yet</option>}
              {m.missions.map((x) => (
                <option key={x.mission_id} value={x.mission_id}>
                  {x.mission_id}
                </option>
              ))}
            </select>
            <ChevronDown size={14} />
          </span>
        </label>
        <span className={`c-chip tone-${link}`}>
          <i className={`dot ${status?.running ? "live" : ""}`} />
          <span className="mono">{m.error === NO_SERVICE ? "Website only" : m.error && !status ? "Service unreachable" : LINK_LABEL[link]}</span>
        </span>
        <span className="c-top-right">
          <span className="c-source mono">{m.role === "cloud" ? "CLOUD" : m.role === "edge" ? "ON BOARD" : m.role === "demo" ? "CLOUD" : "—"}</span>
          <span className="c-clock mono">{clock(now)}</span>
          <button type="button" className="btn btn-ghost c-cmd" onClick={() => setPalette(true)}>
            <CommandIcon size={14} /> <span>Actions</span> <kbd className="mono">Ctrl K</kbd>
          </button>
        </span>
      </header>

      {m.error && m.error !== NO_SERVICE && (
        <div className="c-error" role="alert">
          <span className="mono">{m.error}</span>
          <button type="button" onClick={m.actions.refresh}>Retry</button>
        </div>
      )}

      <aside className="c-left" aria-label="Mission readings">
        <div className="c-block">
          <Reading big tone="signal" label="People placed" value={num(summary.estimated_people)} />
          <div className="reading-pair">
            <Reading label="Not placed" value={num(summary.unlocated_tracks)} />
            <Reading label="Cells" value={num(summary.occupied_cells)} />
          </div>
          <div className="reading-pair">
            <Reading label="Median radius" value={summary.median_error_m ?? "—"} unit="m" />
            <Reading label="90% radius" value={summary.p90_error_m ?? "—"} unit="m" />
          </div>
          <div className="reading-pair">
            <Reading label="Frames" value={num(status?.frames ?? summary.frames_processed)} />
            <Reading label="Detections" value={num(status?.detections ?? summary.detections_processed)} />
          </div>
        </div>

        <div className="c-block">
          <span className="label">Pipeline</span>
          <dl className="facts compact">
            <dt>Detector</dt>
            <dd>{status?.running ? `running · ${status?.scheduler?.mode || "active"}` : manifest ? "idle · results kept" : "waiting"}</dd>
            <dt>Positions</dt>
            <dd className="mono">{manifest?.position_method || "—"}</dd>
            <dt>Telemetry</dt>
            <dd className="mono">{m.role === "edge" ? num(status?.telemetry_samples) : "—"}</dd>
            <dt>Last seen</dt>
            <dd className="mono">{clock(manifest?.data_window?.end)}</dd>
            <dt>Snapshot</dt>
            <dd className="mono">{manifest ? `v${manifest.version} · ${clock(manifest.created_at)}` : "—"}</dd>
          </dl>
        </div>

        <div className="c-block">
          <span className="label">Uplink</span>
          <p className={`c-link tone-${link}`}>
            <i className="dot" /> {LINK_LABEL[link]}
          </p>
          <p className="mono c-sync">{sync}</p>
          {uploader?.needs_attention && <p className="c-warn">Needs attention: {uploader.last_error}</p>}
          {(m.canDriveLink || m.canCorrupt) && (
            <div className="c-demo">
              <span className="label">Demo</span>
              <div className="seg">
                <button
                  type="button"
                  className={link !== "offline" ? "on" : ""}
                  disabled={busy || !m.canDriveLink || link !== "offline"}
                  onClick={() => run(() => m.actions.setOffline(false), "Link restored. The newest snapshot goes up.")}
                >
                  Link up
                </button>
                <button
                  type="button"
                  className={link === "offline" ? "on off" : ""}
                  disabled={busy || !m.canDriveLink || link === "offline"}
                  onClick={() => run(() => m.actions.setOffline(true), "Link cut. Capture continues on board.")}
                >
                  Cut link
                </button>
              </div>
              <button
                type="button"
                className="btn btn-ghost c-fault"
                disabled={busy || !m.canCorrupt}
                onClick={() => run(m.actions.corruptNextUpload, "Next upload will be corrupted; the cloud rejects it and the drone retries.")}
              >
                Corrupt next upload
              </button>
            </div>
          )}
        </div>
      </aside>

      <main className="c-map">
        <div className="c-map-tools">
          <div className="seg" role="group" aria-label="Show people last seen within">
            {WINDOWS.map((w) => (
              <button type="button" key={w.value} className={windowSeconds === w.value ? "on" : ""} onClick={() => setWindowSeconds(w.value)}>
                {w.label}
              </button>
            ))}
          </div>
          <div className="seg" role="group" aria-label="Map layers">
            <button type="button" className={layers.people ? "on" : ""} aria-pressed={layers.people} onClick={() => toggle("people")} title="People">
              <Users size={14} /> <span>People</span>
            </button>
            <button type="button" className={layers.cells ? "on" : ""} aria-pressed={layers.cells} onClick={() => toggle("cells")} title="Density grid">
              <Layers size={14} /> <span>Grid</span>
            </button>
            <button type="button" className={layers.path ? "on" : ""} aria-pressed={layers.path} onClick={() => toggle("path")} title="Drone path">
              <Route size={14} /> <span>Path</span>
            </button>
            <button type="button" className={basemap !== "off" ? "on" : ""} onClick={nextBasemap} title="Background map: off, satellite, streets (needs internet)">
              <MapIcon size={14} /> <span>{basemap === "off" ? "Basemap" : BASEMAPS[basemap].label}</span>
            </button>
          </div>
        </div>

        <div className="c-map-frame">
          {manifest ? (
            <MapView
              people={people}
              cells={cells}
              flightPath={flightPath}
              showCells={layers.cells}
              showPeople={layers.people}
              showPath={layers.path}
              basemap={basemap}
              cutoffMs={cutoffMs}
              selectedId={selected?.type === "person" ? selected.properties.person_id : null}
              onSelect={select}
            />
          ) : (
            <div className="c-empty">
              <span className="label">
                {m.loaded ? (m.error === NO_SERVICE ? "Website only" : m.error ? "Not connected" : "No mission yet") : "Connecting"}
              </span>
              <p>
                {m.error === NO_SERVICE
                  ? "This copy of Vantage has no mission service behind it. Live missions appear on the Jetson's dashboard and on the cloud API's address."
                  : m.error
                    ? "The map comes back as soon as the service answers."
                    : "Start the edge pipeline, or wait for the drone's first snapshot to reach the cloud."}
              </p>
              <a className="btn btn-ghost" href="#/">See the example flight on the home page</a>
            </div>
          )}
          <div className="c-legend">
            <span><i className="lg-person" /> Estimated person</span>
            <span><i className="lg-ring" /> Search radius</span>
            {layers.path && flightPath?.features?.length > 0 && <span><i className="lg-path" /> Drone path · not sightings</span>}
            {layers.cells && (
              <span className="lg-heat">
                {HEAT.map((c) => <i key={c} style={{ background: c }} />)} Density
              </span>
            )}
          </div>
          {basemap !== "off" && <span className="c-attrib mono">{BASEMAPS[basemap].attribution}</span>}
        </div>

        <Timeline people={people} manifest={manifest} windowSeconds={windowSeconds} />
      </main>

      <aside className="c-right" aria-label="Details">
        <div className="tabs" role="tablist">
          <button type="button" role="tab" aria-selected={tab === "inspect"} className={tab === "inspect" ? "on" : ""} onClick={() => setTab("inspect")}>
            Inspect <span className="mono">{people.length}</span>
          </button>
          <button type="button" role="tab" aria-selected={tab === "unplaced"} className={tab === "unplaced" ? "on" : ""} onClick={() => setTab("unplaced")}>
            Not placed <span className="mono">{unlocated ? unlocated.length : "…"}</span>
          </button>
          <button type="button" role="tab" aria-selected={tab === "reports"} className={tab === "reports" ? "on" : ""} onClick={() => setTab("reports")}>
            Reports
          </button>
        </div>
        <div className="tab-body">
          {tab === "inspect" && <Inspect selected={selected} people={people} onSelect={select} />}
          {tab === "unplaced" && <Unplaced rows={unlocated} />}
          {tab === "reports" && <Reports m={m} onRead={setReading} busy={busy} onGenerate={generate} />}
        </div>
        <p className="c-safety">Decision support · every position and count is an estimate · a person reviews each result.</p>
      </aside>

      <AnimatePresence>
        {toast && (
          <motion.div className="c-toast" role="status" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 8 }}>
            {toast}
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {reading && (
          <motion.div className="c-modal" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={() => setReading(null)}>
            <motion.section
              className="c-report"
              role="dialog"
              aria-modal="true"
              aria-label="Mission report"
              initial={{ y: 16, opacity: 0 }}
              animate={{ y: 0, opacity: 1 }}
              exit={{ y: 10, opacity: 0 }}
              onClick={(e) => e.stopPropagation()}
            >
              <div className="inspect-head">
                <span className="label">
                  {reading.generated_by === "gemini" ? "Mission report · Gemini" : "Mission report · deterministic"} · snapshot v{reading.snapshot_version}
                </span>
                <button type="button" className="icon-btn" onClick={() => setReading(null)} aria-label="Close report">
                  <X size={16} />
                </button>
              </div>
              {reading.fallback_reason && <p className="c-warn">Gemini unavailable: {reading.fallback_reason}</p>}
              <pre className="report-text">{reading.report}</pre>
              <p className="fine">Estimated observations only. A person reviews every result.</p>
            </motion.section>
          </motion.div>
        )}
      </AnimatePresence>

      <Command.Dialog open={palette} onOpenChange={setPalette} label="Actions" className="cmdk">
        <div className="cmdk-panel">
          <div className="cmdk-search">
            <Search size={16} />
            <Command.Input autoFocus placeholder="Type an action…" />
            <kbd className="mono">Esc</kbd>
          </div>
          <Command.List>
            <Command.Empty>No matching action.</Command.Empty>
            <Command.Group heading="Map">
              <Command.Item onSelect={() => { setPalette(false); toggle("people"); }}>Toggle people</Command.Item>
              <Command.Item onSelect={() => { setPalette(false); toggle("cells"); }}>Toggle density grid</Command.Item>
              <Command.Item onSelect={() => { setPalette(false); toggle("path"); }}>Toggle drone path</Command.Item>
              <Command.Item onSelect={() => { setPalette(false); setBasemap("satellite"); }}>Satellite basemap</Command.Item>
              <Command.Item onSelect={() => { setPalette(false); setBasemap("streets"); }}>Street basemap</Command.Item>
              <Command.Item onSelect={() => { setPalette(false); setBasemap("off"); }}>No basemap (offline)</Command.Item>
              {WINDOWS.map((w) => (
                <Command.Item key={w.value} onSelect={() => { setPalette(false); setWindowSeconds(w.value); }}>
                  Show {w.value ? `people seen in the last ${w.label}` : "all people"}
                </Command.Item>
              ))}
            </Command.Group>
            <Command.Group heading="Mission">
              <Command.Item onSelect={() => { setPalette(false); m.actions.refresh(); }}>Refresh now</Command.Item>
              <Command.Item onSelect={() => { setPalette(false); setTab("unplaced"); }}>Show sightings without a position</Command.Item>
              <Command.Item onSelect={() => { setPalette(false); window.location.assign(m.files("sightings.csv")); }}>Download sightings CSV</Command.Item>
              <Command.Item onSelect={() => { setPalette(false); window.location.assign(m.files("heatmap.geojson")); }}>Download GeoJSON</Command.Item>
              {m.role === "cloud" && <Command.Item onSelect={() => { setPalette(false); generate(); }}>Write a fresh mission report</Command.Item>}
            </Command.Group>
            {(m.canDriveLink || m.canCorrupt) && (
              <Command.Group heading="Demo">
                {m.canDriveLink && <Command.Item onSelect={() => { setPalette(false); run(() => m.actions.setOffline(true), "Link cut."); }}>Cut the link</Command.Item>}
                {m.canDriveLink && <Command.Item onSelect={() => { setPalette(false); run(() => m.actions.setOffline(false), "Link restored."); }}>Restore the link</Command.Item>}
                <Command.Item onSelect={() => { setPalette(false); run(m.actions.corruptNextUpload, "Next upload will be corrupted."); }}>Corrupt the next upload</Command.Item>
              </Command.Group>
            )}
            <Command.Group heading="Go">
              <Command.Item onSelect={() => { setPalette(false); window.location.hash = "/"; }}>Vantage home</Command.Item>
            </Command.Group>
          </Command.List>
        </div>
      </Command.Dialog>
    </div>
  );
}
