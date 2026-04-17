# Prediction pipeline

End-to-end, one request's journey from CSV row to red marker on the map.

## 1. Preprocess — `preprocess_dataset.load_and_clean`

- Reads CSV with `encoding="utf-8-sig"` (strips BOM from Open Context exports).
- Renames the ~15 Open Context column names to lowercase short forms (`Latitude (WGS 84)` → `lat`, etc.).
- Synthesizes `context_path` by joining `Context`, `Context [2]`, `Context [3]`, ... with `/`.
- Coerces `lat`, `lon`, `early`, `late` to numeric; drops NaN lat/lon.
- Filters lat ∈ [-90, 90], lon ∈ [-180, 180]; drops (0, 0).
- Dedupes on `(lat, lon, label)`.
- Minimum 10 rows — otherwise raises.

## 2. Cluster — `app._fit_dbscan` / `_fit_hdbscan`

Coordinates → radians (so sklearn can use the haversine metric).

- **DBSCAN**: `eps = eps_m / 6_371_000`, `min_samples`. Labels `-1` are noise.
- **HDBSCAN**: `min_cluster_size`, `min_samples`, `cluster_selection_method="eom"`. Exposes `cluster_persistence_` ∈ [0, 1] and per-point `probabilities_`.

Silhouette (subsampled to ≤1500 points) computed as a quality hint.

## 3. KDE — `predict_artifacts.kde_on_grid`

- bbox of cluster points + 20% margin.
- `GRID_RESOLUTION` adaptively scaled by cluster-point count: ≤1500 → 300, ≤3500 → 220, ≤6000 → 170, >6000 → 130. Eval cost is O(n_points × n_cells).
- `gaussian_kde` with Scott's bandwidth × `KDE_BW_SCALE = 0.6` (sharper than default).

## 4. Candidate selection — `predict_artifacts.pick_candidates`

Over every grid cell:

1. **Proximity filter**: BallTree haversine against all known points → keep cells with distance ≥ `MIN_DISTANCE_M = 2m` (so we don't predict where we already dug).
2. **Plausibility filter**: BallTree against cluster points → keep cells with distance ≤ `MAX_CLUSTER_DISTANCE_M = 15m` (so predictions sit near real data).
3. Sort surviving cells by density descending.
4. **Non-max suppression** at `NMS_SPACING_M = 3m` — greedy pick, reject any cell within 3m of an already-picked pick.
5. Take top `TOP_N = 100`.

Output: `pd.DataFrame` with `rank, latitude, longitude, density_score`.

## 5. Cluster summaries — `predict_artifacts.build_cluster_summaries`

For each non-noise label: count, centroid (mean lat/lon), convex hull (Shapely), category histogram, era span. Used by the Records tab, the cluster legend, and hull overlays.

If HDBSCAN ran, `persistence` is attached to each summary.

## 6. Classification — `app.classify`

For a user-supplied `(lat, lon)`:

- Haversine distance to nearest known point → `nearest_known_m`.
- Haversine distance to nearest cluster point → `nearest_cluster_m`; assigned to that cluster if `nearest_cluster_m ≤ eps_m × 2`, else `-1`.
- KDE score at the point → percentile rank among all grid scores.
- Verdict ladder:
  - `< MIN_DISTANCE_M` from a known point → **KNOWN AREA**
  - `≤ MAX_CLUSTER_DISTANCE_M` and `score ≥ top-N threshold` → **HIGH-POTENTIAL DISCOVERY ZONE**
  - `≤ MAX_CLUSTER_DISTANCE_M` only → **PLAUSIBLE — near a cluster, below top-N threshold**
  - else → **LOW-POTENTIAL / OUTSIDE SURVEYED AREA**

## 7. Cluster-growth forecasting — `api_predict_clusters`

Different KDE: fit over *cluster centroids* instead of raw points, weighted by persistence (HDBSCAN) or `log(count+1)` (DBSCAN). Grid expanded around centroid bbox by `expansion=3×`. Filters: distance to nearest centroid ≥ `min_dist_m=30m`. NMS at `nms_m=40m`. Returns top-k "where might a new cluster form" peaks.

## 8. Bootstrap uncertainty — `api_bootstrap_forecast`

For `B=30` resamples of centroids (with replacement), re-fit the weighted KDE on a 160×160 grid, snap each input peak to the nearest high-density cell within a 50m search window, record its (lat, lon). Per-peak `sigma_m = sqrt(var_dlat + var_dlon)` in metres. Frontend draws a fuzzy blob of that radius.

## 9. Cross-validation — `api_cv_score`

Hold out 10% of records `folds` times. Re-fit the current model on the held-in set. Evaluate the held-in KDE at held-out coords. Count hits where both `score ≥ 90th-percentile` and `distance_to_held_in_cluster ≤ radius_m`. Report `recall_mean ± recall_std`. Displayed in the header pill.

## 10. Active learning — `/api/add` with `refit: true`

After saving a new point to `submissions.csv`, re-read all submissions and call `fit_and_set(extra_rows=…)` — DBSCAN/HDBSCAN now include user feedback. `ACTIVE_RECORDS` is updated so the map shows them alongside the original corpus.

---

## Tunables

All defaults defined at the top of `predict_artifacts.py`:

```python
EPS_METERS              = 5     # DBSCAN radius
MIN_SAMPLES             = 5     # DBSCAN density threshold
GRID_RESOLUTION         = 300   # (max — adaptive)
MIN_DISTANCE_M          = 2     # reject predictions too close to known
MAX_CLUSTER_DISTANCE_M  = 15    # predictions must stay near clusters
NMS_SPACING_M           = 3     # spacing between picked peaks
TOP_N                   = 100   # how many predictions to return
KDE_BW_SCALE            = 0.6   # < 1 sharpens the density surface
```

Any of these can be overridden per-request via `/api/recluster`.
