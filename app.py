"""Flask website for the Giza artifact discovery model.

Routes:
    GET  /                -> main page (two tabs: Map + Add Artifact)
    GET  /api/data        -> known points + clusters + hulls + predictions (JSON)
    POST /api/classify    -> classify a lat/lon, return verdict + cluster
    POST /api/add         -> append a user-submitted artifact record to submissions.csv

The model state is loaded once at startup from model_state.pkl.
"""

from __future__ import annotations

import csv
import pickle
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from flask import Flask, jsonify, render_template, request
from scipy.stats import gaussian_kde
from sklearn.cluster import DBSCAN, HDBSCAN
from sklearn.metrics import silhouette_score
from sklearn.neighbors import BallTree

from predict_artifacts import (
    build_cluster_summaries,
    kde_on_grid,
    pick_candidates,
)

HERE = Path(__file__).parent
MODEL_PATH = HERE / "model_state.pkl"
CSV_PATH = HERE / "open-context-2537-records.csv"
PRED_CSV = HERE / "predicted_sites.csv"
SUBMISSIONS_CSV = HERE / "submissions.csv"

EARTH_R = 6_371_000.0

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Load model state + source records once
# ---------------------------------------------------------------------------
def _load_state():
    if not MODEL_PATH.exists():
        raise SystemExit(
            f"{MODEL_PATH.name} not found. Run `python predict_artifacts.py` first."
        )
    with open(MODEL_PATH, "rb") as f:
        state = pickle.load(f)
    state["kde"] = gaussian_kde(
        state["cluster_pts"].T, bw_method=state["kde_bw_factor"]
    )
    return state


def _load_records() -> pd.DataFrame:
    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    ctx_cols = [c for c in df.columns if c == "Context" or c.startswith("Context [")]
    # Join multi-level context into a single "/" path, dropping empty levels.
    df["context_path"] = (
        df[ctx_cols]
        .fillna("")
        .astype(str)
        .agg(lambda r: "/".join(x for x in r if x), axis=1)
    )
    df = df.rename(
        columns={
            "Latitude (WGS 84)": "lat",
            "Longitude (WGS 84)": "lon",
            "Item Label": "label",
            "Item Category": "category",
            "Early BCE/CE": "early",
            "Late BCE/CE": "late",
            "URI": "uri",
            "Citation URI": "citation_uri",
            "Project": "project",
            "Project URI": "project_uri",
            "Context URI": "context_uri",
            "icon": "icon",
            "Thumbnail": "thumbnail",
            "Published Date": "published",
            "Updated Date": "updated",
        }
    )
    keep = [
        "label", "lat", "lon", "category", "early", "late",
        "uri", "citation_uri", "project", "project_uri",
        "context_path", "context_uri",
        "icon", "thumbnail", "published", "updated",
    ]
    df = df[keep].copy()
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    return df.dropna(subset=["lat", "lon"]).reset_index(drop=True)


def _load_predictions() -> list[dict]:
    if not PRED_CSV.exists():
        return []
    pdf = pd.read_csv(PRED_CSV)
    return pdf.to_dict(orient="records")


STATE = _load_state()
STATE.setdefault("model", "dbscan")
STATE.setdefault("min_cluster_size", None)
STATE.setdefault("persistence", {})
RECORDS = _load_records()
ACTIVE_RECORDS = RECORDS
PREDICTIONS = _load_predictions()
FILTER_META = {"category": None, "early_min": None, "late_max": None,
               "include_submissions": False, "n_records": len(RECORDS)}


def _prime_state():
    """Warm STATE with lat/lon/density grids + soft probabilities for the default
    DBSCAN fit (the pickle doesn't carry grids)."""
    try:
        fit_and_set(model="dbscan",
                    eps_m=float(STATE.get("eps_m") or 5),
                    min_samples=int(STATE.get("min_samples") or 5))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Re-clustering: fit DBSCAN with given eps/min_samples, rebuild KDE + predictions,
# replace STATE / PREDICTIONS in place. Used by /api/recluster.
# ---------------------------------------------------------------------------
def _compute_silhouette(coords_rad: np.ndarray, labels: np.ndarray) -> float | None:
    non_noise = labels != -1
    n_clusters = len(set(labels) - {-1})
    if n_clusters < 2 or non_noise.sum() <= n_clusters:
        return None
    X = coords_rad[non_noise]
    y = labels[non_noise]
    if len(X) > 1500:
        rng = np.random.default_rng(0)
        idx = rng.choice(len(X), 1500, replace=False)
        X, y = X[idx], y[idx]
        if len(set(y)) < 2:
            return None
    try:
        return float(silhouette_score(X, y, metric="haversine"))
    except Exception:
        return None


def _fit_dbscan(eps_m: float, min_samples: int, coords_rad=None):
    if coords_rad is None:
        coords_rad = np.radians(RECORDS[["lat", "lon"]].to_numpy())
    labels = DBSCAN(
        eps=eps_m / EARTH_R, min_samples=min_samples, metric="haversine"
    ).fit_predict(coords_rad)
    return coords_rad, labels, None, None


