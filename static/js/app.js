/* Giza Artifact Discovery — frontend
 * Uses Leaflet + fetch, no build step.
 */

const TILE_OSM = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_SAT = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const ATTR_OSM = "© OpenStreetMap contributors";
const ATTR_SAT = "Tiles © Esri — Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP";

const state = {
  map: null,
  miniMap: null,
  layers: {
    known: L.layerGroup(),
    hulls: L.layerGroup(),
    preds: L.layerGroup(),
    forecast: L.layerGroup(),
    bootstrap: L.layerGroup(),
    heatmap: null,
    userPin: null,
  },
  forecast: { picks: [] },
  knownMarkers: [],   // same order as data.records
  data: null,
  records: {
    sortKey: "idx",
    sortDir: "asc",
    page: 0,
    pageSize: 100,
    filtered: [],
  },
};

// ---------- tabs ----------
function initTabs() {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById(btn.dataset.tab).classList.add("active");
      if (btn.dataset.tab === "map-tab" && state.map) {
        setTimeout(() => state.map.invalidateSize(), 80);
      }
    });
  });
}

// ---------- main map ----------
function initMap(data) {
  const satellite = L.tileLayer(TILE_SAT, { attribution: ATTR_SAT, maxZoom: 21 });
  const osm = L.tileLayer(TILE_OSM, { attribution: ATTR_OSM, maxZoom: 19 });

  // Compute tight bounds around cluster centroids to drop the user into Giza
  const lats = data.clusters.map(c => c.centroid[0]);
  const lons = data.clusters.map(c => c.centroid[1]);
  const bounds = [
    [Math.min(...lats) - 0.0008, Math.min(...lons) - 0.0008],
    [Math.max(...lats) + 0.0008, Math.max(...lons) + 0.0008],
  ];

  state.map = L.map("map", {
    layers: [satellite],
    zoomControl: true,
    maxZoom: 21,
  }).fitBounds(bounds);

  L.control.layers(
    { "Satellite (Esri)": satellite, "OpenStreetMap": osm },
    {
      "Known artifacts": state.layers.known,
      "Cluster segmentation": state.layers.hulls,
      "Predicted sites": state.layers.preds,
      "Forecasted new clusters": state.layers.forecast,
    },
    { collapsed: true, position: "topleft" }
  ).addTo(state.map);

  L.control.scale({ imperial: false }).addTo(state.map);

  drawKnown(data);
  drawHulls(data);
  drawPreds(data);

  state.layers.known.addTo(state.map);
  state.layers.hulls.addTo(state.map);
  state.layers.preds.addTo(state.map);
  state.layers.forecast.addTo(state.map);
  state.layers.bootstrap.addTo(state.map);

  wireLegendToggles();
  buildClusterList(data);
}

function colorForCluster(id, data) {
  const c = data.clusters.find(x => x.id === id);
  return c ? c.color : "#888";
}

// Short hover preview (no thumbnail, small).
function buildRecordTooltipHtml(r) {
  const clusterTxt = r.cluster === -1
    ? '<span style="color:#9aa7b8">noise / outlier</span>'
    : `Cluster <b>${r.cluster}</b>`;
  const dates = (r.early || r.late)
    ? `<br><span style="color:#9aa7b8">Dates: ${escapeHtml(r.early || "—")} → ${escapeHtml(r.late || "—")}</span>`
    : "";
  const ctx = r.context_path
    ? `<br><span style="color:#9aa7b8">${escapeHtml(r.context_path)}</span>`
    : "";
  return (
    `<b>${escapeHtml(r.label)}</b><br>` +
    `<span style="color:#9aa7b8">${escapeHtml(r.category || "—")}</span> · ${clusterTxt}` +
    dates + ctx +
    `<br><span style="color:#9aa7b8">${fmtCoord(r.lat, r.lon)}</span>`
  );
}

// Full popup with thumbnail + links + all JSON metadata.
function buildRecordPopupHtml(r) {
  const thumb = r.thumbnail
    ? `<img class="popup-thumb" src="${escapeHtml(r.thumbnail)}"
            onerror="this.style.display='none'" alt="" />`
    : "";
  const clusterBadge = r.cluster === -1
    ? `<span class="meta-pill noise">noise</span>`
    : `<span class="meta-pill">Cluster ${r.cluster}</span>`;
  const row = (k, v) =>
    v ? `<div class="m-row"><span class="m-k">${k}</span><span class="m-v">${v}</span></div>` : "";
  const link = (label, url) =>
    url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${label} ↗</a>` : "";

  return (
    `<div class="popup-card">` +
      thumb +
      `<div class="popup-body">` +
        `<div class="popup-title">${escapeHtml(r.label)}</div>` +
        `<div class="popup-sub">${escapeHtml(r.category || "—")} · ${clusterBadge}</div>` +
        row("Context", escapeHtml(r.context_path || "—")) +
        row("Project", escapeHtml(r.project || "—")) +
        row("Dates",
            (r.early || r.late) ? `${escapeHtml(r.early || "?")} → ${escapeHtml(r.late || "?")}` : "—") +
        row("Coordinates", fmtCoord(r.lat, r.lon)) +
        row("Published", escapeHtml(r.published || "—")) +
        row("Updated",   escapeHtml(r.updated || "—")) +
        `<div class="m-links">` +
          link("Open Context", r.uri) + " " +
          link("Citation", r.citation_uri) + " " +
          link("Project page", r.project_uri) + " " +
          link("Context page", r.context_uri) +
        `</div>` +
      `</div>` +
    `</div>`
  );
}

function drawKnown(data) {
  const g = state.layers.known;
  g.clearLayers();
  state.knownMarkers = [];
  data.records.forEach((r, idx) => {
    const color = r.cluster === -1 ? "#8a8f99" : colorForCluster(r.cluster, data);
    const tooltip = buildRecordTooltipHtml(r);
    const popup = buildRecordPopupHtml(r);
    const m = L.circleMarker([r.lat, r.lon], {
      radius: 3,
      color: color,
      weight: 1,
      fillColor: color,
      fillOpacity: 0.75,
    })
      .bindTooltip(tooltip, {
        direction: "top",
        offset: [0, -4],
        className: "pred-tooltip",
        sticky: true,
      })
      .bindPopup(popup, { maxWidth: 360, minWidth: 280 });
    m.addTo(g);
    state.knownMarkers[idx] = m;
  });
}

function drawHulls(data) {
  const g = state.layers.hulls;
  g.clearLayers();
  data.clusters.forEach(c => {
    if (c.hull && c.hull.length >= 3) {
      const poly = L.polygon(c.hull, {
        color: c.color,
        weight: 2,
        fillColor: c.color,
        fillOpacity: 0.22,
        dashArray: "4 3",
      })
        .bindTooltip(`Cluster ${c.id} · ${c.count} items`,
                     { sticky: true, className: "pred-tooltip" })
        .bindPopup(
          `<b>Cluster ${c.id}</b><br>` +
          `Items: ${c.count}<br>` +
          `Centroid: ${fmtCoord(c.centroid[0], c.centroid[1])}`
        );
      poly.addTo(g);
    } else {
      L.circleMarker(c.centroid, {
        radius: 8, color: c.color, fillColor: c.color, fillOpacity: 0.4, weight: 2
      }).bindTooltip(`Cluster ${c.id} · ${c.count} items`,
                     { sticky: true, className: "pred-tooltip" }).addTo(g);
    }
  });
}

function drawPreds(data) {
  const g = state.layers.preds;
  g.clearLayers();
  data.predictions.forEach(p => {
    const icon = L.divIcon({ className: "pred-marker", iconSize: [18, 18] });
    const marker = L.marker([p.lat, p.lon], { icon });
    const pctTxt = fmtPercentile(p.score_percentile);
    const confBar = scoreBar(p.score_normalized);
    const tooltip =
      `<b>Predicted #${p.rank}</b><br>` +
      `${fmtCoord(p.lat, p.lon)}<br>` +
      `Nearest cluster: ${p.nearest_cluster_id} (${p.nearest_cluster_distance_m} m)<br>` +
      `Confidence: <b>${fmtScore(p.score)}</b>` +
      (pctTxt ? ` <span style="color:#d4a53a">· ${pctTxt}</span>` : "") +
      `${confBar}`;
    marker.bindTooltip(tooltip, {
      direction: "top",
      offset: [0, -8],
      className: "pred-tooltip",
      sticky: false,
    });
    marker.bindPopup(tooltip);
    marker.addTo(g);
  });
}

