# HTTP API

All endpoints are defined in `app.py`. Responses are JSON unless noted. Errors return `{"error": "..."}` with a 4xx status.

## Page

### `GET /`
Renders `templates/index.html`.

---

## Read

### `GET /api/data`
Everything the map needs in one payload.

```jsonc
{
  "center": [lat, lon],
  "clusters": [{ "id": 0, "count": 317, "centroid": [lat, lon], "hull": [...], "persistence": 0.83? }],
  "records":  [{ "lat", "lon", "label", "category", "early", "late", "uri", ..., "cluster": 0, "probability": 0.91 }],
  "predictions": [{ "rank": 1, "lat", "lon", "score", "score_normalized", "score_percentile", "nearest_cluster_id" }],
  "stats":   { "records", "clusters", "noise", "predictions" },
  "model":   { "name", "eps_m", "min_cluster_size", "min_samples", "silhouette", "has_soft_membership" },
  "filter":  { "category", "early_min", "late_max", "include_submissions", "n_records" },
  "categories": ["Area", "Feature", ...],
  "era_bounds": [min_early, max_late]
}
```

### `GET /api/current_params`
Small snapshot used by the Hyperparameter Tuning tab to sync sliders on load.

### `GET /api/heatmap`
Down-sampled density grid for `leaflet.heat`. `{points: [[lat, lon, norm], ...], max}`.

### `GET /api/datasets`
Lists local CSVs: the current dataset plus everything in `other datasets/`. `{datasets: [{path, name, rows, active}], opencontext_url}`.

### `GET /api/export/<which>`
CSV download. `which` ∈ `predictions | clusters | assignments | submissions`.

---

## Classify / add

### `POST /api/classify`
```json
{ "lat": 29.9748, "lon": 31.1380 }
```
Returns verdict, assigned cluster, nearest-cluster distance, KDE score + percentile.

### `POST /api/add`
```json
{ "lat", "lon", "label", "category", "early", "late", "notes", "refit": true }
```
Appends to `submissions.csv`. If `refit: true`, triggers active learning: re-reads all submissions and refits the current model with them included.

---

## Cluster / refit

### `POST /api/recluster`
DBSCAN body: `{ "model": "dbscan", "eps_m": 5, "min_samples": 5 }`
HDBSCAN body: `{ "model": "hdbscan", "min_cluster_size": 5, "min_samples": 3 }`
Refits with validation (eps ∈ [0.1, 500] m, samples ∈ [2, 100]).

### `POST /api/switch_model`
`{ "model": "dbscan" | "hdbscan" }` — refits with sensible defaults, returns metrics. The header toggle calls this; the client then re-fetches `/api/data`.

### `POST /api/refit_filter`
Refit over a subset.
```json
{ "category": "Feature", "early_min": -2000, "late_max": 1500, "include_submissions": true }
```
Returns metrics + updated `filter` meta. Errors if fewer than 10 records match.

### `POST /api/sweep`
Grid-search over `eps_values × min_samples_values` (DBSCAN-only, capped at 400 combos). Used by the tuning tab's heat grid.

---

## Forecast / uncertainty

### `POST /api/predict_clusters`
Weighted KDE over cluster centroids; returns top-k plausible *new-cluster* locations.
```json
{ "expansion": 3.0, "top_k": 5, "min_dist_m": 30, "bw_scale": 1.2, "nms_m": 40 }
```
HDBSCAN persistence is used as the weight when active; DBSCAN falls back to `log(size + 1)`.

### `POST /api/bootstrap_forecast`
Resample centroids with replacement B times, refit weighted KDE, record where each input peak lands on the new density surface. Returns per-peak `sigma_m` (stdev over resamples, in metres) → rendered as fuzzy blobs on the map.
```json
{ "B": 30, "expansion": 3.0, "bw_scale": 1.2, "min_dist_m": 30, "peaks": [{ "rank", "lat", "lon" }] }
```

### `POST /api/cv_score`
Holdout CV (default 10% holdout × 3 folds, radius 20m). Refits on the held-in set, evaluates the KDE at held-out coords, counts hits where `score ≥ 90th-percentile density AND distance_to_cluster ≤ radius_m`. Reports `recall_mean ± recall_std`.

---

## Dataset swap

### `POST /api/load_dataset`
`{ "path": "other datasets/open-context-7232-records.csv" }`
Path is resolved relative to the project root and confined to it for safety. Calls `preprocess_dataset.load_and_clean`, replaces `RECORDS`, clears filter, refits the current model.

### `POST /api/upload_dataset`
Multipart form with a single `file` field. CSV-only. Saved into `other datasets/` (auto-renamed on collision), then loaded via the same path as above.
