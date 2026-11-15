// Vantage UI pieces shared by the home page and mission control.
import { Fragment, useCallback, useEffect, useMemo, useRef } from "react";
import { Circle, MapContainer, Marker, Polygon, Polyline, TileLayer, Tooltip, useMap } from "react-leaflet";
import L from "leaflet";

// Where the map opens before any mission data arrives (the demo flight, Nepal).
export const HOME_CENTER = [28.2087, 85.3552];

export const BASEMAPS = {
  off: null,
  streets: {
    label: "Streets",
    url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution: "© OpenStreetMap contributors",
    className: "tiles-streets",
    maxNativeZoom: 19,
  },
  satellite: {
    label: "Satellite",
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attribution: "Imagery © Esri, Maxar, Earthstar Geographics",
    className: "tiles-satellite",
    maxNativeZoom: 18,
  },
};

export function Mark({ size = 22 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M3.5 4 12 16.5 20.5 4" stroke="currentColor" strokeWidth="2.4" strokeLinejoin="miter" />
      <path d="M2 21h20" stroke="currentColor" strokeWidth="1.2" opacity="0.45" />
      <circle cx="12" cy="21" r="2.2" fill="var(--signal)" />
    </svg>
  );
}

export function Wordmark({ href = "#/", label = "Vantage home" }) {
  return (
    <a className="wordmark" href={href} aria-label={label}>
      <Mark />
      <span>VANTAGE</span>
    </a>
  );
}

// Sequential ramp for cell density, dark to signal.
export const HEAT = ["#3a2a20", "#5e3420", "#8f3f1f", "#c94b1f", "#ff6a2e"];

const personIcon = (selected) =>
  L.divIcon({
    className: `v-person${selected ? " selected" : ""}`,
    html: "<i></i>",
    iconSize: [14, 14],
    iconAnchor: [7, 7],
  });
const ICON = personIcon(false);
const ICON_SELECTED = personIcon(true);

function Fit({ points }) {
  const map = useMap();
  const moved = useRef(false);
  const key = points.map((p) => p.join(",")).join(";");
  const fit = useCallback(() => {
    if (points.length) map.fitBounds(points, { padding: [56, 56], maxZoom: 19 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, key]);

  useEffect(() => {
    if (!moved.current) fit();
  }, [fit]);

  useEffect(() => {
    const container = map.getContainer();
    const mark = () => {
      moved.current = true;
    };
    map.on("dragstart", mark);
    container.addEventListener("wheel", mark, { passive: true });
    const observer = new ResizeObserver(() => {
      map.invalidateSize({ pan: false });
      if (!moved.current) fit();
    });
    observer.observe(container);
    return () => {
      observer.disconnect();
      map.off("dragstart", mark);
      container.removeEventListener("wheel", mark);
    };
  }, [map, fit]);
  return null;
}

function Zoom() {
  const map = useMap();
  return (
    <div className="map-zoom">
      <button type="button" aria-label="Zoom in" onClick={() => map.zoomIn()}>+</button>
      <button type="button" aria-label="Zoom out" onClick={() => map.zoomOut()}>−</button>
    </div>
  );
}

/**
 * The mission map. Positions are estimates: every person gets their error ring; cells show
 * density; the drone's own GPS track is drawn separately and never as a sighting.
 */
export function MapView({
  people,
  cells,
  flightPath,
  showCells = true,
  showPeople = true,
  showPath = true,
  basemap = "off",
  cutoffMs = null,
  selectedId = null,
  onSelect,
  interactive = true,
  className = "",
}) {
  const points = useMemo(() => {
    const result = people.map((f) => [f.geometry.coordinates[1], f.geometry.coordinates[0]]);
    if (!result.length) {
      for (const seg of flightPath?.features || []) {
        for (const [lon, lat] of seg.geometry.coordinates) result.push([lat, lon]);
      }
    }
    return result;
  }, [people, flightPath]);
  const byId = useMemo(() => Object.fromEntries(people.map((f) => [f.properties.person_id, f])), [people]);
  const old = (iso) => cutoffMs !== null && Date.parse(iso) < cutoffMs;

  return (
    <MapContainer
      className={`v-map ${className}`}
      center={HOME_CENTER}
      zoom={13}
      zoomControl={false}
      attributionControl={false}
      scrollWheelZoom={interactive}
      dragging={interactive}
      doubleClickZoom={interactive}
      keyboard={interactive}
      preferCanvas
    >
      {BASEMAPS[basemap] && (
        <TileLayer
          key={basemap}
          url={BASEMAPS[basemap].url}
          className={BASEMAPS[basemap].className}
          maxNativeZoom={BASEMAPS[basemap].maxNativeZoom}
          maxZoom={20}
        />
      )}
      <Fit points={points} />
      {interactive && <Zoom />}
      {showPath &&
        (flightPath?.features || []).map((seg, i) => (
          <Polyline
            key={`p${i}`}
            positions={seg.geometry.coordinates.map(([lon, lat]) => [lat, lon])}
            pathOptions={{ color: "#8fb6cc", weight: 1.4, opacity: 0.6, dashArray: "1 6", lineCap: "round" }}
            interactive={false}
          />
        ))}
      {showCells &&
        cells.map((f) => {
          const p = f.properties;
          const color = HEAT[Math.min(4, Math.max(0, p.count - 1))];
          const included = (p.person_ids || []).map((id) => byId[id]).filter(Boolean);
          const faded = included.length > 0 && included.every((q) => old(q.properties.last_seen_utc));
          return (
            <Polygon
              key={p.cell_id}
              positions={f.geometry.coordinates[0].map(([lon, lat]) => [lat, lon])}
              pathOptions={{
                color,
                weight: 1,
                opacity: faded ? 0.25 : 0.9,
                fillColor: color,
                fillOpacity: faded ? 0.08 : 0.38,
              }}
              eventHandlers={
                onSelect ? { click: () => onSelect({ type: "cell", properties: p, people: included }) } : {}
              }
            >
              {interactive && (
                <Tooltip sticky>
                  {p.count} estimated · cell {p.cell_id}
                </Tooltip>
              )}
            </Polygon>
          );
        })}
      {showPeople &&
        people.map((f) => {
          const p = f.properties;
          const [lon, lat] = f.geometry.coordinates;
          const faded = old(p.last_seen_utc);
          const selected = selectedId === p.person_id;
          const pick = onSelect ? { click: () => onSelect({ type: "person", properties: p, lat, lon }) } : {};
          return (
            <Fragment key={p.person_id}>
              <Circle
                center={[lat, lon]}
                radius={p.horizontal_error_m || 0}
                pathOptions={{
                  color: "#ff5b1f",
                  weight: 1,
                  dashArray: "3 4",
                  opacity: faded ? 0.18 : selected ? 0.95 : 0.5,
                  fillColor: "#ff5b1f",
                  fillOpacity: faded ? 0.02 : selected ? 0.14 : 0.05,
                }}
                eventHandlers={pick}
              />
              <Marker
                position={[lat, lon]}
                icon={selected ? ICON_SELECTED : ICON}
                opacity={faded ? 0.3 : 1}
                eventHandlers={pick}
                keyboard={false}
              >
                {interactive && (
                  <Tooltip direction="top" offset={[0, -8]}>
                    {p.person_id} · ±{p.horizontal_error_m} m · {Math.round((p.confidence || 0) * 100)}%
                  </Tooltip>
                )}
              </Marker>
            </Fragment>
          );
        })}
    </MapContainer>
  );
}