function wireLegendToggles() {
  const map = state.map;
  const bind = (id, layer) => {
    document.getElementById(id).addEventListener("change", (e) => {
      if (e.target.checked) layer.addTo(map); else map.removeLayer(layer);
    });
  };
  bind("toggle-known", state.layers.known);
  bind("toggle-hulls", state.layers.hulls);
  bind("toggle-preds", state.layers.preds);
  bind("toggle-forecast", state.layers.forecast);
}

// ---------- cluster forecasting ----------
function drawForecast(picks) {
  const g = state.layers.forecast;
  g.clearLayers();
  picks.forEach((p) => {
    const acc = Math.max(0, Math.min(1, p.score_normalized || 0));
    const radiusM = 6 + 30 * acc;                 // coverage footprint in metres
    const iconSize = Math.round(28 + 28 * acc);   // 28 .. 56 px
    L.circle([p.lat, p.lon], {
      radius: radiusM,
      color: "#b46bff",
      weight: 1.5,
      opacity: 0.8,
      fillColor: "#b46bff",
      fillOpacity: 0.16,
      dashArray: "4 4",
      interactive: false,
    }).addTo(g);
    const icon = L.divIcon({
      className: "forecast-marker",
      html: `<div class="fc-star" style="font-size:${Math.round(iconSize * 0.85)}px">★</div><div class="fc-rank">${p.rank}</div>`,
      iconSize: [iconSize, iconSize],
      iconAnchor: [iconSize / 2, iconSize / 2],
    });
    const m = L.marker([p.lat, p.lon], { icon });
    const html =
      `<div class="popup-card"><div class="popup-body">` +
        `<div class="popup-title">Forecasted cluster #${p.rank}</div>` +
        `<div class="popup-sub">Weighted-KDE peak · not near any existing cluster</div>` +
        `<div class="m-row"><span class="m-k">Coordinates</span><span class="m-v">${fmtCoord(p.lat, p.lon)}</span></div>` +
        `<div class="m-row"><span class="m-k">Score (norm.)</span><span class="m-v">${(p.score_normalized * 100).toFixed(1)}%</span></div>` +
        `<div class="m-row"><span class="m-k">Dist. to nearest</span><span class="m-v">${p.dist_to_nearest_m.toFixed(1)} m</span></div>` +
        `<div class="m-row"><span class="m-k">Nearest cluster</span><span class="m-v">#${p.nearest_cluster_id}</span></div>` +
        `<div class="m-row"><span class="m-k">Size hint</span><span class="m-v">~${p.predicted_size_hint}</span></div>` +
      `</div></div>`;
    m.bindTooltip(
      `<b>Forecast #${p.rank}</b><br>` +
      `<span style="color:#9aa7b8">${fmtCoord(p.lat, p.lon)}</span><br>` +
      `Score ${(p.score_normalized * 100).toFixed(1)}% · ${p.dist_to_nearest_m.toFixed(0)} m from nearest`,
      { direction: "top", offset: [0, -14] }
    );
    m.bindPopup(html, { maxWidth: 360 });
    m.addTo(g);
  });
}

function renderForecastTable(picks) {
  const tbody = document.querySelector("#forecast-table tbody");
  tbody.innerHTML = "";
  if (!picks.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="muted">No forecasts meet the constraints. Try increasing expansion or decreasing min distance.</td></tr>`;
    return;
  }
  picks.forEach((p) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${p.rank}</td>` +
      `<td>${p.lat.toFixed(6)}</td>` +
      `<td>${p.lon.toFixed(6)}</td>` +
      `<td>${(p.score_normalized * 100).toFixed(1)}%</td>` +
      `<td>${p.dist_to_nearest_m.toFixed(1)}</td>` +
      `<td>#${p.nearest_cluster_id}</td>` +
      `<td>~${p.predicted_size_hint}</td>` +
      `<td><button type="button" class="link-btn">Fly to</button></td>`;
    tr.querySelector("button").addEventListener("click", () => {
      document.querySelector('.tab[data-tab="map-tab"]').click();
      setTimeout(() => state.map.flyTo([p.lat, p.lon], 20, { duration: 0.6 }), 120);
    });
    tbody.appendChild(tr);
  });
}

function initForecast() {
  const btn = document.getElementById("btn-forecast");
  const status = document.getElementById("forecast-status");
  btn.addEventListener("click", async () => {
    const body = {
      expansion: parseFloat(document.getElementById("fc-expansion").value),
      top_k: parseInt(document.getElementById("fc-topk").value, 10),
      min_dist_m: parseFloat(document.getElementById("fc-mindist").value),
      bw_scale: parseFloat(document.getElementById("fc-bw").value),
      nms_m: parseFloat(document.getElementById("fc-nms").value),
    };
    status.textContent = "Computing…";
    btn.disabled = true;
    try {
      const res = await fetch("/api/predict_clusters", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const json = await res.json();
      if (!res.ok) { status.textContent = json.error || "Error"; return; }
      state.forecast.picks = json.predictions;
      renderForecastTable(json.predictions);
      drawForecast(json.predictions);
      // Always replan so the drone picks up the new forecasts, even if a prior sim was running/paused.
      resetSim();
      planAndDraw();
      status.textContent = `Found ${json.predictions.length} forecast peaks from ${json.n_source_clusters} clusters. Simulation replanned.`;
    } catch (err) {
      status.textContent = "Request failed: " + err.message;
    } finally {
      btn.disabled = false;
    }
  });
}

function buildClusterList(data) {
  const host = document.getElementById("cluster-list");
  host.innerHTML = "";
  data.clusters
    .slice()
    .sort((a, b) => b.count - a.count)
    .forEach(c => {
      const row = document.createElement("div");
      row.className = "c-row";
      row.innerHTML = `<span class="dot" style="background:${c.color}"></span>
                       Cluster ${c.id} · ${c.count}`;
      row.addEventListener("click", () => {
        state.map.flyTo(c.centroid, 20, { duration: 0.6 });
      });
      host.appendChild(row);
    });
}

// ---------- stats ----------
function renderStats(data) {
  const s = data.stats;
  document.getElementById("stats").innerHTML = `
    <span class="chip"><b>${s.records.toLocaleString()}</b> records</span>
    <span class="chip"><b>${s.clusters}</b> clusters</span>
    <span class="chip"><b>${s.noise}</b> noise</span>
    <span class="chip"><b>${s.predictions}</b> predicted sites</span>
  `;
}

// ---------- records tab ----------
function initRecords(data) {
  const clusterSel = document.getElementById("records-cluster-filter");
  data.clusters
    .slice().sort((a, b) => a.id - b.id)
    .forEach(c => {
      const opt = document.createElement("option");
      opt.value = String(c.id);
      opt.textContent = `Cluster ${c.id} (${c.count})`;
      clusterSel.appendChild(opt);
    });
  const noiseOpt = document.createElement("option");
  noiseOpt.value = "-1";
  noiseOpt.textContent = "Noise / outliers";
  clusterSel.appendChild(noiseOpt);

  document.getElementById("records-search")
    .addEventListener("input", () => { state.records.page = 0; refreshTable(); });
  clusterSel.addEventListener("change",
    () => { state.records.page = 0; refreshTable(); });

  document.querySelectorAll("#records-table thead th[data-sort]").forEach(th => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (state.records.sortKey === key) {
        state.records.sortDir = state.records.sortDir === "asc" ? "desc" : "asc";
      } else {
        state.records.sortKey = key;
        state.records.sortDir = "asc";
      }
      refreshTable();
    });
  });

  document.getElementById("pg-prev").addEventListener("click", () => {
    if (state.records.page > 0) { state.records.page--; renderTable(); }
  });
  document.getElementById("pg-next").addEventListener("click", () => {
    const total = state.records.filtered.length;
    const maxPage = Math.max(0, Math.ceil(total / state.records.pageSize) - 1);
    if (state.records.page < maxPage) { state.records.page++; renderTable(); }
  });

  refreshTable();
}

