# The Hidden Variable

An interactive archaeology-prediction web app. Given a CSV of known artifact coordinates (Open Context export format), it clusters the sites with **DBSCAN** or **HDBSCAN**, fits a **weighted Gaussian KDE** to the clusters, and surfaces the **top-100 locations** where new artifacts are most likely to be found. Includes a GPR drone-survey simulation, cluster-growth forecasting with bootstrap uncertainty, active learning from user submissions, and dataset hot-swap.

Originally built around the ARCE Sphinx Project archive (Giza plateau, 2,537 records). Works on any Open Context-format CSV.

![stack](https://img.shields.io/badge/python-3.11%2B-blue) ![stack](https://img.shields.io/badge/flask-3.x-black) ![stack](https://img.shields.io/badge/sklearn-1.3%2B-orange) ![stack](https://img.shields.io/badge/leaflet-1.9-green)

---

## Quick start

```bash
pip install -r requirements.txt
python predict_artifacts.py      # one-time: generate model_state.pkl + predicted_sites.csv
python app.py                    # serves http://127.0.0.1:5000
```

Open the URL and use the five tabs: **Discovery Map**, **Records Browser**, **Hyperparameter Tuning**, **Cluster Forecasting**, **Add / Classify Coordinates**.

To swap datasets at runtime, click **↻ Try a different dataset** in the header. Drop any Open Context CSV into `other datasets/` or use the **⬆ Upload CSV** button inside the dropdown.

---

## What's in the box

| Feature | Where |
|---|---|
| DBSCAN + HDBSCAN clustering (haversine) | `app.py` → `_fit_dbscan`, `_fit_hdbscan` |
| Weighted KDE peak picking (top-100) | `predict_artifacts.py` → `pick_candidates` |
| GPR drone simulation (TSP + 2-opt) | `static/js/app.js` → `planRoute`, `tickSim` |
| Cluster-growth forecasting | `/api/predict_clusters` |
| Bootstrap uncertainty per peak | `/api/bootstrap_forecast` |
| Cross-validation recall@r | `/api/cv_score` |
| Active learning from submissions | `/api/add` with `refit: true` |
| Dataset swap + CSV upload | `/api/datasets`, `/api/load_dataset`, `/api/upload_dataset` |
| Category / era filter refit | `/api/refit_filter` |
| CSV export (predictions, clusters, assignments, submissions) | `/api/export/<which>` |

Full walkthrough in [docs/](docs/).

---

## Project layout

```
the hidden variable/
├── app.py                         # Flask server + all API endpoints
├── predict_artifacts.py           # Core pipeline: load → DBSCAN → KDE → top-N
├── preprocess_dataset.py          # Standalone CSV normalizer (Open Context schema)
├── classify_point.py              # CLI: classify one lat/lon (verdict + cluster)
├── requirements.txt
│
├── model_state.pkl                # Generated — cached fit (DBSCAN + KDE + grids)
├── predicted_sites.csv            # Generated — top-N predictions
├── submissions.csv                # Generated — user-added points (active learning)
├── open-context-2537-records.csv  # Default dataset (Giza)
├── other datasets/                # Drop additional CSVs here
│
├── templates/index.html           # Single-page UI (5 tabs)
├── static/js/app.js               # Leaflet + all frontend logic
├── static/css/style.css
│
└── docs/                          # Full documentation
    ├── ARCHITECTURE.md
    ├── API.md
    ├── PIPELINE.md
    ├── UI.md
    ├── DATASETS.md
    ├── CONFIG.md
    └── TROUBLESHOOTING.md
```

---

## Model at a glance

1. **Load** CSV → normalize Open Context columns → drop null/out-of-range coords → dedup.
2. **Cluster** on haversine radians. DBSCAN (`eps=5m`) or HDBSCAN (`min_cluster_size=5`).
3. **KDE** (weighted Gaussian) over cluster points on an adaptive grid (130×130 → 300×300 depending on dataset size).
4. **Candidate selection**: reject cells within `MIN_DISTANCE_M=2m` of known points and farther than `MAX_CLUSTER_DISTANCE_M=15m` from any cluster point; rank by density; non-max suppress at `NMS_SPACING_M=3m`; keep top-100.
5. **Forecast new clusters**: weighted KDE over cluster *centroids* on an expanded bbox, far enough from any existing centroid, NMS at 40m → top-k peaks with bootstrap σ.
6. **Score user points**: haversine distance to nearest cluster + KDE percentile → verdict (`KNOWN AREA` / `HIGH-POTENTIAL` / `PLAUSIBLE` / `LOW-POTENTIAL`).

Accuracy (10% holdout, radius 20m, 3 folds): **≈ 98.8 %** recall on the Giza dataset.

---

## License & attribution

Artifact records © Open Context contributors (CC-BY). See each record's Citation URI.
Code is MIT-licensed for the academic/research context it was written in.
