// Shared by the landing page (Landing.jsx) and mission control (App.jsx): API helpers, the
// Leaflet mission map, and small formatting helpers.
import { Fragment, useCallback, useEffect, useMemo, useRef } from "react";
import { Circle, MapContainer, Marker, Polygon, Polyline, TileLayer, Tooltip, useMap } from "react-leaflet";
import L from "leaflet";

export const API = "/api/v1";
export const EDGE_API = import.meta.env.DEV ? "/edge-api" : null;
export const POLL_MS = 3000;
export const WINDOWS = [
  { value: 0, label: "All activity" },
  { value: 60, label: "Last minute" },
  { value: 300, label: "Last 5 minutes" },
  { value: 900, label: "Last 15 minutes" },
];
export const HEAT_COLORS = ["#9caf91", "#b4b17b", "#c99b65", "#b9785d", "#a9574e"];

export const personIcon = L.divIcon({
  className: "sighting-icon",
  html: '<span class="sighting-core"></span>',
  iconSize: [20, 20],
  iconAnchor: [10, 10],
});

export async function getJSON(path, options) {
  const response = await fetch(`${API}${path}`, options);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `${response.status} ${path}`);
  }
  return response.json();
}

export async function getEdgeJSON(path, options) {
  const response = await fetch(`${EDGE_API}${path}`, options);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `${response.status} ${path}`);
  }
  return response.json();
}

export function FitMission({ features }) {
  const map = useMap();
  const userMoved = useRef(false);
  const points = useMemo(() => {
    const result = [];
    for (const feature of features) {
      if (feature.geometry?.type === "Point") {
        const [lon, lat] = feature.geometry.coordinates;
        result.push([lat, lon]);
      }
    }
    return result;
  }, [features]);
  const fit = useCallback(() => {
    // 19 = OpenStreetMap's deepest zoom; a ~200 m search field then fills the map.
    if (points.length) map.fitBounds(points, { padding: [50, 50], maxZoom: 19 });
  }, [map, points]);

  useEffect(() => {
    fit();
  }, [fit]);

  // The map's flex container can grow after the first fit (the side panel loads later).
  // Leaflet only notices window resizes, so it kept the old size: the fit was off-centre
  // and the canvas could leave the bottom strip undrawn. Track the container itself and
  // re-fit, unless the user has already panned or zoomed.
  useEffect(() => {
    const container = map.getContainer();
    const markMoved = () => {
      userMoved.current = true;
    };
    map.on("dragstart", markMoved);
    container.addEventListener("wheel", markMoved, { passive: true });
    const observer = new ResizeObserver(() => {
      map.invalidateSize({ pan: false });
      if (!userMoved.current) fit();
    });
    observer.observe(container);
    return () => {
      observer.disconnect();
      map.off("dragstart", markMoved);
      container.removeEventListener("wheel", markMoved);
    };
  }, [map, fit]);
  return null;
}

export function ZoomControls() {
  const map = useMap();
  return (
    <div className="map-zoom">
      <button aria-label="Zoom in" onClick={() => map.zoomIn()}>+</button>
      <button aria-label="Zoom out" onClick={() => map.zoomOut()}>−</button>
    </div>
  );
}

export function HeatCell({ feature, people, onSelect, cutoffMs }) {
  const properties = feature.properties;
  const color = HEAT_COLORS[Math.min(4, Math.max(0, properties.count - 1))];
  const positions = feature.geometry.coordinates[0].map(([lon, lat]) => [lat, lon]);
  const included = (properties.person_ids || []).map((id) => people[id]).filter(Boolean);
  // Fade (never hide) a cell when every estimated person in it was last seen before the window.
  const old =
    cutoffMs !== null &&
    included.length > 0 &&
    included.every((person) => Date.parse(person.properties.last_seen_utc) < cutoffMs);

  return (
    <Polygon
      positions={positions}
      pathOptions={{
        color,
        fillColor: color,
        fillOpacity: old ? 0.06 : 0.22,
        opacity: old ? 0.3 : 1,
        weight: 1.5,
      }}
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

export function MissionMap({ data, showCells, showPeople, showOsm, windowSeconds, onSelect, flightPath }) {
  const features = data?.heatmap?.features || [];
  const people = useMemo(() => {
    const result = {};
    for (const feature of features) {
      if (feature.properties?.kind === "person") result[feature.properties.person_id] = feature;
    }
    return result;
  }, [features]);
  const end = Date.parse(data?.manifest?.data_window?.end || "") || Date.now();
  const cutoffMs = windowSeconds > 0 ? end - windowSeconds * 1000 : null;

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
      {(flightPath?.features || []).map((segment, index) => (
        <Polyline
          key={`path-${index}`}
          positions={segment.geometry.coordinates.map(([lon, lat]) => [lat, lon])}
          pathOptions={{ color: "#3f6f8f", weight: 1.5, opacity: 0.55, dashArray: "2 6" }}
          interactive={false}
        />
      ))}
      {showCells &&
        features
          .filter((feature) => feature.properties?.kind === "cell")
        .map((feature) => (
            <HeatCell
              key={feature.properties.cell_id}
              feature={feature}
              people={people}
              onSelect={onSelect}
              cutoffMs={cutoffMs}
            />
          ))}
      {showPeople &&
        Object.values(people).map((feature) => {
          const properties = feature.properties;
          const [lon, lat] = feature.geometry.coordinates;
          const old = cutoffMs !== null && Date.parse(properties.last_seen_utc) < cutoffMs;
          return (
            <Fragment key={properties.person_id}>
              <Circle
                center={[lat, lon]}
                radius={properties.horizontal_error_m || 0}
                pathOptions={{
                  color: "#59645b", // same as the legend's "Position error" ring
                  dashArray: "4 4",
                  fillColor: "#59645b",
                  fillOpacity: old ? 0.02 : 0.05,
                  opacity: old ? 0.25 : 0.7,
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


// Reports are periodic (REPORT_INTERVAL_S) or on demand, never per upload.
export function describeReport(report) {
  if (!report) return "Checking for a mission report…";
  const schedule = report.schedule || {};
  const every = schedule.interval_s ? Math.max(1, Math.round(schedule.interval_s / 60)) : null;
  if (report.status === "failed") return report.error || "The last report attempt failed.";
  if (!report.report) {
    return schedule.mode === "on_demand" || report.status === "none"
      ? "Reports are on demand: use Generate fresh summary."
      : `The next scheduled report is due within ${every} min, or generate one now.`;
  }
  const source = report.generated_by === "gemini" ? "Gemini + mission tools" : "Deterministic fallback";
  const newer = report.stale && report.latest_snapshot_version
    ? ` · newer data (v${report.latest_snapshot_version}) not summarised yet`
    : "";
  return `${source} · snapshot v${report.snapshot_version}${newer}`;
}

export function errorDetail(reason) {
  try {
    return JSON.parse(reason.message).detail || reason.message;
  } catch {
    return reason.message;
  }
}

export function formatTime(value) {
  if (!value) return "Unknown";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Unknown"
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