function refreshTable() {
  const q = (document.getElementById("records-search").value || "")
    .trim().toLowerCase();
  const clusterFilter = document.getElementById("records-cluster-filter").value;
  const all = state.data.records.map((r, i) => ({ ...r, idx: i }));

  let filtered = all;
  if (clusterFilter !== "") {
    const want = parseInt(clusterFilter, 10);
    filtered = filtered.filter(r => r.cluster === want);
  }
  if (q) {
    filtered = filtered.filter(r =>
      (r.label || "").toLowerCase().includes(q) ||
      (r.category || "").toLowerCase().includes(q) ||
      String(r.cluster).includes(q) ||
      String(r.early || "").toLowerCase().includes(q) ||
      String(r.late || "").toLowerCase().includes(q)
    );
  }

  const { sortKey, sortDir } = state.records;
  const dir = sortDir === "asc" ? 1 : -1;
  filtered.sort((a, b) => {
    const va = a[sortKey], vb = b[sortKey];
    if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
    return String(va ?? "").localeCompare(String(vb ?? "")) * dir;
  });

  state.records.filtered = filtered;
  renderTable();
}

function renderTable() {
  const { filtered, page, pageSize, sortKey, sortDir } = state.records;
  const tbody = document.querySelector("#records-table tbody");
  tbody.innerHTML = "";

  const start = page * pageSize;
  const slice = filtered.slice(start, start + pageSize);

  slice.forEach(r => {
    const color = r.cluster === -1
      ? "#8a8f99"
      : (state.data.clusters.find(c => c.id === r.cluster)?.color || "#888");
    const clusterPill =
      r.cluster === -1
        ? `<span class="cluster-pill noise">noise</span>`
        : `<span class="cluster-pill"><span class="dot" style="background:${color}"></span>${r.cluster}</span>`;

    const thumb = r.thumbnail
      ? `<img class="row-thumb" src="${escapeHtml(r.thumbnail)}"
              onerror="this.style.display='none'" alt="" loading="lazy"/>`
      : `<span class="row-thumb empty">—</span>`;
    const ctx = r.context_path || "—";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${r.idx + 1}</td>
      <td>${thumb}</td>
      <td class="label-cell" title="${escapeHtml(r.label)}">${escapeHtml(r.label)}</td>
      <td>${escapeHtml(r.category || "—")}</td>
      <td>${clusterPill}</td>
      <td>${r.probability != null ? probabilityBar(r.probability) : '<span class="muted tiny">—</span>'}</td>
      <td class="label-cell" title="${escapeHtml(ctx)}">${escapeHtml(ctx)}</td>
      <td>${escapeHtml(r.early || "—")}</td>
      <td>${escapeHtml(r.late || "—")}</td>
      <td>${r.lat.toFixed(6)}</td>
      <td>${r.lon.toFixed(6)}</td>
      <td><button class="view-btn" type="button">View on map</button></td>
    `;
    tr.addEventListener("click", () => focusOnRecord(r.idx));
    tbody.appendChild(tr);
  });

  document.getElementById("records-count").textContent =
    `${filtered.length.toLocaleString()} records`;
  const total = filtered.length;
  const maxPage = Math.max(0, Math.ceil(total / pageSize) - 1);
  document.getElementById("pg-info").textContent =
    `${page + 1} / ${maxPage + 1}`;
  document.getElementById("pg-prev").disabled = page === 0;
  document.getElementById("pg-next").disabled = page >= maxPage;

  document.querySelectorAll("#records-table thead th").forEach(th => {
    th.classList.remove("sort-asc", "sort-desc");
    if (th.dataset.sort === sortKey) {
      th.classList.add(sortDir === "asc" ? "sort-asc" : "sort-desc");
    }
  });
}

function focusOnRecord(idx) {
  const r = state.data.records[idx];
  if (!r) return;
  activateTab("map-tab");
  if (!state.layers.known._map) state.layers.known.addTo(state.map);
  document.getElementById("toggle-known").checked = true;
  setTimeout(() => {
    state.map.flyTo([r.lat, r.lon], 21, { duration: 0.6 });
    const marker = state.knownMarkers[idx];
    if (marker) setTimeout(() => marker.openPopup(), 650);
  }, 120);
}

function activateTab(id) {
  document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
  document.querySelector(`.tab[data-tab="${id}"]`).classList.add("active");
  document.getElementById(id).classList.add("active");
  if (id === "map-tab" && state.map) {
    setTimeout(() => state.map.invalidateSize(), 80);
  }
}

// ---------- form ----------
const DUMMY_SAMPLES = [
  { lat: 29.975263, lon: 31.137906, label: "Demo sherd A-01",
    category: "Pottery", early: "-2500", late: "-2200",
    notes: "Top-1 prediction (high-potential discovery zone)" },
  { lat: 29.975382, lon: 31.137273, label: "Demo fragment B-07",
    category: "Small Find", early: "-2400", late: "-2100",
    notes: "Near Sphinx Ditch — high-score candidate" },
  { lat: 29.9753, lon: 31.1379, label: "Demo drawing D-99",
    category: "Drawing", early: "-2500", late: "-2300",
    notes: "Plausible zone just off an existing cluster" },
  { lat: 29.9790, lon: 31.1400, label: "Demo outlier X-01",
    category: "Small Find", early: "-2000", late: "-1800",
    notes: "Deliberately far from surveyed area — should be LOW-POTENTIAL" },
];
let dummyIdx = 0;

function fillDummy(form) {
  const s = DUMMY_SAMPLES[dummyIdx % DUMMY_SAMPLES.length];
  dummyIdx++;
  form.elements["lat"].value = s.lat;
  form.elements["lon"].value = s.lon;
  form.elements["label"].value = s.label;
  form.elements["category"].value = s.category;
  form.elements["early"].value = s.early;
  form.elements["late"].value = s.late;
  form.elements["notes"].value = s.notes;
  document.getElementById("auto-cluster").value = "";
  document.getElementById("btn-classify").click();
}

function initForm() {
  const form = document.getElementById("add-form");
  const btnClassify = document.getElementById("btn-classify");
  const btnDummy = document.getElementById("btn-dummy");
  if (btnDummy) btnDummy.addEventListener("click", () => fillDummy(form));

  btnClassify.addEventListener("click", async () => {
    const fd = readForm(form);
    if (fd.lat == null || fd.lon == null) return;
    const res = await fetch("/api/classify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lat: fd.lat, lon: fd.lon }),
    });
    const r = await res.json();
    if (r.error) { alert(r.error); return; }
    showResult(r);
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = readForm(form);
    if (fd.lat == null || fd.lon == null) return;
    const refit = document.getElementById("chk-refit")?.checked || false;
    const res = await fetch("/api/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...fd, refit }),
    });
    const r = await res.json();
    if (r.error) { alert(r.error); return; }
    showResult(r.classification);
    if (refit && r.refit && !r.refit.error) {
      flashBanner(`Saved + model refit: ${r.refit.n_clusters} clusters, silhouette=${(r.refit.silhouette ?? 0).toFixed?.(3) || "n/a"}`);
      await reloadData();
      if (typeof refreshCurrentParams === "function") refreshCurrentParams();
      runCV();
    } else {
      flashBanner("Saved to submissions.csv");
    }
  });
}

function readForm(form) {
  const fd = new FormData(form);
  const obj = {};
  for (const [k, v] of fd.entries()) obj[k] = v;
  const lat = parseFloat(obj.lat);
  const lon = parseFloat(obj.lon);
  obj.lat = Number.isFinite(lat) ? lat : null;
  obj.lon = Number.isFinite(lon) ? lon : null;
  return obj;
}

function showResult(r) {
  document.getElementById("result-empty").hidden = true;
  document.getElementById("result").hidden = false;

  const v = document.getElementById("verdict");
  v.textContent = r.verdict;
  v.className = `verdict ${r.verdict_class}`;

  const set = (id, val) => (document.getElementById(id).textContent = val);
  set("r-assigned",
      r.assigned_cluster === -1
        ? "noise / outlier"
        : `Cluster ${r.assigned_cluster}`);
  set("r-near-id", `Cluster ${r.nearest_cluster_id}`);
  set("r-near-dist", `${r.nearest_cluster_distance_m} m`);
  set("r-known-dist", `${r.nearest_known_distance_m} m`);
  set("r-score", r.kde_score.toExponential(4));
  set("r-pct", `${r.kde_percentile}%`);
  set("r-thresh", r.top20_threshold.toExponential(3));

  document.getElementById("auto-cluster").value =
    r.assigned_cluster === -1
      ? `noise (nearest: ${r.nearest_cluster_id}, ${r.nearest_cluster_distance_m} m)`
      : `Cluster ${r.assigned_cluster}`;

  drawMiniMap(r);
}

function drawMiniMap(r) {
  const host = document.getElementById("mini-map");
  if (!state.miniMap) {
    state.miniMap = L.map(host, {
      zoomControl: false, attributionControl: false,
    }).setView([r.lat, r.lon], 20);
    L.tileLayer(TILE_SAT, { maxZoom: 21 }).addTo(state.miniMap);
  }
  state.miniMap.eachLayer(l => {
    if (l instanceof L.Marker || l instanceof L.Polygon || l instanceof L.CircleMarker) {
      state.miniMap.removeLayer(l);
    }
  });
  state.miniMap.setView([r.lat, r.lon], 20);

  // Show nearby cluster hulls + predicted sites around the point.
  if (state.data) {
    state.data.clusters.forEach(c => {
      if (c.hull && c.hull.length >= 3) {
        L.polygon(c.hull, {
          color: c.color, weight: 1.5, fillColor: c.color,
          fillOpacity: 0.18, dashArray: "3 3",
        }).addTo(state.miniMap);
      }
    });
    state.data.predictions.forEach(p => {
      L.circleMarker([p.lat, p.lon], {
        radius: 5, color: "#ef5f5f", fillColor: "#ef5f5f", fillOpacity: 0.9, weight: 1,
      }).bindTooltip(`Predicted #${p.rank}`, { className: "pred-tooltip" })
        .addTo(state.miniMap);
    });
  }

  L.marker([r.lat, r.lon], {
    icon: L.divIcon({ className: "user-marker", iconSize: [16, 16] }),
  }).bindPopup(`<b>${r.verdict}</b><br>${fmtCoord(r.lat, r.lon)}`)
    .addTo(state.miniMap)
    .openPopup();

  setTimeout(() => state.miniMap.invalidateSize(), 60);
}