def _fit_hdbscan(min_cluster_size: int, min_samples: int, coords_rad=None):
    """HDBSCAN on haversine-radian coordinates.

    Persistence ∈ [0, 1] per cluster — higher = more stable. Probabilities_ gives
    per-point soft membership.
    """
    if coords_rad is None:
        coords_rad = np.radians(RECORDS[["lat", "lon"]].to_numpy())
    model = HDBSCAN(
        min_cluster_size=int(min_cluster_size),
        min_samples=int(min_samples),
        metric="haversine",
        cluster_selection_method="eom",
    ).fit(coords_rad)
    labels = model.labels_
    persistence = {}
    unique = sorted(set(labels) - {-1})
    if hasattr(model, "cluster_persistence_") and len(model.cluster_persistence_):
        for i, lbl in enumerate(unique):
            if i < len(model.cluster_persistence_):
                persistence[int(lbl)] = float(model.cluster_persistence_[i])
    # Per-point soft membership (strength of assignment, 0..1).
    probs = getattr(model, "probabilities_", None)
    return coords_rad, labels, persistence, probs


def fit_and_set(model: str = "dbscan", **params) -> dict:
    """Rebuild clustering + KDE + predictions + cluster summaries; replace globals.

    Parameters:
        model: "dbscan" or "hdbscan"
        DBSCAN params : eps_m, min_samples
        HDBSCAN params: min_cluster_size, min_samples
    """
    global STATE, PREDICTIONS

    global ACTIVE_RECORDS
    model = (model or "dbscan").lower()
    # Optional subset mask (category/era) OR supplemental rows (active-learning).
    subset_mask = params.get("subset_mask")
    extra_rows = params.get("extra_rows")   # a DataFrame of submissions to append
    base = RECORDS if subset_mask is None else RECORDS.loc[subset_mask]
    if extra_rows is not None and len(extra_rows):
        sub_records = pd.concat([base, extra_rows], ignore_index=True)
    else:
        sub_records = base.reset_index(drop=True)
    ACTIVE_RECORDS = sub_records
    coords_rad_all = np.radians(sub_records[["lat", "lon"]].to_numpy())
    if model == "hdbscan":
        min_cluster_size = int(params.get("min_cluster_size", 5))
        min_samples = int(params.get("min_samples", 3))
        coords_rad, labels, persistence, probs = _fit_hdbscan(
            min_cluster_size, min_samples, coords_rad=coords_rad_all)
        eps_m = None
    else:
        eps_m = float(params.get("eps_m", 5))
        min_samples = int(params.get("min_samples", 5))
        coords_rad, labels, persistence, probs = _fit_dbscan(
            eps_m, min_samples, coords_rad=coords_rad_all)
        min_cluster_size = None
    n_clusters = len(set(labels) - {-1})
    noise = int((labels == -1).sum())
    if n_clusters == 0:
        return {
            "error": "no clusters",
            "model": model,
            "eps_m": eps_m,
            "min_cluster_size": min_cluster_size,
            "min_samples": min_samples,
            "n_clusters": 0,
            "noise": noise,
        }

    cluster_mask = labels != -1
    cluster_pts = sub_records.loc[cluster_mask, ["lat", "lon"]].to_numpy()
    known_pts = sub_records[["lat", "lon"]].to_numpy()

    lat_grid, lon_grid, density, kde = kde_on_grid(cluster_pts)

    preds = pick_candidates(lat_grid, lon_grid, density, known_pts, cluster_pts)
    preds.insert(0, "rank", np.arange(1, len(preds) + 1))

    cluster_summaries = build_cluster_summaries(sub_records, labels)
    # Attach persistence to each summary (HDBSCAN only — None for DBSCAN).
    if persistence:
        for s in cluster_summaries:
            s["persistence"] = round(persistence.get(int(s["id"]), 0.0), 4)

    silhouette = _compute_silhouette(coords_rad, labels)

    prev_min_d  = STATE.get("min_distance_m", 2) if isinstance(STATE, dict) else 2
    prev_max_cd = STATE.get("max_cluster_distance_m", 15) if isinstance(STATE, dict) else 15

    STATE = {
        "model": model,
        "eps_m": eps_m,
        "min_cluster_size": min_cluster_size,
        "min_samples": min_samples,
        "labels": labels,
        "known_pts": known_pts,
        "cluster_pts": cluster_pts,
        "cluster_labels": labels[cluster_mask],
        "kde": kde,
        "kde_bw_factor": float(kde.factor),
        "grid_scores_sorted": np.sort(density.ravel()),
        "density_max": float(density.max()),
        "min_distance_m": prev_min_d,
        "max_cluster_distance_m": prev_max_cd,
        "top_n_threshold": float(preds["density_score"].min()) if len(preds) else 0.0,
        "cluster_summaries": cluster_summaries,
        "silhouette": silhouette,
        "persistence": persistence or {},
        "probabilities": probs if probs is not None else None,
        "lat_grid": lat_grid,
        "lon_grid": lon_grid,
        "density_grid": density,
    }

    PREDICTIONS = preds.to_dict(orient="records")

    cluster_sizes = [int((labels == lbl).sum()) for lbl in sorted(set(labels) - {-1})]
    return {
        "model": model,
        "eps_m": eps_m,
        "min_cluster_size": min_cluster_size,
        "min_samples": min_samples,
        "n_clusters": n_clusters,
        "noise": noise,
        "noise_pct": round(noise / len(labels) * 100, 2),
        "silhouette": silhouette,
        "largest_cluster": max(cluster_sizes),
        "smallest_cluster": min(cluster_sizes),
        "mean_cluster_size": round(float(np.mean(cluster_sizes)), 1),
        "predictions_found": len(preds),
    }


