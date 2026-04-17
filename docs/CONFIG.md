# Configuration reference

All tunables are module-level constants. No `.env` file, no CLI flags for the Flask server.

## `predict_artifacts.py`

```python
EPS_METERS             = 5     # DBSCAN neighbourhood radius (metres on Earth)
MIN_SAMPLES            = 5     # DBSCAN min neighbours to be a core point
GRID_RESOLUTION        = 300   # upper bound — adaptive from cluster size
MIN_DISTANCE_M         = 2     # predictions must be at least this far from any known point
MAX_CLUSTER_DISTANCE_M = 15    # predictions must be within this of some cluster point
NMS_SPACING_M          = 3     # non-max suppression between picked predictions
TOP_N                  = 100   # # of predictions returned
KDE_BW_SCALE           = 0.6   # < 1 sharpens the density surface
```

DBSCAN and KDE both depend on `EARTH_R = 6_371_000.0`. Don't change unless you're modelling a different celestial body.

## `app.py`

```python
HERE            = Path(__file__).parent
MODEL_PATH      = HERE / "model_state.pkl"
CSV_PATH        = HERE / "open-context-2537-records.csv"
PRED_CSV        = HERE / "predicted_sites.csv"
SUBMISSIONS_CSV = HERE / "submissions.csv"
```

`CSV_PATH` is mutated at runtime on dataset swap — initial value is just the default.

### Hyperparameter validation ranges

Enforced by `/api/recluster`:

- DBSCAN: `eps_m ∈ [0.1, 500]`, `min_samples ∈ [2, 100]`
- HDBSCAN: `min_cluster_size ∈ [2, 200]`, `min_samples ∈ [1, 100]`

Out-of-range requests return 400.

### Sweep cap

`/api/sweep` rejects grids with `|eps| × |min_samples| > 400` combos — silhouette scoring blows up otherwise.

### CV defaults

`/api/cv_score` defaults to `holdout_pct=10, folds=5, radius_m=15`. The header pill calls it with `folds=3, radius_m=20` for speed.

### Bootstrap defaults

`/api/bootstrap_forecast` defaults to `B=30, expansion=3.0, bw_scale=1.2, min_dist_m=30` and a 50 m peak-snap window (hard-coded in the handler).

## Flask server

```python
app.run(host="127.0.0.1", port=5000, debug=False)
```

`debug=False` means Jinja templates are cached — restart the server after editing `templates/index.html`. `static/*` is served fresh on every request but the browser caches aggressively; hard-refresh (Ctrl+Shift+R) to pick up JS/CSS edits.

## Frontend

`static/js/app.js` top-of-file `state` object holds everything. No config file, no bundler.

Drone simulation defaults (editable in the UI panel):
- Speed: 2 m/s
- Scan time per site: 2 s
- Sim speed-up: 1×
- Return to base: on