// ---------- utils ----------
function fmtCoord(lat, lon) {
  return `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
}

function fmtScore(score) {
  if (!Number.isFinite(score)) return "—";
  const abs = Math.abs(score);
  if (abs >= 1e9) return (score / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return (score / 1e6).toFixed(2) + "M";
  if (abs >= 1e3) return (score / 1e3).toFixed(2) + "K";
  return score.toFixed(2);
}

function scoreBar(norm) {
  if (norm == null || !Number.isFinite(norm)) return "";
  const pct = Math.max(0, Math.min(1, norm)) * 100;
  return (
    `<div style="margin-top:6px;height:6px;background:#2f3845;border-radius:3px;overflow:hidden">` +
    `<div style="width:${pct.toFixed(1)}%;height:100%;` +
    `background:linear-gradient(90deg,#ffdd7a,#ef5f5f)"></div></div>`
  );
}

function probabilityBar(p) {
  const pct = Math.max(0, Math.min(1, p)) * 100;
  return (
    `<div class="prob-cell" title="soft membership ${pct.toFixed(1)}%">` +
    `<div class="prob-bar"><div style="width:${pct.toFixed(0)}%"></div></div>` +
    `<span>${pct.toFixed(0)}%</span></div>`
  );
}

function fmtPercentile(pct) {
  if (pct == null || !Number.isFinite(pct)) return "";
  const top = Math.max(0, 100 - pct);
  if (top < 1) return `top ${top.toFixed(2)}%`;
  return `top ${top.toFixed(1)}%`;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, m =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[m]
  );
}
function flashBanner(msg) {
  const b = document.createElement("div");
  b.textContent = msg;
  Object.assign(b.style, {
    position: "fixed", bottom: "24px", left: "50%", transform: "translateX(-50%)",
    background: "rgba(65, 201, 126, 0.95)", color: "#072514",
    padding: "0.6rem 1rem", borderRadius: "8px", fontWeight: "700",
    zIndex: 9999, boxShadow: "0 6px 20px rgba(0,0,0,0.4)",
  });
  document.body.appendChild(b);
  setTimeout(() => b.remove(), 2200);
}

// ---------- tuning tab ----------
function initTuning() {
  const epsInput = document.getElementById("eps-input");
  const msInput = document.getElementById("ms-input");
  const epsVal = document.getElementById("eps-val");
  const msVal = document.getElementById("ms-val");
  epsInput.addEventListener("input", () => (epsVal.textContent = epsInput.value));
  msInput.addEventListener("input", () => (msVal.textContent = msInput.value));

  document.getElementById("tune-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await applyParams(parseFloat(epsInput.value), parseInt(msInput.value, 10));
  });

  document.getElementById("btn-sweep")
    .addEventListener("click", runSweep);

  refreshCurrentParams();
}

function syncTuneUI(modelName) {
  const epsLbl = document.querySelector('label[for="eps-input"]')
    || document.getElementById("eps-input")?.closest("label");
  const epsInput = document.getElementById("eps-input");
  const epsValSpan = document.getElementById("eps-val");
  const msLbl = document.getElementById("ms-input")?.closest("label");
  const msInput = document.getElementById("ms-input");
  if (!epsInput || !msInput) return;
  if (modelName === "hdbscan") {
    if (epsLbl) epsLbl.childNodes[0].nodeValue = "min_cluster_size ";
    epsInput.min = 2; epsInput.max = 80; epsInput.step = 1;
    if (parseFloat(epsInput.value) < 2) { epsInput.value = 5; epsValSpan.textContent = "5"; }
    if (msLbl) msLbl.childNodes[0].nodeValue = "min_samples ";
    msInput.min = 1; msInput.max = 30; msInput.step = 1;
  } else {
    if (epsLbl) epsLbl.childNodes[0].nodeValue = "ε (metres) ";
    epsInput.min = 1; epsInput.max = 30; epsInput.step = 0.5;
    if (msLbl) msLbl.childNodes[0].nodeValue = "min_samples ";
    msInput.min = 2; msInput.max = 30; msInput.step = 1;
  }
}

async function refreshCurrentParams() {
  const res = await fetch("/api/current_params");
  const p = await res.json();
  const sil = p.silhouette == null ? "n/a" : p.silhouette.toFixed(4);
  const modelName = (p.model || "dbscan").toLowerCase();
  syncTuneUI(modelName);
  const headline = modelName === "hdbscan"
    ? `Current (HDBSCAN): min_cluster_size=${p.min_cluster_size} · min_samples=${p.min_samples}`
    : `Current (DBSCAN): ε=${p.eps_m} m · min_samples=${p.min_samples}`;
  document.getElementById("tune-current").textContent =
    `${headline} · ${p.n_clusters} clusters · ${p.noise} noise · silhouette=${sil}`;
  const primary = modelName === "hdbscan" ? p.min_cluster_size : p.eps_m;
  document.getElementById("eps-input").value = primary;
  document.getElementById("eps-val").textContent = primary;
  document.getElementById("ms-input").value = p.min_samples;
  document.getElementById("ms-val").textContent = p.min_samples;
}

