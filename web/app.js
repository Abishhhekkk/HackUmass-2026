// RescueSight dashboard. Same page runs on the Jetson (role=edge) and in the cloud (role=cloud).
const API = "/api/v1";
const POLL_MS = 2000;
// Sequential blue ramp (one hue, light -> dark) for estimated people per cell.
const STEPS = [
  { min: 1, color: "#9ec5f4", label: "1" },
  { min: 2, color: "#6da7ec", label: "2" },
  { min: 3, color: "#3987e5", label: "3–4" },
  { min: 5, color: "#256abf", label: "5–6" },
  { min: 7, color: "#184f95", label: "7+" },
];
const colorFor = (n) => [...STEPS].reverse().find((s) => n >= s.min).color;

const state = { role: null, mission: null, version: null, data: null, fitted: null, offline: false };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const map = L.map("map", { zoomControl: true, preferCanvas: false }).setView([42.3893, -72.5280], 17);
L.control.scale({ metric: true, imperial: false }).addTo(map);
const osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 20, maxNativeZoom: 19, attribution: "© OpenStreetMap contributors",
});
const cellLayer = L.layerGroup().addTo(map);
const peopleLayer = L.layerGroup().addTo(map);

async function getJSON(path, opts) {
  const r = await fetch(API + path, opts);
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
}