def _era_bounds():
    """Pull min/max Early/Late BCE/CE across RECORDS for the time slider."""
    def _to_num(s):
        s = pd.to_numeric(s, errors="coerce")
        return s.dropna()
    early = _to_num(RECORDS.get("early", pd.Series([], dtype=float)))
    late = _to_num(RECORDS.get("late", pd.Series([], dtype=float)))
    if not len(early) and not len(late):
        return None
    lo = int(min(early.min() if len(early) else 1e9, late.min() if len(late) else 1e9))
    hi = int(max(early.max() if len(early) else -1e9, late.max() if len(late) else -1e9))
    return [lo, hi]


def _build_subset_mask(category=None, early_min=None, late_max=None):
    """Boolean mask over RECORDS honoring category + era overlap."""
    m = np.ones(len(RECORDS), dtype=bool)
    if category:
        cats = {c.strip().lower() for c in (category if isinstance(category, list) else [category])}
        col = RECORDS["category"].astype(str).str.strip().str.lower()
        m &= col.isin(cats).to_numpy()
    if early_min is not None or late_max is not None:
        e = pd.to_numeric(RECORDS.get("early"), errors="coerce")
        l = pd.to_numeric(RECORDS.get("late"), errors="coerce")
        lo = -1e12 if early_min is None else float(early_min)
        hi =  1e12 if late_max  is None else float(late_max)
        # Keep rows whose [early, late] interval overlaps the slider window.
        overlap = (l.fillna(e).to_numpy() >= lo) & (e.fillna(l).to_numpy() <= hi)
        m &= overlap
    return m


def sweep_metrics(eps_m: float, min_samples: int) -> dict:
    """Cheap: cluster only, no KDE/predictions. Used by /api/sweep."""
    coords_rad, labels, _, _ = _fit_dbscan(eps_m, min_samples)
    unique = sorted(set(labels) - {-1})
    n_clusters = len(unique)
    noise = int((labels == -1).sum())
    cluster_sizes = [int((labels == lbl).sum()) for lbl in unique]
    return {
        "eps_m": eps_m,
        "min_samples": min_samples,
        "n_clusters": n_clusters,
        "noise": noise,
        "noise_pct": round(noise / len(labels) * 100, 2),
        "silhouette": _compute_silhouette(coords_rad, labels),
        "largest_cluster": max(cluster_sizes) if cluster_sizes else 0,
        "smallest_cluster": min(cluster_sizes) if cluster_sizes else 0,
        "mean_cluster_size": round(float(np.mean(cluster_sizes)), 1) if cluster_sizes else 0,
    }


# ---------------------------------------------------------------------------
# Core classification
# ---------------------------------------------------------------------------
def _haversine_m(lat1, lon1, lat2, lon2):
    lat1r, lat2r = np.radians(lat1), np.radians(lat2)
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1r) * np.cos(lat2r) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_R * np.arcsin(np.sqrt(a))