async function applyParams(eps_m, min_samples) {
  const status = document.getElementById("apply-status");
  const modelName = (state.data?.model?.name || "dbscan").toLowerCase();
  status.textContent = "Re-clustering…";
  const body = modelName === "hdbscan"
    ? { model: "hdbscan", min_cluster_size: eps_m, min_samples }
    : { model: "dbscan", eps_m, min_samples };
  const res = await fetch("/api/recluster", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const r = await res.json();
  if (r.error) {
    status.textContent = `Error: ${r.error}`;
    return;
  }
  status.textContent =
    `Done: ${r.n_clusters} clusters · ${r.noise} noise · ` +
    `silhouette=${r.silhouette == null ? "n/a" : r.silhouette.toFixed(4)} · ` +
    `${r.predictions_found} predictions`;
  await reloadData();
  refreshCurrentParams();
}

async function reloadData(opts = {}) {
  const res = await fetch("/api/data");
  const data = await res.json();
  state.data = data;
  if (opts.recenter && state.map && data.records?.length) {
    const lats = data.records.map(r => r.lat);
    const lons = data.records.map(r => r.lon);
    const bounds = [[Math.min(...lats), Math.min(...lons)],
                    [Math.max(...lats), Math.max(...lons)]];
    state.map.flyToBounds(bounds, { padding: [40, 40], duration: 1.2, maxZoom: 19 });
  }
  syncModelToggle(data);
  renderStats(data);
  drawKnown(data);
  drawHulls(data);
  drawPreds(data);
  buildClusterList(data);
  rebuildClusterFilter(data);
  if (typeof populateFilterUI === "function") populateFilterUI(data);
  state.records.page = 0;
  refreshTable();
  if (state.forecast) state.forecast.picks = [];
  if (state.layers.forecast) state.layers.forecast.clearLayers();
  if (state.layers.bootstrap) state.layers.bootstrap.clearLayers();
  const fcTbody = document.querySelector("#forecast-table tbody");
  if (fcTbody) fcTbody.innerHTML = "";
  // Heatmap data is stale after a refit.
  if (state.layers.heatmap) {
    state.map.removeLayer(state.layers.heatmap);
    state.layers.heatmap = null;
    if (document.getElementById("toggle-heatmap")?.checked) await loadHeatmap().then(() => {
      if (state.layers.heatmap) state.layers.heatmap.addTo(state.map);
    });
  }
}

function syncModelToggle(data) {
  const name = (data?.model?.name || "dbscan").toLowerCase();
  document.querySelectorAll("#model-toggle .mt-opt").forEach((b) => {
    b.classList.toggle("active", b.dataset.model === name);
  });
  // Refresh the tuning-tab UI to match the active model.
  if (typeof syncTuneUI === "function") syncTuneUI(name);
}

function initModelToggle() {
  const host = document.getElementById("model-toggle");
  if (!host) return;
  host.querySelectorAll(".mt-opt").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const target = btn.dataset.model;
      if (btn.classList.contains("active")) return;
      host.querySelectorAll(".mt-opt").forEach((b) => (b.disabled = true));
      try {
        const res = await fetch("/api/switch_model", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ model: target }),
        });
        const json = await res.json();
        if (!res.ok || json.error) {
          alert("Switch failed: " + (json.error || res.statusText));
          return;
        }
        await reloadData();
        if (typeof refreshCurrentParams === "function") refreshCurrentParams();
        // Reset simulation so the drone replans against the new clustering.
        if (typeof resetSim === "function") resetSim();
      } finally {
        host.querySelectorAll(".mt-opt").forEach((b) => (b.disabled = false));
      }
    });
  });
}

function rebuildClusterFilter(data) {
  const sel = document.getElementById("records-cluster-filter");
  const prev = sel.value;
  sel.innerHTML = '<option value="">All clusters</option>';
  data.clusters
    .slice().sort((a, b) => a.id - b.id)
    .forEach(c => {
      const opt = document.createElement("option");
      opt.value = String(c.id);
      opt.textContent = `Cluster ${c.id} (${c.count})`;
      sel.appendChild(opt);
    });
  const noiseOpt = document.createElement("option");
  noiseOpt.value = "-1";
  noiseOpt.textContent = "Noise / outliers";
  sel.appendChild(noiseOpt);
  if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
}

const sweepState = { rows: [], sortKey: "silhouette", sortDir: "desc" };

function range(a, b, step) {
  const out = [];
  for (let v = a; v <= b + 1e-9; v += step) out.push(+v.toFixed(4));
  return out;
}

async function runSweep() {
  const epsMin = parseFloat(document.getElementById("sw-eps-min").value);
  const epsMax = parseFloat(document.getElementById("sw-eps-max").value);
  const epsStep = parseFloat(document.getElementById("sw-eps-step").value);
  const msMin = parseInt(document.getElementById("sw-ms-min").value, 10);
  const msMax = parseInt(document.getElementById("sw-ms-max").value, 10);
  const msStep = parseInt(document.getElementById("sw-ms-step").value, 10);
  const epsVals = range(epsMin, epsMax, epsStep);
  const msVals = range(msMin, msMax, msStep);

  const status = document.getElementById("sweep-status");
  const total = epsVals.length * msVals.length;
  if (total > 400) {
    status.textContent = `Too many combinations (${total}). Limit is 400.`;
    return;
  }
  status.textContent = `Running ${total} combinations…`;
  const t0 = performance.now();
  const res = await fetch("/api/sweep", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ eps_values: epsVals, min_samples_values: msVals }),
  });
  const r = await res.json();
  if (r.error) { status.textContent = `Error: ${r.error}`; return; }
  const elapsed = ((performance.now() - t0) / 1000).toFixed(2);
  status.textContent = `${r.count} combinations in ${elapsed}s`;

  sweepState.rows = r.results;
  renderSweep();

  document.querySelectorAll("#sweep-table thead th[data-sort]").forEach(th => {
    th.onclick = () => {
      const key = th.dataset.sort;
      if (sweepState.sortKey === key) {
        sweepState.sortDir = sweepState.sortDir === "asc" ? "desc" : "asc";
      } else {
        sweepState.sortKey = key;
        sweepState.sortDir = key === "silhouette" ? "desc" : "asc";
      }
      renderSweep();
    };
  });
}

function renderSweep() {
  const tbody = document.querySelector("#sweep-table tbody");
  tbody.innerHTML = "";
  const { sortKey, sortDir } = sweepState;
  const dir = sortDir === "asc" ? 1 : -1;
  const rows = sweepState.rows.slice().sort((a, b) => {
    const va = a[sortKey] ?? -Infinity, vb = b[sortKey] ?? -Infinity;
    if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
    return String(va).localeCompare(String(vb)) * dir;
  });

  const bestSil = rows.reduce((best, r) =>
    (r.silhouette != null && (best == null || r.silhouette > best.silhouette)) ? r : best,
    null);

  rows.forEach(r => {
    const tr = document.createElement("tr");
    if (bestSil && r.eps_m === bestSil.eps_m && r.min_samples === bestSil.min_samples) {
      tr.classList.add("best");
    }
    tr.innerHTML = `
      <td>${r.eps_m}</td>
      <td>${r.min_samples}</td>
      <td>${r.n_clusters}</td>
      <td>${r.noise}</td>
      <td>${r.noise_pct}%</td>
      <td>${r.silhouette == null ? "—" : r.silhouette.toFixed(4)}</td>
      <td>${r.mean_cluster_size}</td>
      <td>${r.largest_cluster}</td>
      <td><button class="view-btn" type="button">Apply</button></td>
    `;
    tr.addEventListener("click", () => applyParams(r.eps_m, r.min_samples));
    tbody.appendChild(tr);
  });

  document.querySelectorAll("#sweep-table thead th").forEach(th => {
    th.classList.remove("sort-asc", "sort-desc");
    if (th.dataset.sort === sortKey) {
      th.classList.add(sortDir === "asc" ? "sort-asc" : "sort-desc");
    }
  });
}

// ---------- GPR drone simulation ----------
const sim = {
  pts: [],             // ordered stops: {lat, lon, label, base, rank}
  segDist: [],         // meters per segment (length = pts.length - 1)
  total: 0,            // total route meters
  routeLayer: null,
  stopsLayer: null,
  droneMarker: null,
  running: false,
  rafId: null,
  simTime: 0,          // seconds of simulated mission elapsed
  leg: 0,              // current segment index
  phase: "idle",       // idle | flying | scanning | done
  phaseRemaining: 0,   // seconds left in current phase
  lastFrameMs: null,
  speed: 5,
  scanTime: 30,
  mult: 30,
  totalFly: 0,
  totalScan: 0,
};