function ago(iso) {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${(s / 3600).toFixed(1)} h ago`;
}
const chip = (cls, text) => `<span class="chip ${cls}"><span class="dot"></span>${esc(text)}</span>`;

function renderStatus(st, latest) {
  const out = [];
  if (st.role === "edge") {
    const u = st.uploader;
    if (u) {
      state.offline = u.online === false;
      if (u.online === false) out.push(chip("bad", "Uplink offline — still recording locally"));
      else if (u.online === true) out.push(chip("good", "Uplink online"));
      else out.push(chip("warn", "Uplink not tried yet"));
      out.push(chip(u.pending ? "warn" : "good",
        u.pending ? `Local v${u.local_version} · cloud has v${u.acked_version} (pending)`
                  : `Cloud up to date (v${u.acked_version})`));
      if (u.last_error && u.online !== false) out.push(chip("bad", `Last upload error: ${u.last_error.slice(0, 60)}`));
      $("btn-link").textContent = state.offline ? "Restore uplink" : "Cut uplink";
    }
    const s = st.scheduler;
    out.push(chip(s.mode === "active" ? "good" : s.mode === "thermal" ? "bad" : "warn",
      `Detector: ${s.mode} (every ${s.stride === 1 ? "" : s.stride + "th "}frame)`));
    out.push(chip(st.running ? "good" : "", st.running ? `Processing · ${st.frames} frames` : "Pipeline idle"));
  } else if (latest) {
    out.push(chip("good", `Cloud copy v${latest.manifest.version} · received ${ago(latest.received_at)}`));
  }
  $("status").innerHTML = out.join("");
}

function renderSummary(m) {
  const s = m.summary;
  $("summary").innerHTML = `
    <h3>Mission ${esc(m.mission_id)} · v${m.version}</h3>
    <div class="stats">
      <div class="stat"><div class="v">${s.estimated_people}</div><div class="l">estimated people</div></div>
      <div class="stat"><div class="v">${s.unlocated_tracks}</div><div class="l">sightings without location</div></div>
      <div class="stat"><div class="v">${s.median_error_m ?? "–"} m</div><div class="l">median position error</div></div>
      <div class="stat"><div class="v">${s.p90_error_m ?? "–"} m</div><div class="l">90th pct. error</div></div>
    </div>
    <p class="muted">Drone ${esc(m.drone_id)} · data ${esc(m.data_window.start?.slice(11, 19))}–${esc(m.data_window.end?.slice(11, 19))} UTC ·
    ${s.frames_processed} frames · ${s.occupied_cells} cells of ${m.cell_size_m} m</p>
    ${s.unlocated_tracks ? `<p class="muted">Sightings without location had no valid GPS fix or attitude; they may be people already shown on the map. They are listed in the CSV.</p>` : ""}`;
  $("legend").innerHTML = `
    <h3>Heat grid: estimated people per ${m.cell_size_m} m cell</h3>
    <div class="ramp">${STEPS.map((s) => `<span style="background:${s.color}"></span>`).join("")}</div>
    <div class="ramp-l">${STEPS.map((s) => `<span>${s.label}</span>`).join("")}</div>
    <p class="muted">● estimated person · dashed circle = estimated position error. Cell score (shown on hover): ${esc(m.heat_formula)}.</p>`;
  const base = `${API}/missions/${encodeURIComponent(m.mission_id)}/files`;
  $("links").innerHTML = ["sightings.csv", "heatmap.geojson", "report.md", "manifest.json"]
    .map((f) => `<a href="${base}/${f}" download>${f}</a>`).join("");
}

function render() {
  const d = state.data;
  if (!d) return;
  const m = d.manifest;
  renderSummary(m);
  cellLayer.clearLayers();
  peopleLayer.clearLayers();
  const end = Date.parse(m.data_window.end);
  const win = Number($("window").value);
  const people = {};
  const bounds = [];
  for (const f of d.heatmap.features) {
    const p = f.properties;
    if (p.kind === "person") people[p.person_id] = f;
  }
  if ($("show-cells").checked) {
    for (const f of d.heatmap.features) {
      const p = f.properties;
      if (p.kind !== "cell") continue;
      const latlngs = f.geometry.coordinates[0].map(([lon, lat]) => [lat, lon]);
      bounds.push(...latlngs);
      L.polygon(latlngs, { color: "#ffffff", weight: 2, fillColor: colorFor(p.count), fillOpacity: 0.6 })
        .bindTooltip(`${p.count} estimated ${p.count === 1 ? "person" : "people"} · score ${p.score}`)
        .on("click", () => showCell(p, people))
        .addTo(cellLayer);
    }
  }
  for (const f of Object.values(people)) {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    bounds.push([lat, lon]);
    if (!$("show-people").checked) continue;
    const old = win > 0 && Date.parse(p.last_seen_utc) < end - win * 1000;
    const op = old ? 0.25 : 1;
    L.circle([lat, lon], { radius: p.horizontal_error_m, color: "#1d1d1b", weight: 1, dashArray: "4 4",
      fill: false, opacity: 0.6 * op }).addTo(peopleLayer);
    L.marker([lat, lon], { opacity: op, icon: L.divIcon({ className: "", html: '<div class="person-dot"></div>', iconSize: [12, 12] }) })
      .bindTooltip(`${p.person_id} · conf ${p.confidence} · ±${p.horizontal_error_m} m`)
      .on("click", () => showPerson(p, lat, lon))
      .addTo(peopleLayer);
  }
  if (bounds.length && state.fitted !== m.mission_id) {
    map.fitBounds(bounds, { padding: [30, 30] });
    state.fitted = m.mission_id;
  }
}

function showCell(p, people) {
  const rows = p.person_ids.map((id) => people[id]?.properties).filter(Boolean)
    .map((q) => `<tr><td>${q.person_id}</td><td>${q.confidence}</td><td>±${q.horizontal_error_m} m</td><td>${q.last_seen_utc.slice(11, 19)}</td></tr>`).join("");
  $("detail").innerHTML = `<h3>Cell ${esc(p.cell_id)}</h3>
    <p>${p.count} estimated ${p.count === 1 ? "person" : "people"} · score ${p.score}</p>
    <table><tr><th>ID</th><th>Conf.</th><th>Error</th><th>Last seen (UTC)</th></tr>${rows}</table>`;
}

function showPerson(p, lat, lon) {
  $("detail").innerHTML = `<h3>${esc(p.person_id)} (estimated)</h3>
    <table>
      <tr><th>Position</th><td>${lat.toFixed(6)}, ${lon.toFixed(6)}</td></tr>
      <tr><th>Est. error</th><td>±${p.horizontal_error_m} m</td></tr>
      <tr><th>Confidence</th><td>${p.confidence}</td></tr>
      <tr><th>First seen</th><td>${esc(p.first_seen_utc)}</td></tr>
      <tr><th>Last seen</th><td>${esc(p.last_seen_utc)}</td></tr>
      <tr><th>Passes merged</th><td>${p.n_tracks}</td></tr>
      <tr><th>Cell</th><td>${esc(p.cell_id)}</td></tr>
    </table>`;
}

// Minimal Markdown -> HTML for the report (headings, bullets, tables, bold, quotes).
function md(text) {
  const lines = esc(text).split("\n");
  let html = "", inList = false, inTable = false;
  const inline = (s) => s.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, "<code>$1</code>");
  for (const l of lines) {
    if (inList && !l.startsWith("- ")) { html += "</ul>"; inList = false; }
    if (inTable && !l.startsWith("|")) { html += "</table>"; inTable = false; }
    if (l.startsWith("### ")) html += `<h4>${inline(l.slice(4))}</h4>`;
    else if (l.startsWith("## ")) html += `<h3>${inline(l.slice(3))}</h3>`;
    else if (l.startsWith("# ")) html += `<h2>${inline(l.slice(2))}</h2>`;
    else if (l.startsWith("&gt; ")) html += `<blockquote>${inline(l.slice(5))}</blockquote>`;
    else if (l.startsWith("- ")) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inline(l.slice(2))}</li>`; }
    else if (l.startsWith("|")) {
      if (/^\|[-| ]+\|$/.test(l)) continue;
      if (!inTable) { html += "<table>"; inTable = true; }
      html += "<tr>" + l.slice(1, -1).split("|").map((c) => `<td>${inline(c.trim())}</td>`).join("") + "</tr>";
    } else if (l.trim()) html += `<p>${inline(l)}</p>`;
  }
  return html + (inList ? "</ul>" : "") + (inTable ? "</table>" : "");
}

