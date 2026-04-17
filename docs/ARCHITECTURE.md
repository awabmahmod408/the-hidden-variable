# Architecture

## Stack

- **Backend**: Flask 3 serving JSON APIs + rendering a single Jinja template.
- **Compute**: scikit-learn (DBSCAN, HDBSCAN, BallTree, silhouette), SciPy (`gaussian_kde`), NumPy, pandas.
- **Frontend**: Vanilla ES modules over Leaflet 1.9 + leaflet.heat. No build step.
- **Persistence**: pickle (`model_state.pkl`) for warm starts; CSV for submissions and exports.

## Runtime shape

```
┌──────────────────────────────────────────────────────────────────┐
│  Browser (Leaflet SPA — templates/index.html + static/js/app.js) │
└───────────────┬──────────────────────────────────────────────────┘
                │ JSON / multipart
┌───────────────▼──────────────────────────────────────────────────┐
│  Flask app (app.py) — 17 endpoints                               │
│                                                                  │
│    STATE (module global, mutable)                                │
│    ├─ model name, params (eps_m, min_cluster_size, …)            │
│    ├─ labels, cluster_pts, cluster_labels, known_pts             │
│    ├─ kde, lat_grid, lon_grid, density_grid                      │
│    ├─ persistence (HDBSCAN), probabilities (soft membership)     │
│    └─ silhouette, top_n_threshold, cluster_summaries             │
│                                                                  │
│    RECORDS          — raw DataFrame (source of truth)            │
│    ACTIVE_RECORDS   — current subset (filters + submissions)     │
│    PREDICTIONS      — list[dict] of top-N KDE peaks              │
│    FILTER_META      — category / era / include_submissions flags │
└───────────────┬──────────────────────────────────────────────────┘
                │
┌───────────────▼────────────────┐ ┌──────────────────────────────┐
│  predict_artifacts.py          │ │  preprocess_dataset.py       │
│  (clustering, KDE, candidates, │ │  (Open Context CSV cleaner)  │
│   plotting — original one-shot │ │                              │
│   pipeline, reused by Flask)   │ │                              │
└────────────────────────────────┘ └──────────────────────────────┘
```

`app.py` imports `build_cluster_summaries`, `kde_on_grid`, `pick_candidates` from `predict_artifacts.py` and `load_and_clean` from `preprocess_dataset.py`. The one-shot CLI (`predict_artifacts.py __main__`) produces `predicted_sites.csv`, `artifact_map.html`, `artifact_3d.html`, and a standalone Folium map — independent of Flask.

## Lifecycle

1. **Cold start** (`py app.py`):
   - `_load_state()` reads `model_state.pkl`, reconstructs the `gaussian_kde`.
   - `_load_records()` reads `open-context-2537-records.csv`, normalizes columns, synthesizes `context_path`.
   - `_prime_state()` runs `fit_and_set(model="dbscan")` to populate grid arrays the pickle doesn't carry.

2. **User interaction** (`/api/data`, `/api/classify`, etc.):
   - Pure reads off `STATE` / `RECORDS` / `PREDICTIONS`.

3. **Refit triggers** — all funnel through `fit_and_set()`:
   - Model toggle (`/api/switch_model`)
   - Recluster with new params (`/api/recluster`)
   - Filter refit (`/api/refit_filter`) with `subset_mask` / `extra_rows`
   - Active-learning save (`/api/add` with `refit: true`)
   - Dataset swap (`/api/load_dataset`, `/api/upload_dataset`) — also rewrites `RECORDS`

4. **Frontend refresh**:
   - Any refit response triggers `reloadData()` → re-fetch `/api/data` → redraw layers.
   - Dataset swap passes `{recenter: true}` → `map.flyToBounds()` on the new point cloud.
   - CV recall (`/api/cv_score`) runs async via `setTimeout(runCV, 50)` so the map paints first.

## Concurrency model

Single-threaded Flask dev server. Every refit mutates module-level globals (`STATE`, `RECORDS`, `ACTIVE_RECORDS`, `PREDICTIONS`, `FILTER_META`). Safe only because requests serialize. For multi-user deployment, wrap with a lock or move STATE into a per-request context.

## Why these choices

- **sklearn HDBSCAN over pypi `hdbscan`**: the pypi package fails to build on Windows; sklearn 1.3+ ships a drop-in `sklearn.cluster.HDBSCAN` with `cluster_persistence_` and `probabilities_`.
- **Haversine radians**: sklearn DBSCAN/BallTree accept `metric="haversine"` when coords are in radians — gives distance in metres via `r × EARTH_R`.
- **Weighted KDE for forecasting**: HDBSCAN persistence is a more honest cluster-importance signal than raw count; DBSCAN falls back to `log(size+1)`.
- **Adaptive grid**: KDE eval is O(n_points × n_cells). With 7k+ points on a 300×300 grid this is ~630M ops; dropping to 130×130 keeps latency under 6s.