function haversineM(lat1, lon1, lat2, lon2) {
  const R = 6_371_000;
  const toR = (d) => (d * Math.PI) / 180;
  const dLat = toR(lat2 - lat1);
  const dLon = toR(lon2 - lon1);
  const a = Math.sin(dLat/2)**2 +
            Math.cos(toR(lat1)) * Math.cos(toR(lat2)) * Math.sin(dLon/2)**2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

function fmtDuration(s) {
  if (!Number.isFinite(s)) return "—";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${m}m ${sec}s`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
}

function fmtMeters(m) {
  if (!Number.isFinite(m)) return "—";
  if (m >= 1000) return (m / 1000).toFixed(2) + " km";
  return m.toFixed(1) + " m";
}

// Build an ordered route through base + predictions using nearest-neighbor + 2-opt.
function planRoute(returnToBase) {
  const preds = (state.data?.predictions || []).slice();
  const forecasts = (state.forecast?.picks || []).slice();
  if (preds.length === 0 && forecasts.length === 0) return null;

  const predStops = preds.map(p => ({
    lat: p.lat, lon: p.lon, rank: p.rank,
    cluster: p.nearest_cluster_id, score: p.score,
    kind: "predicted",
    label: `#${p.rank}`,
    scanScale: 1,
  }));
  const forecastStops = forecasts.map(f => ({
    lat: f.lat, lon: f.lon, rank: f.rank,
    cluster: f.nearest_cluster_id, score: f.score,
    scoreNorm: f.score_normalized,
    kind: "forecast",
    label: `F${f.rank}`,
    // Higher accuracy = larger footprint = longer scan (1× .. 3×).
    scanScale: 1 + 2 * Math.max(0, Math.min(1, f.score_normalized || 0)),
  }));
  const targets = predStops.concat(forecastStops);

  const baseLat = targets.reduce((s, p) => s + p.lat, 0) / targets.length;
  const baseLon = targets.reduce((s, p) => s + p.lon, 0) / targets.length;
  const pts = [{ lat: baseLat, lon: baseLon, label: "Base", base: true, scanScale: 0 }]
    .concat(targets);

  const n = pts.length;
  const d = (i, j) => haversineM(pts[i].lat, pts[i].lon, pts[j].lat, pts[j].lon);

  // Nearest-neighbor tour starting at base (index 0).
  let order = [0];
  const rest = new Set();
  for (let i = 1; i < n; i++) rest.add(i);
  while (rest.size) {
    const last = order[order.length - 1];
    let best = -1, bestD = Infinity;
    for (const i of rest) {
      const dd = d(last, i);
      if (dd < bestD) { bestD = dd; best = i; }
    }
    order.push(best); rest.delete(best);
  }
  if (returnToBase) order.push(0);

  // 2-opt improvement.
  const routeCost = (ord) => {
    let t = 0;
    for (let k = 0; k < ord.length - 1; k++) t += d(ord[k], ord[k+1]);
    return t;
  };
  let improved = true, guard = 0;
  while (improved && guard++ < 50) {
    improved = false;
    for (let i = 1; i < order.length - 2; i++) {
      for (let j = i + 1; j < order.length - 1; j++) {
        const a = order[i-1], b = order[i], c = order[j], e = order[j+1];
        const oldD = d(a, b) + d(c, e);
        const newD = d(a, c) + d(b, e);
        if (newD + 1e-9 < oldD) {
          order = order.slice(0, i)
            .concat(order.slice(i, j+1).reverse())
            .concat(order.slice(j+1));
          improved = true;
        }
      }
    }
  }

  const orderedPts = order.map(i => pts[i]);
  const segDist = [];
  let total = 0;
  for (let k = 0; k < orderedPts.length - 1; k++) {
    const s = haversineM(
      orderedPts[k].lat, orderedPts[k].lon,
      orderedPts[k+1].lat, orderedPts[k+1].lon
    );
    segDist.push(s); total += s;
  }
  return { pts: orderedPts, segDist, total };
}

function drawRoute() {
  if (sim.routeLayer) state.map.removeLayer(sim.routeLayer);
  if (sim.stopsLayer) state.map.removeLayer(sim.stopsLayer);
  sim.routeLayer = L.layerGroup().addTo(state.map);
  sim.stopsLayer = L.layerGroup().addTo(state.map);

  const latlngs = sim.pts.map(p => [p.lat, p.lon]);
  L.polyline(latlngs, {
    color: "#d4a53a",
    weight: 3,
    opacity: 0.85,
    dashArray: "6 6",
  }).addTo(sim.routeLayer);

  sim.pts.forEach((p, i) => {
    if (i === sim.pts.length - 1 && p.base && i !== 0) return;

    // Forecast stops get an accuracy-sized coverage circle underneath.
    if (p.kind === "forecast") {
      const radiusM = 6 + 30 * (p.scoreNorm || 0);   // 6 m .. 36 m
      L.circle([p.lat, p.lon], {
        radius: radiusM,
        color: "#b46bff",
        weight: 1.5,
        opacity: 0.9,
        fillColor: "#b46bff",
        fillOpacity: 0.18,
        dashArray: "4 4",
        interactive: false,
      }).addTo(sim.routeLayer);
    }

    const classes = ["route-stop-num"];
    if (p.base) classes.push("base");
    else if (p.kind === "forecast") classes.push("forecast");
    const icon = L.divIcon({
      className: "",
      html: `<div class="${classes.join(" ")}" data-stop="${i}">${p.base ? "B" : i}</div>`,
      iconSize: [22, 22],
      iconAnchor: [11, 11],
    });
    let tip;
    if (p.base) {
      tip = `<b>Base</b><br>${fmtCoord(p.lat, p.lon)}`;
    } else if (p.kind === "forecast") {
      const acc = ((p.scoreNorm || 0) * 100).toFixed(1);
      const scanS = Math.round(sim.scanTime * p.scanScale);
      tip =
        `<b>Stop ${i} · Forecast #${p.rank}</b><br>` +
        `New cluster · nearest #${p.cluster}<br>` +
        `Accuracy: ${acc}% · scan ${scanS}s<br>` +
        `${fmtCoord(p.lat, p.lon)}`;
    } else {
      tip =
        `<b>Stop ${i} · Predicted #${p.rank}</b><br>` +
        `Cluster: ${p.cluster}<br>` +
        `Score: ${fmtScore(p.score)}<br>` +
        `${fmtCoord(p.lat, p.lon)}`;
    }
    L.marker([p.lat, p.lon], { icon })
      .bindTooltip(tip, { className: "pred-tooltip", direction: "top", offset: [0, -8] })
      .addTo(sim.stopsLayer);
  });
}

function updateStops() {
  // Mark stops already visited as "done".
  document.querySelectorAll(".route-stop-num").forEach(el => {
    const stopIdx = parseInt(el.dataset.stop, 10);
    const done = stopIdx <= sim.leg && sim.phase !== "idle";
    el.classList.toggle("done", done && stopIdx !== 0);
  });
}

function computeTotals() {
  sim.totalFly = sim.total / sim.speed;
  // Scans happen at every non-base stop. Forecast stops scale by scanScale (accuracy footprint).
  sim.totalScan = sim.pts.slice(1)
    .filter(p => !p.base)
    .reduce((s, p) => s + sim.scanTime * (p.scanScale ?? 1), 0);
}

function renderStats() {
  const setTxt = (id, v) => (document.getElementById(id).textContent = v);
  const nonBase = sim.pts.filter(p => !p.base);
  const nPred = nonBase.filter(p => p.kind !== "forecast").length;
  const nFc   = nonBase.filter(p => p.kind === "forecast").length;
  const parts = [];
  if (nPred) parts.push(`${nPred} predicted`);
  if (nFc)   parts.push(`${nFc} forecast`);
  setTxt("s-stops", parts.join(" + ") || "—");
  setTxt("s-dist", fmtMeters(sim.total));
  setTxt("s-fly",  fmtDuration(sim.totalFly));
  setTxt("s-scan", fmtDuration(sim.totalScan));
  setTxt("s-total", fmtDuration(sim.totalFly + sim.totalScan));
  setTxt("s-status", sim.phase);
  setTxt("s-elapsed", fmtDuration(sim.simTime));
  const remaining = (sim.totalFly + sim.totalScan) - sim.simTime;
  setTxt("s-eta", fmtDuration(remaining));
  const pct = (sim.totalFly + sim.totalScan) > 0
    ? Math.min(100, (sim.simTime / (sim.totalFly + sim.totalScan)) * 100)
    : 0;
  document.getElementById("sim-prog-bar").style.width = pct + "%";
}

function placeDrone(lat, lon) {
  if (!sim.droneMarker) {
    const icon = L.divIcon({
      className: "",
      html: `<div class="drone-marker" id="drone-gfx">🛸</div>`,
      iconSize: [28, 28],
      iconAnchor: [14, 14],
    });
    sim.droneMarker = L.marker([lat, lon], { icon, interactive: false, zIndexOffset: 1000 }).addTo(state.map);
  } else {
    sim.droneMarker.setLatLng([lat, lon]);
  }
}

function setDroneScanning(scanning) {
  const el = document.getElementById("drone-gfx");
  if (!el) return;
  el.classList.toggle("drone-scanning", scanning);
  if (scanning && !el.querySelector(".ring")) {
    const r = document.createElement("div");
    r.className = "ring";
    el.appendChild(r);
  } else if (!scanning) {
    el.querySelectorAll(".ring").forEach(x => x.remove());
  }
}

