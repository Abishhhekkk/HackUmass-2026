// Mission replay: the drone's GPS track draws itself and each estimated person appears at the
// moment they were first seen, with their error ring. Real mission data when available; a
// clearly labelled example otherwise.
import { useEffect, useMemo, useRef, useState } from "react";
import { Pause, Play } from "lucide-react";
import { coord } from "./lib.jsx";

const W = 1000;
const H = 760;
const PAD = 70;
const LOOP_MS = 16000;

function seeded(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let r = Math.imul(s ^ (s >>> 15), 1 | s);
    r ^= r + Math.imul(r ^ (r >>> 7), 61 | r);
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
}

// A lawnmower survey over ~260 x 180 m with nine people: used only when no mission is connected.
function exampleMission() {
  const lat0 = 42.3893;
  const lon0 = -72.528;
  const kx = Math.cos((lat0 * Math.PI) / 180) * 111320;
  const ky = 110540;
  const toLL = (x, y) => [lon0 + x / kx, lat0 + y / ky];
  const path = [];
  for (let i = 0; i <= 6; i += 1) {
    const y = -90 + i * 30;
    const xs = i % 2 ? [130, -130] : [-130, 130];
    path.push(toLL(xs[0], y), toLL(xs[1], y));
  }
  const rand = seeded(7);
  const start = Date.parse("2026-11-15T14:00:00Z");
  const people = Array.from({ length: 9 }, (_, i) => {
    const t = 0.08 + (i / 9) * 0.85 + rand() * 0.05;
    const leg = Math.min(6, Math.floor(t * 7));
    const y = -90 + leg * 30 + (rand() - 0.5) * 18;
    const x = (rand() - 0.5) * 230;
    return {
      geometry: { coordinates: toLL(x, y) },
      properties: {
        person_id: `P${String(i + 1).padStart(3, "0")}`,
        horizontal_error_m: 3 + rand() * 6,
        confidence: 0.4 + rand() * 0.5,
        first_seen_utc: new Date(start + t * 600000).toISOString(),
      },
    };
  });
  return {
    segments: [path],
    people,
    start,
    end: start + 600000,
  };
}

function project(points) {
  const lat0 = points.reduce((s, p) => s + p[1], 0) / points.length;
  const kx = Math.cos((lat0 * Math.PI) / 180) * 111320;
  const ky = 110540;
  const xs = points.map((p) => p[0] * kx);
  const ys = points.map((p) => p[1] * ky);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const spanX = Math.max(maxX - minX, 40);
  const spanY = Math.max(maxY - minY, 40);
  const scale = Math.min((W - 2 * PAD) / spanX, (H - 2 * PAD) / spanY);
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  return {
    scale, // px per metre
    xy: ([lon, lat]) => [
      W / 2 + (lon * kx - cx) * scale,
      H / 2 - (lat * ky - cy) * scale,
    ],
  };
}

function niceMetres(pxPerM) {
  const target = 160 / pxPerM;
  const steps = [5, 10, 20, 25, 50, 100, 200, 250, 500, 1000];
  return steps.find((s) => s >= target) || 1000;
}

