# UI guide

Single-page app served from `templates/index.html`. No build step — Leaflet + vanilla JS.

## Header

- **CV pill** — cross-validated recall @ 20 m. Colour-coded: green ≥ 80%, amber ≥ 50%, red < 50%. Click to rerun.
- **Model toggle** (DBSCAN ↔ HDBSCAN) — swaps the active algorithm; everything refits.
- **↻ Try a different dataset** — dropdown with local CSVs, an **⬆ Upload CSV** row, and a link to Open Context to grab more data.
- **⤓ Export** — CSV downloads for predictions, clusters, point assignments, submissions.

## Tab 1 — Discovery Map

- **Filter bar**: category select + era window + "include submissions" checkbox. `Apply filter` refits on the subset; `Clear` resets.
- **Legend / layer control** (top-right corner of the map):
  - Known artifacts (blue dots)
  - Cluster segmentation (coloured hulls)
  - Predicted sites (red circles, top 100)
  - Forecasted new clusters (purple ★)
  - KDE density heatmap (toggle)
- **GPR Drone Survey Simulation** (bottom-left panel):
  - Speed (m/s), scan-time per site, sim speed-up factor, return-to-base toggle.
  - `Plan route` solves a nearest-neighbour TSP then runs 2-opt over predicted sites + forecasts.
  - Forecast stops scan with a radius proportional to their accuracy (`1 + 2 × score_normalized`).
  - Status panel shows stops, total distance, flight/scan/mission time, ETA.

## Tab 2 — Records Browser

Paged, sortable table of every record (lat, lon, label, category, era, cluster id, HDBSCAN probability). Click a row to fly the map to it. Column headers sort; the table updates on filter refits and dataset swaps.

## Tab 3 — Hyperparameter Tuning

- **DBSCAN**: eps (m) and min_samples sliders — live metrics (clusters, noise, silhouette).
- **HDBSCAN**: min_cluster_size and min_samples.
- **Sweep heat grid**: grid-search over a small eps × min_samples region, renders silhouette as a heatmap so you can eyeball the sweet spot.

## Tab 4 — Cluster Forecasting

Weighted KDE over cluster centroids → top-k predicted *new-cluster* locations (purple ★). Table lists rank, lat/lon, density score, distance to nearest existing centroid, nearest-cluster id. `Compute bootstrap uncertainty` draws fuzzy blobs of radius σ around each forecast so you can see how stable the pick is.

Forecasts auto-feed into the drone-survey route — the sim visits them in addition to the top-100 predicted artifacts.

## Tab 5 — Add / Classify Coordinates

Form: lat, lon, label, category, era, notes + "Refit after saving (active learning)" checkbox. Submits to `/api/add`. Response shows the classification verdict and, if refit was enabled, the new model metrics. Submissions are persisted to `submissions.csv` and can be toggled into the working set from Tab 1's filter bar.

---

## State shape (`static/js/app.js`)

```js
state = {
  data,              // cached /api/data response
  map, miniMap,      // Leaflet instances
  layers: {          // feature groups for toggleable layers
    known, hulls, preds, forecast, bootstrap, heatmap
  },
  records: { page, sortKey, sortDir, filter },
  sim: { routePts, stops, ... },  // drone simulation
  forecast: { picks: [] },
}
```

Refit triggers (`syncModelToggle`, filter apply, dataset swap, `/api/add` with refit) funnel through `reloadData()` → clears stale heatmap/forecasts/bootstrap and re-renders every layer. Dataset swap passes `{recenter: true}` → `map.flyToBounds()` on the new records.