function readSimInputs() {
  sim.speed = Math.max(0.1, parseFloat(document.getElementById("sim-speed").value) || 5);
  sim.scanTime = Math.max(0, parseFloat(document.getElementById("sim-scan").value) || 30);
  sim.mult = Math.max(1, parseFloat(document.getElementById("sim-mult").value) || 30);
}

function planAndDraw() {
  readSimInputs();
  const ret = document.getElementById("sim-return").checked;
  const plan = planRoute(ret);
  if (!plan) return;
  sim.pts = plan.pts;
  sim.segDist = plan.segDist;
  sim.total = plan.total;
  sim.leg = 0;
  sim.phase = "idle";
  sim.simTime = 0;
  sim.phaseRemaining = 0;
  drawRoute();
  placeDrone(sim.pts[0].lat, sim.pts[0].lon);
  setDroneScanning(false);
  computeTotals();
  renderStats();
}

function startSim() {
  // Always rebuild the plan from the latest predictions + forecasts when starting
  // from idle or after completion (so the drone always visits the newest forecasts).
  if (sim.phase === "idle" || sim.phase === "done" || !sim.pts.length) {
    if (sim.phase === "done") resetSim();
    planAndDraw();
  }
  if (!sim.pts.length) return;

  readSimInputs();
  computeTotals();

  if (sim.phase === "idle") {
    sim.leg = 0;
    sim.phase = sim.segDist.length ? "flying" : "done";
    sim.phaseRemaining = sim.segDist[0] / sim.speed;
    sim.simTime = 0;
  }

  sim.running = true;
  sim.lastFrameMs = performance.now();
  document.getElementById("btn-start").disabled = true;
  document.getElementById("btn-pause").disabled = false;
  document.getElementById("btn-plan").disabled = true;
  tickSim();
}

function pauseSim() {
  sim.running = false;
  cancelAnimationFrame(sim.rafId);
  document.getElementById("btn-start").disabled = false;
  document.getElementById("btn-pause").disabled = true;
  document.getElementById("btn-plan").disabled = false;
  document.getElementById("btn-start").textContent = "▶ Resume";
}

function resetSim() {
  sim.running = false;
  cancelAnimationFrame(sim.rafId);
  sim.phase = "idle";
  sim.simTime = 0;
  sim.leg = 0;
  sim.phaseRemaining = 0;
  if (sim.droneMarker) { state.map.removeLayer(sim.droneMarker); sim.droneMarker = null; }
  if (sim.routeLayer) { state.map.removeLayer(sim.routeLayer); sim.routeLayer = null; }
  if (sim.stopsLayer) { state.map.removeLayer(sim.stopsLayer); sim.stopsLayer = null; }
  sim.pts = []; sim.segDist = []; sim.total = 0;
  document.getElementById("btn-start").disabled = false;
  document.getElementById("btn-pause").disabled = true;
  document.getElementById("btn-plan").disabled = false;
  document.getElementById("btn-start").textContent = "▶ Start simulation";
  ["s-stops","s-dist","s-fly","s-scan","s-total","s-elapsed","s-eta"]
    .forEach(id => document.getElementById(id).textContent = "—");
  document.getElementById("s-status").textContent = "idle";
  document.getElementById("sim-prog-bar").style.width = "0%";
}

function tickSim() {
  if (!sim.running) return;
  const now = performance.now();
  const realDt = (now - sim.lastFrameMs) / 1000;
  sim.lastFrameMs = now;
  let simDt = realDt * sim.mult;          // simulated seconds this frame

  while (simDt > 0 && sim.phase !== "done") {
    const step = Math.min(simDt, sim.phaseRemaining);
    sim.phaseRemaining -= step;
    sim.simTime += step;
    simDt -= step;

    // Interpolate drone position while flying.
    if (sim.phase === "flying") {
      const from = sim.pts[sim.leg];
      const to   = sim.pts[sim.leg + 1];
      const segTime = sim.segDist[sim.leg] / sim.speed;
      const t = segTime > 0 ? 1 - sim.phaseRemaining / segTime : 1;
      const lat = from.lat + (to.lat - from.lat) * t;
      const lon = from.lon + (to.lon - from.lon) * t;
      placeDrone(lat, lon);
    }

    if (sim.phaseRemaining <= 1e-6) {
      // Transition.
      if (sim.phase === "flying") {
        sim.leg += 1;
        const arrived = sim.pts[sim.leg];
        placeDrone(arrived.lat, arrived.lon);
        if (!arrived.base && sim.scanTime > 0) {
          sim.phase = "scanning";
          sim.phaseRemaining = sim.scanTime * (arrived.scanScale ?? 1);
          setDroneScanning(true);
        } else if (sim.leg >= sim.segDist.length) {
          sim.phase = "done";
        } else {
          sim.phase = "flying";
          sim.phaseRemaining = sim.segDist[sim.leg] / sim.speed;
        }
      } else if (sim.phase === "scanning") {
        setDroneScanning(false);
        if (sim.leg >= sim.segDist.length) {
          sim.phase = "done";
        } else {
          sim.phase = "flying";
          sim.phaseRemaining = sim.segDist[sim.leg] / sim.speed;
        }
      }
    }
  }

  updateStops();
  renderStats();

  if (sim.phase === "done") {
    sim.running = false;
    setDroneScanning(false);
    document.getElementById("btn-start").disabled = false;
    document.getElementById("btn-pause").disabled = true;
    document.getElementById("btn-plan").disabled = false;
    document.getElementById("btn-start").textContent = "▶ Start simulation";
    document.getElementById("s-status").textContent = "done ✓";
    return;
  }
  sim.rafId = requestAnimationFrame(tickSim);
}

function initSim() {
  document.getElementById("btn-plan").addEventListener("click", planAndDraw);
  document.getElementById("btn-start").addEventListener("click", startSim);
  document.getElementById("btn-pause").addEventListener("click", pauseSim);
  document.getElementById("btn-reset").addEventListener("click", resetSim);
  document.getElementById("sim-collapse").addEventListener("click", () => {
    document.getElementById("sim-panel").classList.toggle("collapsed");
  });
  ["sim-speed", "sim-scan", "sim-mult", "sim-return"].forEach(id => {
    document.getElementById(id).addEventListener("change", () => {
      if (sim.pts.length && sim.phase === "idle") planAndDraw();
      else if (sim.pts.length) { readSimInputs(); computeTotals(); renderStats(); }
    });
  });
}

// ---------- heatmap ----------
async function loadHeatmap() {
  if (typeof L.heatLayer !== "function") return;
  try {
    const res = await fetch("/api/heatmap");
    const json = await res.json();
    if (state.layers.heatmap) {
      state.map.removeLayer(state.layers.heatmap);
      state.layers.heatmap = null;
    }
    if (!json.points?.length) return;
    state.layers.heatmap = L.heatLayer(json.points, {
      radius: 24, blur: 18, maxZoom: 21, minOpacity: 0.35,
      gradient: { 0.2: "#4fb4ff", 0.5: "#ffe119", 0.8: "#f5803e", 1.0: "#ef5f5f" },
    });
  } catch (err) { console.warn("heatmap failed", err); }
}

function wireHeatmapToggle() {
  const cb = document.getElementById("toggle-heatmap");
  if (!cb) return;
  cb.addEventListener("change", async (e) => {
    if (e.target.checked) {
      if (!state.layers.heatmap) await loadHeatmap();
      if (state.layers.heatmap) state.layers.heatmap.addTo(state.map);
    } else if (state.layers.heatmap) {
      state.map.removeLayer(state.layers.heatmap);
    }
  });
}

// ---------- export menu ----------
function wireExportMenu() {
  const btn = document.getElementById("export-toggle");
  const dd  = document.getElementById("export-dropdown");
  if (!btn || !dd) return;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    dd.hidden = !dd.hidden;
  });
  document.addEventListener("click", () => { dd.hidden = true; });
}

// ---------- dataset switcher ----------
async function wireDatasetMenu() {
  const btn = document.getElementById("dataset-toggle");
  const dd = document.getElementById("dataset-dropdown");
  const list = document.getElementById("dataset-list");
  if (!btn || !dd || !list) return;
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    const show = dd.hidden;
    dd.hidden = !dd.hidden;
    if (show) await populateDatasetList(list);
  });
  document.addEventListener("click", () => { dd.hidden = true; });
  dd.addEventListener("click", (e) => e.stopPropagation());
  const fileInput = document.getElementById("dataset-file");
  fileInput?.addEventListener("change", async (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    await uploadDataset(f);
    e.target.value = "";
  });
}