export default function Replay({ people, flightPath, manifest, live }) {
  const reduce = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  const real = people.length > 0 || (flightPath?.features?.length || 0) > 0;

  const mission = useMemo(() => {
    if (!real) return exampleMission();
    const segments = (flightPath?.features || []).map((f) => f.geometry.coordinates);
    const firsts = people.map((p) => Date.parse(p.properties.first_seen_utc)).filter(Number.isFinite);
    const start =
      Date.parse(flightPath?.properties?.start || "") ||
      Date.parse(manifest?.data_window?.start || "") ||
      Math.min(...firsts);
    const end =
      Date.parse(flightPath?.properties?.end || "") ||
      Date.parse(manifest?.data_window?.end || "") ||
      Math.max(...firsts);
    return { segments, people, start, end: end > start ? end : start + 1 };
  }, [real, people, flightPath, manifest]);

  const geo = useMemo(() => {
    const pts = [...mission.segments.flat(), ...mission.people.map((p) => p.geometry.coordinates)];
    return project(pts.length ? pts : [[0, 0]]);
  }, [mission]);

  // Path geometry in px with cumulative length (progress along the path ≈ progress in time).
  const track = useMemo(() => {
    const segs = mission.segments.map((seg) => seg.map(geo.xy));
    let total = 0;
    const parts = segs.map((pts) => {
      let len = 0;
      for (let i = 1; i < pts.length; i += 1) len += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
      const part = { pts, from: total, len, d: `M${pts.map((p) => p.map((v) => v.toFixed(1)).join(" ")).join("L")}` };
      total += len;
      return part;
    });
    return { parts, total };
  }, [mission, geo]);

  const dots = useMemo(
    () =>
      mission.people
        .map((p) => {
          const [x, y] = geo.xy(p.geometry.coordinates);
          // Appear when the drawn track passes closest to the person (the track has no
          // per-point times, so this keeps drone and sightings in step); else by first-seen time.
          let at;
          if (track.total > 0) {
            let best = Infinity;
            for (const part of track.parts) {
              let acc = part.from;
              for (let i = 0; i < part.pts.length; i += 1) {
                if (i) acc += Math.hypot(part.pts[i][0] - part.pts[i - 1][0], part.pts[i][1] - part.pts[i - 1][1]);
                const d = Math.hypot(part.pts[i][0] - x, part.pts[i][1] - y);
                if (d < best) {
                  best = d;
                  at = acc / track.total;
                }
              }
            }
          } else {
            const t = Date.parse(p.properties.first_seen_utc);
            at = Number.isFinite(t) ? (t - mission.start) / (mission.end - mission.start) : 0;
          }
          return {
            id: p.properties.person_id,
            x,
            y,
            r: Math.max(5, (p.properties.horizontal_error_m || 3) * geo.scale),
            at: Math.min(1, Math.max(0, at)),
            conf: p.properties.confidence,
          };
        })
        .sort((a, b) => a.at - b.at),
    [mission, geo, track],
  );

  const [progress, setProgress] = useState(reduce ? 1 : 0);
  const [playing, setPlaying] = useState(!reduce);
  const raf = useRef(0);
  const last = useRef(0);

  useEffect(() => {
    if (!playing) return undefined;
    last.current = performance.now();
    const step = (now) => {
      const dt = now - last.current;
      last.current = now;
      setProgress((p) => {
        const next = p + dt / LOOP_MS;
        return next > 1.12 ? 0 : next; // hold the finished picture briefly, then loop
      });
      raf.current = requestAnimationFrame(step);
    };
    raf.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf.current);
  }, [playing]);

  const p = Math.min(1, progress);
  const drawn = p * track.total;
  let drone = null;
  for (const part of track.parts) {
    if (drawn >= part.from && drawn <= part.from + part.len && part.pts.length > 1) {
      let rest = drawn - part.from;
      for (let i = 1; i < part.pts.length; i += 1) {
        const [ax, ay] = part.pts[i - 1];
        const [bx, by] = part.pts[i];
        const l = Math.hypot(bx - ax, by - ay);
        if (rest <= l) {
          const k = l ? rest / l : 0;
          drone = [ax + (bx - ax) * k, ay + (by - ay) * k];
          break;
        }
        rest -= l;
      }
    }
  }
  const visible = dots.filter((d) => d.at <= p);
  const elapsed = (mission.end - mission.start) * p;
  const mm = String(Math.floor(elapsed / 60000)).padStart(2, "0");
  const ss = String(Math.floor((elapsed % 60000) / 1000)).padStart(2, "0");
  const bar = niceMetres(geo.scale);
  const centre = mission.people[0]?.geometry.coordinates || mission.segments[0]?.[0];

  return (
    <figure className="replay" aria-label="Mission replay: drone track and estimated people over time">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" role="img">
        <defs>
          <pattern id="grid10" width={10 * geo.scale} height={10 * geo.scale} patternUnits="userSpaceOnUse">
            <path d={`M${10 * geo.scale} 0H0V${10 * geo.scale}`} fill="none" stroke="var(--rule)" strokeWidth="1" />
          </pattern>
        </defs>
        <rect width={W} height={H} fill="url(#grid10)" />
        {/* the full planned/flown track, faint */}
        {track.parts.map((part, i) => (
          <path key={`f${i}`} d={part.d} fill="none" stroke="var(--bone-4)" strokeWidth="1" strokeDasharray="1 5" strokeLinecap="round" />
        ))}
        {/* flown so far */}
        {track.parts.map((part, i) => {
          const shown = Math.max(0, Math.min(part.len, drawn - part.from));
          return (
            <path
              key={`d${i}`}
              d={part.d}
              fill="none"
              stroke="var(--sky)"
              strokeWidth="1.6"
              strokeDasharray={`${shown} ${part.len + 1}`}
              opacity="0.85"
            />
          );
        })}
        {visible.map((d) => {
          const age = Math.min(1, (p - d.at) * 14);
          return (
            <g key={d.id} transform={`translate(${d.x.toFixed(1)} ${d.y.toFixed(1)})`}>
              <circle r={d.r} fill="var(--signal-soft)" stroke="var(--signal)" strokeWidth="1" strokeDasharray="3 4" opacity={0.25 + 0.6 * age} />
              <circle r={10 + 26 * (1 - age)} fill="none" stroke="var(--signal)" strokeWidth="1" opacity={0.7 * (1 - age)} />
              <circle r="4.5" fill="var(--signal)" stroke="var(--ink-0)" strokeWidth="2" />
            </g>
          );
        })}
        {drone && (
          <g transform={`translate(${drone[0].toFixed(1)} ${drone[1].toFixed(1)})`}>
            <circle r="34" fill="none" stroke="var(--sky)" strokeOpacity="0.35" strokeDasharray="2 4" />
            <path d="M-7 -7L7 7M7 -7L-7 7" stroke="var(--sky)" strokeWidth="2" />
            <circle r="3" fill="var(--ink-0)" stroke="var(--sky)" strokeWidth="1.5" />
          </g>
        )}
        {/* scale bar */}
        <g transform={`translate(${PAD - 30} ${H - 34})`}>
          <path d={`M0 0V6H${bar * geo.scale}V0`} fill="none" stroke="var(--bone-3)" strokeWidth="1" />
          <text x={bar * geo.scale + 10} y="7" className="replay-svgtext">{bar} m</text>
        </g>
      </svg>

      <div className="replay-hud tl">
        <span className="label">{real ? (live ? "Live mission" : "Mission replay") : "Example data"}</span>
        <span className="mono">{real ? manifest?.mission_id : "No mission connected"}</span>
      </div>
      <div className="replay-hud tr">
        <span className="label">Elapsed</span>
        <span className="mono replay-clock">T+{mm}:{ss}</span>
      </div>
      <div className="replay-hud bl">
        <span className="mono">{centre ? coord(centre[1], centre[0]) : ""}</span>
      </div>
      <div className="replay-hud br">
        <span className="replay-count mono">
          <b>{visible.length}</b>/{dots.length}
        </span>
        <span className="label">people placed</span>
      </div>

      <div className="replay-controls">
        <button type="button" onClick={() => setPlaying((v) => !v)} aria-label={playing ? "Pause replay" : "Play replay"}>
          {playing ? <Pause size={14} /> : <Play size={14} />}
        </button>
        <input
          type="range"
          min="0"
          max="1000"
          value={Math.round(p * 1000)}
          aria-label="Replay position"
          onChange={(e) => {
            setPlaying(false);
            setProgress(Number(e.target.value) / 1000);
          }}
          style={{ "--fill": `${p * 100}%` }}
        />
      </div>
      {!real && (
        <figcaption className="replay-note">
          Example flight. Connect the edge or the cloud API and this becomes your mission.
        </figcaption>
      )}
    </figure>
  );
}