def classify(lat: float, lon: float) -> dict:
    cluster_pts = STATE["cluster_pts"]
    cluster_labels = STATE["cluster_labels"]
    known_pts = STATE["known_pts"]
    kde = STATE["kde"]
    eps_m = STATE["eps_m"]
    min_d = STATE["min_distance_m"]
    max_cd = STATE["max_cluster_distance_m"]
    sorted_scores = STATE["grid_scores_sorted"]
    top_n_thresh = STATE["top_n_threshold"]

    d_known = _haversine_m(lat, lon, known_pts[:, 0], known_pts[:, 1])
    nearest_known_m = float(d_known.min())

    d_cluster = _haversine_m(lat, lon, cluster_pts[:, 0], cluster_pts[:, 1])
    i_nearest = int(np.argmin(d_cluster))
    nearest_cluster_m = float(d_cluster[i_nearest])
    nearest_cluster_id = int(cluster_labels[i_nearest])
    assigned = nearest_cluster_id if nearest_cluster_m <= eps_m * 2 else -1

    score = float(kde(np.array([[lat], [lon]]))[0])
    percentile = float(np.searchsorted(sorted_scores, score) / len(sorted_scores) * 100)

    if nearest_known_m < min_d:
        verdict = "KNOWN AREA"
        verdict_class = "known"
    elif nearest_cluster_m <= max_cd and score >= top_n_thresh:
        verdict = "HIGH-POTENTIAL DISCOVERY ZONE"
        verdict_class = "high"
    elif nearest_cluster_m <= max_cd:
        verdict = "PLAUSIBLE — near a cluster, below top-20 threshold"
        verdict_class = "plausible"
    else:
        verdict = "LOW-POTENTIAL / OUTSIDE SURVEYED AREA"
        verdict_class = "low"

    return {
        "lat": lat,
        "lon": lon,
        "assigned_cluster": assigned,
        "nearest_cluster_id": nearest_cluster_id,
        "nearest_cluster_distance_m": round(nearest_cluster_m, 2),
        "nearest_known_distance_m": round(nearest_known_m, 2),
        "kde_score": score,
        "kde_percentile": round(percentile, 2),
        "top20_threshold": float(top_n_thresh),
        "verdict": verdict,
        "verdict_class": verdict_class,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    summaries = STATE["cluster_summaries"]
    labels = STATE["labels"]

    # Attach nearest-cluster id for every prediction (for hover tooltip).
    preds_out = []
    cluster_pts = STATE["cluster_pts"]
    cluster_labels = STATE["cluster_labels"]
    sorted_scores = STATE["grid_scores_sorted"]
    max_score = float(sorted_scores[-1]) if len(sorted_scores) else 1.0
    for p in PREDICTIONS:
        lat = float(p["latitude"])
        lon = float(p["longitude"])
        d = _haversine_m(lat, lon, cluster_pts[:, 0], cluster_pts[:, 1])
        i = int(np.argmin(d))
        score = float(p["density_score"])
        pct = float(np.searchsorted(sorted_scores, score) / len(sorted_scores) * 100)
        preds_out.append(
            {
                "rank": int(p["rank"]),
                "lat": lat,
                "lon": lon,
                "score": score,
                "score_normalized": round(score / max_score, 4) if max_score else 0,
                "score_percentile": round(pct, 2),
                "nearest_cluster_id": int(cluster_labels[i]),
                "nearest_cluster_distance_m": round(float(d[i]), 2),
            }
        )

    # Downsample known points for the browser (2537 is fine, but attach label + cluster).
    def _s(v):
        return "" if pd.isna(v) else str(v)

    records_out = []
    probs = STATE.get("probabilities")
    active = ACTIVE_RECORDS.reset_index(drop=True)
    def _g(row, key):
        return row[key] if key in row.index else ""
    for idx, row in active.iterrows():
        records_out.append(
            {
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "label": _s(_g(row, "label")),
                "category": _s(_g(row, "category")),
                "early": _s(_g(row, "early")),
                "late": _s(_g(row, "late")),
                "uri": _s(_g(row, "uri")),
                "citation_uri": _s(_g(row, "citation_uri")),
                "project": _s(_g(row, "project")),
                "project_uri": _s(_g(row, "project_uri")),
                "context_path": _s(_g(row, "context_path")),
                "context_uri": _s(_g(row, "context_uri")),
                "icon": _s(_g(row, "icon")),
                "thumbnail": _s(_g(row, "thumbnail")),
                "published": _s(_g(row, "published")),
                "updated": _s(_g(row, "updated")),
                "cluster": int(labels[idx]),
                "probability": round(float(probs[idx]), 3) if probs is not None else None,
            }
        )

    center_lat = float(active["lat"].mean())
    center_lon = float(active["lon"].mean())

    return jsonify(
        {
            "center": [center_lat, center_lon],
            "clusters": summaries,
            "records": records_out,
            "predictions": preds_out,
            "stats": {
                "records": len(records_out),
                "clusters": len(summaries),
                "noise": int((labels == -1).sum()),
                "predictions": len(preds_out),
            },
            "model": {
                "name": STATE.get("model", "dbscan"),
                "eps_m": STATE.get("eps_m"),
                "min_cluster_size": STATE.get("min_cluster_size"),
                "min_samples": STATE.get("min_samples"),
                "silhouette": STATE.get("silhouette"),
                "has_soft_membership": STATE.get("probabilities") is not None,
            },
            "filter": FILTER_META,
            "categories": sorted(
                {c for c in RECORDS["category"].dropna().astype(str).unique() if c}
            ),
            "era_bounds": _era_bounds(),
        }
    )


@app.route("/api/classify", methods=["POST"])
def api_classify():
    payload = request.get_json(force=True) or {}
    try:
        lat = float(payload.get("lat"))
        lon = float(payload.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid lat/lon"}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"error": "Coordinates out of range"}), 400
    return jsonify(classify(lat, lon))


@app.route("/api/add", methods=["POST"])
def api_add():
    payload = request.get_json(force=True) or {}
    try:
        lat = float(payload.get("lat"))
        lon = float(payload.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid lat/lon"}), 400

    label = (payload.get("label") or "").strip() or "Unnamed submission"
    category = (payload.get("category") or "").strip()
    early = (payload.get("early") or "").strip()
    late = (payload.get("late") or "").strip()
    notes = (payload.get("notes") or "").strip()

    result = classify(lat, lon)

    new_row = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "label": label,
        "lat": lat,
        "lon": lon,
        "category": category,
        "early": early,
        "late": late,
        "notes": notes,
        "assigned_cluster": result["assigned_cluster"],
        "verdict": result["verdict"],
        "kde_score": result["kde_score"],
        "kde_percentile": result["kde_percentile"],
    }

    fieldnames = list(new_row.keys())
    write_header = not SUBMISSIONS_CSV.exists()
    with open(SUBMISSIONS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            w.writeheader()
        w.writerow(new_row)

    refit_info = None
    if bool(payload.get("refit")):
        # Active learning: rebuild the model with all submissions appended.
        try:
            sdf = pd.read_csv(SUBMISSIONS_CSV) if SUBMISSIONS_CSV.exists() else pd.DataFrame()
            extra = None
            if len(sdf) and {"lat", "lon"}.issubset(sdf.columns):
                extra = sdf[["lat", "lon"]].dropna().copy()
                for c in RECORDS.columns:
                    if c not in extra.columns:
                        extra[c] = ""
                extra = extra[RECORDS.columns]
            model = STATE.get("model", "dbscan")
            kwargs = dict(extra_rows=extra)
            if model == "hdbscan":
                kwargs.update(min_cluster_size=int(STATE.get("min_cluster_size") or 5),
                              min_samples=int(STATE.get("min_samples") or 3))
            else:
                kwargs.update(eps_m=float(STATE.get("eps_m") or 5),
                              min_samples=int(STATE.get("min_samples") or 5))
            refit_info = fit_and_set(model=model, **kwargs)
        except Exception as e:
            refit_info = {"error": str(e)}

    return jsonify({"saved": True, "classification": result, "record": new_row,
                    "refit": refit_info})


@app.route("/api/current_params")
def api_current_params():
    return jsonify({
        "model": STATE.get("model", "dbscan"),
        "eps_m": STATE.get("eps_m"),
        "min_cluster_size": STATE.get("min_cluster_size"),
        "min_samples": STATE.get("min_samples"),
        "silhouette": STATE.get("silhouette"),
        "n_clusters": len(STATE.get("cluster_summaries", [])),
        "noise": int((STATE["labels"] == -1).sum()),
    })


@app.route("/api/recluster", methods=["POST"])
def api_recluster():
    p = request.get_json(force=True) or {}
    model = (p.get("model") or STATE.get("model", "dbscan")).lower()
    try:
        if model == "hdbscan":
            min_cluster_size = int(p.get("min_cluster_size", 5))
            min_samples = int(p.get("min_samples", 3))
            if not (2 <= min_cluster_size <= 200) or not (1 <= min_samples <= 100):
                return jsonify({"error": "Out of range (min_cluster_size 2–200, min_samples 1–100)"}), 400
            metrics = fit_and_set(model="hdbscan",
                                  min_cluster_size=min_cluster_size,
                                  min_samples=min_samples)
        else:
            eps_m = float(p.get("eps_m", 5))
            min_samples = int(p.get("min_samples", 5))
            if not (0.1 <= eps_m <= 500) or not (2 <= min_samples <= 100):
                return jsonify({"error": "Out of range (eps 0.1–500 m, min_samples 2–100)"}), 400
            metrics = fit_and_set(model="dbscan", eps_m=eps_m, min_samples=min_samples)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid parameters"}), 400
    return jsonify(metrics)


@app.route("/api/switch_model", methods=["POST"])
def api_switch_model():
    """Switch the active clustering algorithm and refit with sensible defaults.

    Frontend calls this from the header toggle — response carries the fresh
    metrics; the client should then re-fetch /api/data to redraw everything.
    """
    p = request.get_json(force=True) or {}
    model = (p.get("model") or "dbscan").lower()
    if model not in ("dbscan", "hdbscan"):
        return jsonify({"error": "Unknown model"}), 400
    if model == "hdbscan":
        metrics = fit_and_set(model="hdbscan",
                              min_cluster_size=int(p.get("min_cluster_size", 5)),
                              min_samples=int(p.get("min_samples", 3)))
    else:
        metrics = fit_and_set(model="dbscan",
                              eps_m=float(p.get("eps_m", 5)),
                              min_samples=int(p.get("min_samples", 5)))
    return jsonify(metrics)


@app.route("/api/predict_clusters", methods=["POST"])
def api_predict_clusters():
    """Forecast plausible NEW cluster locations using a weighted Gaussian KDE
    over existing cluster centroids, evaluated on an expanded bounding box,
    filtered by distance from known centroids, with non-max suppression.
    """
    p = request.get_json(force=True) or {}
    try:
        expansion = float(p.get("expansion", 3.0))
        top_k     = int(p.get("top_k", 5))
        min_dist_m = float(p.get("min_dist_m", 30.0))
        bw_scale  = float(p.get("bw_scale", 1.2))
        nms_m     = float(p.get("nms_m", 40.0))
    except (TypeError, ValueError):
        return jsonify({"error": "Bad parameters"}), 400
    if not (1.1 <= expansion <= 15) or not (1 <= top_k <= 50):
        return jsonify({"error": "Out of range (expansion 1.1-15, top_k 1-50)"}), 400

    summaries = STATE.get("cluster_summaries", [])
    if len(summaries) < 2:
        return jsonify({"error": "Need at least 2 clusters to forecast"}), 400

    centroids = np.array([s["centroid"] for s in summaries])  # [N, 2] (lat, lon)
    sizes = np.array([s["count"] for s in summaries], dtype=float)
    # Prefer HDBSCAN persistence when available (more meaningful than raw size);
    # fall back to log(size) for DBSCAN.
    if STATE.get("model") == "hdbscan" and STATE.get("persistence"):
        persistence = STATE["persistence"]
        weights = np.array([max(persistence.get(int(s["id"]), 0.0), 1e-3)
                            for s in summaries], dtype=float)
    else:
        weights = np.log(sizes + 1.0)
    weights = weights / weights.sum() if weights.sum() else np.ones_like(sizes) / len(sizes)

    kde = gaussian_kde(centroids.T, weights=weights, bw_method="scott")
    kde.set_bandwidth(bw_method=kde.factor * bw_scale)

    # Expanded bbox around centroid bbox.
    lat_mid = float(centroids[:, 0].mean())
    lon_mid = float(centroids[:, 1].mean())
    lat_rng = float(centroids[:, 0].max() - centroids[:, 0].min()) or 1e-4
    lon_rng = float(centroids[:, 1].max() - centroids[:, 1].min()) or 1e-4
    half_lat = lat_rng * expansion / 2
    half_lon = lon_rng * expansion / 2
    res = 220
    lat_lin = np.linspace(lat_mid - half_lat, lat_mid + half_lat, res)
    lon_lin = np.linspace(lon_mid - half_lon, lon_mid + half_lon, res)
    lon_g, lat_g = np.meshgrid(lon_lin, lat_lin)
    density = kde(np.vstack([lat_g.ravel(), lon_g.ravel()])).reshape(lat_g.shape)

    flat_lat = lat_g.ravel()
    flat_lon = lon_g.ravel()
    flat_d = density.ravel()

    tree = BallTree(np.radians(centroids), metric="haversine")
    grid_rad = np.radians(np.column_stack([flat_lat, flat_lon]))
    nn_dist, nn_idx = tree.query(grid_rad, k=1)
    dist_m = nn_dist.ravel() * EARTH_R
    nn_idx = nn_idx.ravel()

    # Candidates: far enough from any existing centroid.
    far_mask = dist_m >= min_dist_m
    order = np.argsort(-flat_d)
    order = order[far_mask[order]]

    picks = []
    d_max = float(flat_d.max()) if len(flat_d) else 1.0
    for idx in order:
        if len(picks) >= top_k:
            break
        lat = float(flat_lat[idx]); lon = float(flat_lon[idx])
        ok = True
        for pp in picks:
            if _haversine_m(lat, lon, pp["lat"], pp["lon"]) < nms_m:
                ok = False; break
        if not ok:
            continue
        nn = summaries[int(nn_idx[idx])]
        picks.append({
            "rank": len(picks) + 1,
            "lat": lat,
            "lon": lon,
            "score": float(flat_d[idx]),
            "score_normalized": round(float(flat_d[idx]) / d_max, 4) if d_max else 0.0,
            "dist_to_nearest_m": round(float(dist_m[idx]), 2),
            "nearest_cluster_id": int(nn["id"]),
            "predicted_size_hint": int(nn["count"]),
        })

    return jsonify({
        "predictions": picks,
        "bbox": [float(lat_mid - half_lat), float(lon_mid - half_lon),
                 float(lat_mid + half_lat), float(lon_mid + half_lon)],
        "params": {
            "expansion": expansion, "top_k": top_k,
            "min_dist_m": min_dist_m, "bw_scale": bw_scale, "nms_m": nms_m,
        },
        "n_source_clusters": len(summaries),
    })


@app.route("/api/sweep", methods=["POST"])
def api_sweep():
    p = request.get_json(force=True) or {}
    try:
        eps_vals = [float(v) for v in p.get("eps_values", [])]
        ms_vals  = [int(v)   for v in p.get("min_samples_values", [])]
    except (TypeError, ValueError):
        return jsonify({"error": "Bad values"}), 400
    if not eps_vals or not ms_vals:
        return jsonify({"error": "Provide eps_values and min_samples_values"}), 400
    if len(eps_vals) * len(ms_vals) > 400:
        return jsonify({"error": "Sweep too large (limit 400 combinations)"}), 400

    results = []
    for e in eps_vals:
        for ms in ms_vals:
            results.append(sweep_metrics(e, ms))
    return jsonify({"results": results, "count": len(results)})


# ---------------------------------------------------------------------------
# Heatmap (downsampled KDE density grid for Leaflet.heat)
# ---------------------------------------------------------------------------
@app.route("/api/heatmap")
def api_heatmap():
    lat_g = STATE.get("lat_grid"); lon_g = STATE.get("lon_grid")
    dens = STATE.get("density_grid")
    if lat_g is None or lon_g is None or dens is None:
        return jsonify({"points": [], "max": 0})
    # Downsample every Nth cell to cap payload size.
    step = max(1, dens.shape[0] // 90)
    sub = dens[::step, ::step]
    la  = lat_g[::step, ::step]
    lo  = lon_g[::step, ::step]
    mx = float(sub.max()) if sub.size else 0.0
    if mx <= 0:
        return jsonify({"points": [], "max": 0})
    thresh = sub.mean() * 0.2  # drop near-zero cells
    pts = []
    flat_lat = la.ravel(); flat_lon = lo.ravel(); flat_d = sub.ravel()
    for i in range(flat_d.size):
        if flat_d[i] < thresh: continue
        pts.append([float(flat_lat[i]), float(flat_lon[i]), float(flat_d[i] / mx)])
    return jsonify({"points": pts, "max": mx})


# ---------------------------------------------------------------------------
# Cross-validation score — headline "accuracy %"
# ---------------------------------------------------------------------------
@app.route("/api/cv_score", methods=["POST"])
def api_cv_score():
    """10-fold-style holdout: for each fold, remove H% of records, refit,
    compute KDE density at held-out points, count how many land above the
    held-in top-20 threshold AND within `radius_m` of a fitted cluster point.
    Returns recall @ top-k as a single percentage.
    """
    p = request.get_json(force=True) or {}
    try:
        holdout_pct = float(p.get("holdout_pct", 10)) / 100.0
        folds = int(p.get("folds", 5))
        radius_m = float(p.get("radius_m", 15))
    except (TypeError, ValueError):
        return jsonify({"error": "Bad parameters"}), 400
    model_name = STATE.get("model", "dbscan")
    rng = np.random.default_rng(42)
    n = len(RECORDS)
    recalls = []
    densities_thr = []
    for f in range(folds):
        idx = rng.choice(n, int(n * holdout_pct), replace=False)
        mask = np.ones(n, dtype=bool); mask[idx] = False
        try:
            coords_rad = np.radians(RECORDS.loc[mask, ["lat", "lon"]].to_numpy())
            if model_name == "hdbscan":
                _, labels, _, _ = _fit_hdbscan(
                    int(STATE.get("min_cluster_size") or 5),
                    int(STATE.get("min_samples") or 3),
                    coords_rad=coords_rad)
            else:
                _, labels, _, _ = _fit_dbscan(
                    float(STATE.get("eps_m") or 5),
                    int(STATE.get("min_samples") or 5),
                    coords_rad=coords_rad)
            cm = labels != -1
            if cm.sum() < 3:
                continue
            cpts = RECORDS.loc[mask, ["lat", "lon"]].to_numpy()[cm]
            _, _, dens, kde = kde_on_grid(cpts)
            thr = np.quantile(dens.ravel(), 0.90)   # top-10% density = "hit zone"
            densities_thr.append(float(thr))
            held = RECORDS.loc[~mask, ["lat", "lon"]].to_numpy()
            scores = kde(held.T)
            tree = BallTree(np.radians(cpts), metric="haversine")
            nn_dist, _ = tree.query(np.radians(held), k=1)
            nn_m = nn_dist.ravel() * EARTH_R
            hits = (scores >= thr) & (nn_m <= radius_m)
            if len(hits):
                recalls.append(float(hits.mean()))
        except Exception:
            continue
    if not recalls:
        return jsonify({"error": "CV failed — dataset too small or noisy"}), 400
    return jsonify({
        "model": model_name,
        "recall_mean": round(float(np.mean(recalls)) * 100, 2),
        "recall_std":  round(float(np.std(recalls))  * 100, 2),
        "folds": len(recalls),
        "holdout_pct": holdout_pct * 100,
        "radius_m": radius_m,
    })


# ---------------------------------------------------------------------------
# Bootstrap uncertainty on forecast peaks
# ---------------------------------------------------------------------------
@app.route("/api/bootstrap_forecast", methods=["POST"])
def api_bootstrap_forecast():
    """For each forecast peak, resample centroids with replacement B times,
    refit weighted KDE, record the peak displacement. Returns per-peak
    sigma (stdev of lat/lon perturbations, in metres) so the frontend can
    render a fuzzy blob radius.
    """
    p = request.get_json(force=True) or {}
    try:
        B = int(p.get("B", 30))
        expansion = float(p.get("expansion", 3.0))
        bw_scale = float(p.get("bw_scale", 1.2))
        min_dist_m = float(p.get("min_dist_m", 30.0))
        peaks = p.get("peaks") or []
    except (TypeError, ValueError):
        return jsonify({"error": "Bad parameters"}), 400
    summaries = STATE.get("cluster_summaries", [])
    if len(summaries) < 3 or not peaks:
        return jsonify({"error": "Not enough clusters or no peaks"}), 400
    centroids = np.array([s["centroid"] for s in summaries])
    sizes = np.array([s["count"] for s in summaries], dtype=float)
    if STATE.get("model") == "hdbscan" and STATE.get("persistence"):
        base_w = np.array([max(STATE["persistence"].get(int(s["id"]), 0), 1e-3)
                           for s in summaries], dtype=float)
    else:
        base_w = np.log(sizes + 1.0)
    lat_mid = centroids[:, 0].mean()
    lon_mid = centroids[:, 1].mean()
    lat_rng = (centroids[:, 0].max() - centroids[:, 0].min()) or 1e-4
    lon_rng = (centroids[:, 1].max() - centroids[:, 1].min()) or 1e-4
    half_lat, half_lon = lat_rng * expansion / 2, lon_rng * expansion / 2
    res = 160
    lat_lin = np.linspace(lat_mid - half_lat, lat_mid + half_lat, res)
    lon_lin = np.linspace(lon_mid - half_lon, lon_mid + half_lon, res)
    lon_g, lat_g = np.meshgrid(lon_lin, lat_lin)
    grid_pts = np.vstack([lat_g.ravel(), lon_g.ravel()])
    rng = np.random.default_rng(17)
    # For each bootstrap, compute a density grid and snap each peak to the
    # nearest local maximum within a search window.
    per_peak_lats = [[] for _ in peaks]
    per_peak_lons = [[] for _ in peaks]
    peak_arr = np.array([[pk["lat"], pk["lon"]] for pk in peaks])
    search_win_m = 50.0
    for _ in range(B):
        idx = rng.integers(0, len(centroids), len(centroids))
        c = centroids[idx]; w = base_w[idx]
        w = w / w.sum() if w.sum() else None
        try:
            kde = gaussian_kde(c.T, weights=w, bw_method="scott")
            kde.set_bandwidth(bw_method=kde.factor * bw_scale)
        except Exception:
            continue
        dens = kde(grid_pts).reshape(lat_g.shape)
        flat_lat = lat_g.ravel(); flat_lon = lon_g.ravel(); flat_d = dens.ravel()
        order = np.argsort(-flat_d)
        for pi, pk in enumerate(peak_arr):
            # first grid cell within search window with high density
            for k in order[:300]:
                dm = _haversine_m(pk[0], pk[1], flat_lat[k], flat_lon[k])
                if dm <= search_win_m:
                    per_peak_lats[pi].append(float(flat_lat[k]))
                    per_peak_lons[pi].append(float(flat_lon[k]))
                    break
    out = []
    for pi, pk in enumerate(peaks):
        if len(per_peak_lats[pi]) < 3:
            out.append({"rank": pk.get("rank"), "sigma_m": None, "samples": 0})
            continue
        la = np.array(per_peak_lats[pi]); lo = np.array(per_peak_lons[pi])
        dlat = (la - pk["lat"]) * 111_320.0
        dlon = (lo - pk["lon"]) * 111_320.0 * np.cos(np.radians(pk["lat"]))
        sigma = float(np.sqrt(dlat.var() + dlon.var()))
        out.append({
            "rank": pk.get("rank"),
            "sigma_m": round(sigma, 2),
            "samples": len(la),
        })
    _ = min_dist_m  # reserved for future filter; currently unused
    return jsonify({"peaks": out, "B": B})


# ---------------------------------------------------------------------------
# Refit with category / era filter (or clear filter)
# ---------------------------------------------------------------------------
@app.route("/api/refit_filter", methods=["POST"])
def api_refit_filter():
    global FILTER_META
    p = request.get_json(force=True) or {}
    category = p.get("category")
    early_min = p.get("early_min")
    late_max = p.get("late_max")
    include_submissions = bool(p.get("include_submissions"))
    mask = _build_subset_mask(category, early_min, late_max)
    if int(mask.sum()) < 10:
        return jsonify({"error": f"Filter too strict — only {int(mask.sum())} records match"}), 400
    extra = None
    if include_submissions and SUBMISSIONS_CSV.exists():
        try:
            sdf = pd.read_csv(SUBMISSIONS_CSV)
            if {"lat", "lon"}.issubset(sdf.columns):
                extra = sdf[["lat", "lon"]].dropna().copy()
                # Fill in missing columns so concat preserves schema.
                for c in RECORDS.columns:
                    if c not in extra.columns:
                        extra[c] = ""
                extra = extra[RECORDS.columns]
        except Exception:
            extra = None
    model = STATE.get("model", "dbscan")
    kwargs = dict(subset_mask=mask, extra_rows=extra)
    if model == "hdbscan":
        kwargs.update(min_cluster_size=int(STATE.get("min_cluster_size") or 5),
                      min_samples=int(STATE.get("min_samples") or 3))
    else:
        kwargs.update(eps_m=float(STATE.get("eps_m") or 5),
                      min_samples=int(STATE.get("min_samples") or 5))
    metrics = fit_and_set(model=model, **kwargs)
    FILTER_META = {
        "category": category, "early_min": early_min, "late_max": late_max,
        "include_submissions": include_submissions,
        "n_records": int(mask.sum()) + (len(extra) if extra is not None else 0),
    }
    return jsonify({**metrics, "filter": FILTER_META})


# ---------------------------------------------------------------------------
# CSV exports
# ---------------------------------------------------------------------------
from flask import Response

def _csv_response(df: pd.DataFrame, filename: str) -> Response:
    return Response(
        df.to_csv(index=False),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/api/export/<which>")
def api_export(which):
    which = which.lower()
    if which == "predictions":
        return _csv_response(pd.DataFrame(PREDICTIONS), "predictions.csv")
    if which == "clusters":
        rows = []
        for s in STATE.get("cluster_summaries", []):
            rows.append({
                "cluster_id": s["id"], "count": s["count"],
                "lat": s["centroid"][0], "lon": s["centroid"][1],
                "persistence": s.get("persistence"),
            })
        return _csv_response(pd.DataFrame(rows), "clusters.csv")
    if which == "assignments":
        active = ACTIVE_RECORDS.reset_index(drop=True)
        labels = STATE.get("labels")
        probs = STATE.get("probabilities")
        df = pd.DataFrame({
            "label": active.get("label", ""),
            "category": active.get("category", ""),
            "lat": active["lat"],
            "lon": active["lon"],
            "cluster": labels,
            "probability": probs if probs is not None else [None] * len(active),
        })
        return _csv_response(df, "assignments.csv")
    if which == "submissions":
        if SUBMISSIONS_CSV.exists():
            return _csv_response(pd.read_csv(SUBMISSIONS_CSV), "submissions.csv")
        return _csv_response(pd.DataFrame(), "submissions.csv")
    return jsonify({"error": "Unknown export"}), 400


_prime_state()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