async function uploadDataset(file) {
  const btn = document.getElementById("dataset-toggle");
  const prev = btn.textContent;
  btn.textContent = `Uploading ${file.name}…`;
  btn.disabled = true;
  try {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/upload_dataset", { method: "POST", body: fd });
    const j = await res.json();
    if (!res.ok || j.error) { alert(j.error || "Upload failed"); return; }
    await reloadData({ recenter: true });
    setTimeout(runCV, 50);
  } catch (e) {
    alert("Upload failed: " + e);
  } finally {
    btn.textContent = prev;
    btn.disabled = false;
    document.getElementById("dataset-dropdown").hidden = true;
  }
}

async function populateDatasetList(list) {
  list.innerHTML = '<span class="muted">Loading…</span>';
  try {
    const res = await fetch("/api/datasets");
    const j = await res.json();
    list.innerHTML = "";
    (j.datasets || []).forEach((d) => {
      const a = document.createElement("a");
      a.href = "#";
      a.className = "ds-item" + (d.active ? " active" : "");
      const rows = d.rows != null ? `${d.rows.toLocaleString()} rows` : "";
      a.innerHTML = `<span class="ds-name">${d.name}</span><span class="ds-rows muted">${rows}${d.active ? " · active" : ""}</span>`;
      a.addEventListener("click", async (ev) => {
        ev.preventDefault();
        if (d.active) return;
        await loadDataset(d.path, d.name);
      });
      list.appendChild(a);
    });
    const ext = document.getElementById("dataset-external");
    if (ext && j.opencontext_url) ext.href = j.opencontext_url;
  } catch {
    list.innerHTML = '<span class="muted">Failed to load</span>';
  }
}

async function loadDataset(path, name) {
  const btn = document.getElementById("dataset-toggle");
  const prev = btn.textContent;
  btn.textContent = `Loading ${name}…`;
  btn.disabled = true;
  try {
    const res = await fetch("/api/load_dataset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    const j = await res.json();
    if (!res.ok || j.error) { alert(j.error || "Load failed"); return; }
    await reloadData({ recenter: true });
    setTimeout(runCV, 50);
  } catch (e) {
    alert("Load failed: " + e);
  } finally {
    btn.textContent = prev;
    btn.disabled = false;
    document.getElementById("dataset-dropdown").hidden = true;
  }
}

// ---------- CV recall pill ----------
async function runCV() {
  const pill = document.getElementById("cv-pill");
  const val = document.getElementById("cv-value");
  if (!pill || !val) return;
  val.textContent = "…";
  pill.classList.remove("good", "warn", "bad");
  try {
    const res = await fetch("/api/cv_score", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ holdout_pct: 10, folds: 3, radius_m: 20 }),
    });
    const j = await res.json();
    if (!res.ok || j.error) { val.textContent = "err"; return; }
    val.textContent = `${j.recall_mean}% ±${j.recall_std}`;
    pill.title = `CV recall @ 20m over ${j.folds} folds (model=${j.model})`;
    pill.classList.add(j.recall_mean >= 80 ? "good" : j.recall_mean >= 50 ? "warn" : "bad");
  } catch { val.textContent = "err"; }
}

function wireCVPill() {
  const pill = document.getElementById("cv-pill");
  if (pill) pill.addEventListener("click", runCV);
}

// ---------- category + era filters ----------
function populateFilterUI(data) {
  const sel = document.getElementById("filter-category");
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">All</option>';
  (data.categories || []).forEach((c) => {
    const o = document.createElement("option");
    o.value = c; o.textContent = c;
    sel.appendChild(o);
  });
  sel.value = current;
  const early = document.getElementById("filter-early");
  const late  = document.getElementById("filter-late");
  if (early && late && data.era_bounds && !early.value && !late.value) {
    early.placeholder = `early ≥ ${data.era_bounds[0]}`;
    late.placeholder  = `late ≤ ${data.era_bounds[1]}`;
  }
  const filt = data.filter || {};
  if (filt.category) sel.value = filt.category;
  if (filt.early_min != null && early) early.value = filt.early_min;
  if (filt.late_max  != null && late)  late.value  = filt.late_max;
  const chk = document.getElementById("filter-include-subs");
  if (chk) chk.checked = !!filt.include_submissions;
}

function wireFilterBar() {
  const apply = document.getElementById("btn-apply-filter");
  const clear = document.getElementById("btn-clear-filter");
  const status = document.getElementById("filter-status");
  if (!apply || !clear) return;
  async function send(body) {
    status.textContent = "Refitting…";
    apply.disabled = clear.disabled = true;
    try {
      const res = await fetch("/api/refit_filter", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const j = await res.json();
      if (!res.ok || j.error) { status.textContent = "Error: " + (j.error || "failed"); return; }
      status.textContent = `${j.n_clusters} clusters · ${j.noise} noise · ${j.filter?.n_records || "?"} rows`;
      await reloadData();
      if (typeof refreshCurrentParams === "function") refreshCurrentParams();
      runCV();
    } finally {
      apply.disabled = clear.disabled = false;
    }
  }
  apply.addEventListener("click", () => {
    const cat = document.getElementById("filter-category").value;
    const e = document.getElementById("filter-early").value;
    const l = document.getElementById("filter-late").value;
    const subs = document.getElementById("filter-include-subs").checked;
    send({
      category: cat || null,
      early_min: e === "" ? null : parseFloat(e),
      late_max:  l === "" ? null : parseFloat(l),
      include_submissions: subs,
    });
  });
  clear.addEventListener("click", () => {
    document.getElementById("filter-category").value = "";
    document.getElementById("filter-early").value = "";
    document.getElementById("filter-late").value = "";
    document.getElementById("filter-include-subs").checked = false;
    send({ category: null, early_min: null, late_max: null, include_submissions: false });
  });
}

// ---------- bootstrap uncertainty blobs ----------
async function runBootstrap() {
  const btn = document.getElementById("btn-bootstrap");
  const status = document.getElementById("forecast-status");
  if (!state.forecast?.picks?.length) {
    status.textContent = "Run the forecast first.";
    return;
  }
  status.textContent = "Bootstrapping…";
  btn.disabled = true;
  try {
    const res = await fetch("/api/bootstrap_forecast", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        B: 30,
        expansion: parseFloat(document.getElementById("fc-expansion").value) || 3,
        bw_scale: parseFloat(document.getElementById("fc-bw").value) || 1.2,
        peaks: state.forecast.picks.map((p) => ({ rank: p.rank, lat: p.lat, lon: p.lon })),
      }),
    });
    const j = await res.json();
    if (!res.ok || j.error) { status.textContent = "Bootstrap error: " + (j.error || "failed"); return; }
    drawBootstrapBlobs(j.peaks);
    status.textContent = `Bootstrap done (B=${j.B}). Fuzzy blob = ±1σ displacement.`;
  } finally { btn.disabled = false; }
}

function drawBootstrapBlobs(peaks) {
  const g = state.layers.bootstrap;
  g.clearLayers();
  peaks.forEach((pk) => {
    if (!pk.sigma_m || pk.sigma_m <= 0) return;
    const pick = state.forecast.picks.find((p) => p.rank === pk.rank);
    if (!pick) return;
    L.circle([pick.lat, pick.lon], {
      radius: pk.sigma_m,
      color: "#ffd166",
      weight: 1,
      opacity: 0.7,
      fillColor: "#ffd166",
      fillOpacity: 0.12,
      dashArray: "3 6",
      interactive: false,
    }).addTo(g);
    L.circle([pick.lat, pick.lon], {
      radius: pk.sigma_m * 2,
      color: "#ffd166",
      weight: 0.7,
      opacity: 0.35,
      fillColor: "#ffd166",
      fillOpacity: 0.04,
      interactive: false,
    }).addTo(g);
  });
}

// ---------- boot ----------
async function boot() {
  initTabs();
  initForm();
  const res = await fetch("/api/data");
  const data = await res.json();
  state.data = data;
  renderStats(data);
  initMap(data);
  initRecords(data);
  initTuning();
  initSim();
  initForecast();
  initModelToggle();
  syncModelToggle(data);
  populateFilterUI(data);
  wireFilterBar();
  wireHeatmapToggle();
  wireExportMenu();
  wireDatasetMenu();
  wireCVPill();
  document.getElementById("btn-bootstrap")?.addEventListener("click", runBootstrap);
  runCV();
}
boot();