async function showReport(ai) {
  const mid = encodeURIComponent(state.mission);
  $("report-body").innerHTML = "<p class='muted'>Loading…</p>";
  $("report-dlg").showModal();
  try {
    if (ai) {
      const r = await getJSON(`/missions/${mid}/ai-report`, { method: "POST" });
      const note = r.generated_by === "gemini"
        ? `<p class="muted">Written by Gemini from mission data and OpenStreetMap landmarks. Tools used: ${r.tool_calls.map((c) => c.tool).join(", ")}.</p>`
        : `<p class="muted">Gemini unavailable (${esc(r.fallback_reason)}); showing the built-in report.</p>`;
      $("report-body").innerHTML = note + md(r.report);
    } else {
      const r = await fetch(`${API}/missions/${mid}/report`);
      $("report-body").innerHTML = md(await r.text());
    }
  } catch (e) {
    $("report-body").innerHTML = `<p>Could not load report: ${esc(e.message)}</p>`;
  }
}

async function refresh() {
  try {
    const [st, missions] = await Promise.all([getJSON("/status"), getJSON("/missions")]);
    if (state.role !== st.role) {
      state.role = st.role;
      $("role").textContent = st.role === "edge" ? "on drone / ground station" : "cloud";
      $("demo").hidden = st.role !== "edge" || !st.uploader;
      $("btn-ai").hidden = st.role !== "cloud";
    }
    const sel = $("mission");
    const ids = missions.map((m) => m.mission_id);
    if (sel.options.length !== ids.length || [...sel.options].some((o, i) => o.value !== ids[i])) {
      sel.innerHTML = missions.map((m) => `<option value="${esc(m.mission_id)}">${esc(m.mission_id)}</option>`).join("");
    }
    if (!state.mission || !ids.includes(state.mission)) state.mission = st.mission_id && ids.includes(st.mission_id) ? st.mission_id : ids[0];
    sel.value = state.mission ?? "";
    let latest = null;
    if (state.mission) {
      latest = await getJSON(`/missions/${encodeURIComponent(state.mission)}/latest`);
      if (latest.manifest.version !== state.version || latest.manifest.mission_id !== state.data?.manifest.mission_id) {
        state.data = latest;
        state.version = latest.manifest.version;
        render();
      }
    } else {
      $("summary").innerHTML = "<p class='muted'>No snapshots yet. Waiting for the first one…</p>";
    }
    renderStatus(st, latest);
  } catch (e) {
    $("status").innerHTML = chip("bad", `Dashboard cannot reach its API: ${e.message}`);
  }
}

$("mission").addEventListener("change", (e) => { state.mission = e.target.value; state.version = null; refresh(); });
for (const id of ["window", "show-cells", "show-people"]) $(id).addEventListener("change", render);
$("show-osm").addEventListener("change", (e) => (e.target.checked ? osm.addTo(map) : map.removeLayer(osm)));
$("btn-link").addEventListener("click", async () => {
  await getJSON("/network", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ offline: !state.offline }) });
  refresh();
});
$("btn-corrupt").addEventListener("click", () => getJSON("/demo/corrupt-next-upload", { method: "POST" }));
$("btn-report").addEventListener("click", () => showReport(false));
$("btn-ai").addEventListener("click", () => showReport(true));
refresh();
setInterval(refresh, POLL_MS);
