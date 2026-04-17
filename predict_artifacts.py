"""
Predict potential new artifact discovery locations near Giza using DBSCAN + KDE.

Pipeline:
  1. Load Open Context CSV (Giza / ARCE Sphinx Project records).
  2. Cluster known artifact coordinates with DBSCAN (haversine metric).
  3. Fit a Gaussian KDE on clustered points to build a density surface.
  4. Score a fine grid, reject cells too close to known points, keep cells
     near dense clusters, apply non-max suppression, return top-N candidates.
  5. Render a 2D interactive Folium map (world view, zoomable, layer toggle).
  6. Render a 3D interactive Plotly surface (KDE elevation + clusters + predictions).
  7. Persist model state to model_state.pkl for use by classify_point.py.
"""

from __future__ import annotations

import math
import pickle
from pathlib import Path

import folium
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from folium import plugins
from scipy.spatial import ConvexHull, QhullError
from scipy.stats import gaussian_kde
from sklearn.cluster import DBSCAN
from sklearn.neighbors import BallTree

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------
HERE = Path(__file__).parent
CSV_PATH = HERE / "open-context-2537-records.csv"
MAP_PATH = HERE / "artifact_map.html"
MAP3D_PATH = HERE / "artifact_3d.html"
PRED_CSV = HERE / "predicted_sites.csv"
MODEL_PATH = HERE / "model_state.pkl"

EARTH_R = 6_371_000.0  # metres

EPS_METERS = 5
MIN_SAMPLES = 5
GRID_RESOLUTION = 300
MIN_DISTANCE_M = 2
MAX_CLUSTER_DISTANCE_M = 15
NMS_SPACING_M = 3
TOP_N = 20
KDE_BW_SCALE = 0.6  # <1 sharpens the KDE

CLUSTER_COLORS = [
    "#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231",
    "#911eb4", "#46f0f0", "#f032e6", "#bcf60c", "#fabebe",
    "#008080", "#e6beff", "#9a6324", "#fffac8", "#800000",
    "#aaffc3", "#808000", "#ffd8b1", "#000075", "#808080",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_records(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df = df.rename(
        columns={
            "Latitude (WGS 84)": "lat",
            "Longitude (WGS 84)": "lon",
            "Item Label": "label",
            "Item Category": "category",
            "Early BCE/CE": "early",
            "Late BCE/CE": "late",
        }
    )
    df = df[["label", "lat", "lon", "category", "early", "late"]].copy()
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df = df.dropna(subset=["lat", "lon"]).reset_index(drop=True)
    return df


def run_dbscan(df: pd.DataFrame) -> np.ndarray:
    coords_rad = np.radians(df[["lat", "lon"]].to_numpy())
    eps_rad = EPS_METERS / EARTH_R
    labels = DBSCAN(
        eps=eps_rad, min_samples=MIN_SAMPLES, metric="haversine"
    ).fit_predict(coords_rad)
    return labels


def kde_on_grid(cluster_pts: np.ndarray):
    """Return (grid_lat, grid_lon, density) spanning cluster bbox + 20 % margin."""
    lat_min, lat_max = cluster_pts[:, 0].min(), cluster_pts[:, 0].max()
    lon_min, lon_max = cluster_pts[:, 1].min(), cluster_pts[:, 1].max()
    lat_pad = (lat_max - lat_min) * 0.2 or 1e-4
    lon_pad = (lon_max - lon_min) * 0.2 or 1e-4
    lat_lin = np.linspace(lat_min - lat_pad, lat_max + lat_pad, GRID_RESOLUTION)
    lon_lin = np.linspace(lon_min - lon_pad, lon_max + lon_pad, GRID_RESOLUTION)
    lon_grid, lat_grid = np.meshgrid(lon_lin, lat_lin)

    kde = gaussian_kde(cluster_pts.T, bw_method="scott")
    kde.set_bandwidth(bw_method=kde.factor * KDE_BW_SCALE)
    stacked = np.vstack([lat_grid.ravel(), lon_grid.ravel()])
    density = kde(stacked).reshape(lat_grid.shape)
    return lat_grid, lon_grid, density, kde


def build_cluster_summaries(df: pd.DataFrame, labels: np.ndarray) -> list[dict]:
    """One entry per cluster: id, color, centroid, bbox, count, convex-hull polygon."""
    summaries = []
    unique = sorted(set(labels) - {-1})
    for i, lbl in enumerate(unique):
        mask = labels == lbl
        sub = df.loc[mask, ["lat", "lon"]].to_numpy()
        centroid = sub.mean(axis=0)
        hull_poly: list[list[float]] = []
        uniq_pts = np.unique(sub, axis=0)
        if len(uniq_pts) >= 3:
            try:
                h = ConvexHull(uniq_pts)
                hull_poly = uniq_pts[h.vertices].tolist()
            except QhullError:
                hull_poly = []
        summaries.append(
            {
                "id": int(lbl),
                "color": CLUSTER_COLORS[i % len(CLUSTER_COLORS)],
                "count": int(mask.sum()),
                "centroid": [float(centroid[0]), float(centroid[1])],
                "bbox": [
                    float(sub[:, 0].min()),
                    float(sub[:, 1].min()),
                    float(sub[:, 0].max()),
                    float(sub[:, 1].max()),
                ],
                "hull": hull_poly,  # [[lat, lon], ...]
            }
        )
    return summaries


def haversine_m(lat1, lon1, lat2, lon2):
    lat1r, lat2r = np.radians(lat1), np.radians(lat2)
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1r) * np.cos(lat2r) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_R * np.arcsin(np.sqrt(a))


def pick_candidates(
    lat_grid, lon_grid, density, known_pts, cluster_pts
) -> pd.DataFrame:
    flat_lat = lat_grid.ravel()
    flat_lon = lon_grid.ravel()
    flat_d = density.ravel()

    known_rad = np.radians(known_pts)
    cluster_rad = np.radians(cluster_pts)
    grid_rad = np.radians(np.column_stack([flat_lat, flat_lon]))

    tree_known = BallTree(known_rad, metric="haversine")
    d_known, _ = tree_known.query(grid_rad, k=1)
    d_known_m = d_known.ravel() * EARTH_R

    tree_cluster = BallTree(cluster_rad, metric="haversine")
    d_cluster, _ = tree_cluster.query(grid_rad, k=1)
    d_cluster_m = d_cluster.ravel() * EARTH_R

    keep = (d_known_m >= MIN_DISTANCE_M) & (d_cluster_m <= MAX_CLUSTER_DISTANCE_M)
    cand = pd.DataFrame(
        {
            "lat": flat_lat[keep],
            "lon": flat_lon[keep],
            "score": flat_d[keep],
        }
    ).sort_values("score", ascending=False).reset_index(drop=True)

    # Non-max suppression in geographic space.
    picked = []
    for _, row in cand.iterrows():
        if len(picked) >= TOP_N:
            break
        ok = True
        for p in picked:
            if haversine_m(row.lat, row.lon, p[0], p[1]) < NMS_SPACING_M:
                ok = False
                break
        if ok:
            picked.append((row.lat, row.lon, row.score))

    return pd.DataFrame(picked, columns=["latitude", "longitude", "density_score"])


# ---------------------------------------------------------------------------
# 2D Folium map
# ---------------------------------------------------------------------------
def build_map(df: pd.DataFrame, labels: np.ndarray, preds: pd.DataFrame) -> folium.Map:
    mean_lat = df["lat"].mean()
    mean_lon = df["lon"].mean()

    fmap = folium.Map(
        location=[mean_lat, mean_lon],
        zoom_start=2,  # start at world view; user zooms in
        tiles="OpenStreetMap",
        control_scale=True,
    )
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri World Imagery",
        name="Satellite (Esri)",
        overlay=False,
        control=True,
    ).add_to(fmap)

    known_fg = folium.FeatureGroup(name="Known artifacts", show=True)
    clusters_fg = folium.FeatureGroup(name="DBSCAN clusters", show=False)
    pred_fg = folium.FeatureGroup(name="Predicted sites (top 20)", show=True)

    for _, row in df.iterrows():
        popup = f"<b>{row['label']}</b><br>{row.get('category','')}"
        folium.CircleMarker(
            location=[row["lat"], row["lon"]],
            radius=2,
            color="#1f77b4",
            fill=True,
            fill_opacity=0.7,
            popup=popup,
        ).add_to(known_fg)

    unique_labels = sorted(set(labels) - {-1})
    for i, lbl in enumerate(unique_labels):
        color = CLUSTER_COLORS[i % len(CLUSTER_COLORS)]
        mask = labels == lbl
        sub = df.loc[mask]
        for _, row in sub.iterrows():
            folium.CircleMarker(
                location=[row["lat"], row["lon"]],
                radius=3,
                color=color,
                fill=True,
                fill_opacity=0.85,
                popup=f"Cluster {lbl}",
            ).add_to(clusters_fg)

    for _, row in preds.reset_index(drop=True).iterrows():
        rank = int(row.name) + 1 if hasattr(row, "name") else 0
    # iterate explicitly to get rank
    for rank, (_, row) in enumerate(preds.iterrows(), start=1):
        folium.Marker(
            location=[row["latitude"], row["longitude"]],
            icon=folium.Icon(color="red", icon="star", prefix="fa"),
            popup=(
                f"<b>Predicted #{rank}</b><br>"
                f"lat: {row['latitude']:.6f}<br>"
                f"lon: {row['longitude']:.6f}<br>"
                f"score: {row['density_score']:.4g}"
            ),
        ).add_to(pred_fg)

    known_fg.add_to(fmap)
    clusters_fg.add_to(fmap)
    pred_fg.add_to(fmap)

    plugins.Fullscreen().add_to(fmap)
    plugins.MousePosition(position="bottomleft", prefix="Lat/Lon:").add_to(fmap)
    folium.LayerControl(collapsed=False).add_to(fmap)
    return fmap


# ---------------------------------------------------------------------------
# 3D Plotly visualisation
# ---------------------------------------------------------------------------
def build_3d(df, labels, lat_grid, lon_grid, density, preds, out_path: Path):
    fig = go.Figure()

    fig.add_trace(
        go.Surface(
            x=lon_grid,
            y=lat_grid,
            z=density,
            colorscale="YlOrBr",
            opacity=0.85,
            name="KDE density",
            showscale=True,
            colorbar=dict(title="Density"),
        )
    )

    # Known artifacts coloured by DBSCAN label.
    unique_labels = sorted(set(labels))
    for i, lbl in enumerate(unique_labels):
        mask = labels == lbl
        sub = df.loc[mask]
        color = "#999999" if lbl == -1 else CLUSTER_COLORS[i % len(CLUSTER_COLORS)]
        name = "Noise" if lbl == -1 else f"Cluster {lbl}"
        # Elevate points slightly above the surface at their grid location.
        pts_z = np.full(len(sub), density.max() * 0.02) + density.max() * 0.02
        fig.add_trace(
            go.Scatter3d(
                x=sub["lon"],
                y=sub["lat"],
                z=pts_z,
                mode="markers",
                marker=dict(size=3, color=color),
                name=name,
                hovertext=sub["label"],
                hoverinfo="text+x+y",
            )
        )

    if len(preds) > 0:
        pred_z = np.full(len(preds), density.max() * 1.05)
        fig.add_trace(
            go.Scatter3d(
                x=preds["longitude"],
                y=preds["latitude"],
                z=pred_z,
                mode="markers",
                marker=dict(size=6, color="red", symbol="diamond"),
                name="Predicted sites",
                hovertext=[
                    f"Rank {i+1} • score {s:.3g}"
                    for i, s in enumerate(preds["density_score"])
                ],
                hoverinfo="text+x+y",
            )
        )

    fig.update_layout(
        title="Giza Artifact Density — DBSCAN Clusters + KDE Surface + Predictions",
        scene=dict(
            xaxis_title="Longitude",
            yaxis_title="Latitude",
            zaxis_title="KDE density",
            camera=dict(eye=dict(x=1.4, y=-1.4, z=1.0)),
        ),
        legend=dict(itemsizing="constant"),
        margin=dict(l=0, r=0, t=50, b=0),
    )
    fig.write_html(str(out_path), include_plotlyjs="cdn", full_html=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print(f"Loading {CSV_PATH.name}")
    df = load_records(CSV_PATH)
    print(f"  {len(df)} records, {df[['lat','lon']].drop_duplicates().shape[0]} unique coords")

    print("Running DBSCAN ...")
    labels = run_dbscan(df)
    n_clusters = len(set(labels) - {-1})
    n_noise = int((labels == -1).sum())
    print(f"  clusters: {n_clusters} | noise: {n_noise}")

    if n_clusters == 0:
        raise SystemExit("No dense clusters found; try increasing EPS_METERS.")

    cluster_mask = labels != -1
    cluster_pts = df.loc[cluster_mask, ["lat", "lon"]].to_numpy()
    known_pts = df[["lat", "lon"]].to_numpy()

    print(f"Fitting KDE and scoring {GRID_RESOLUTION}x{GRID_RESOLUTION} grid ...")
    lat_grid, lon_grid, density, kde = kde_on_grid(cluster_pts)

    print("Selecting candidates ...")
    preds = pick_candidates(lat_grid, lon_grid, density, known_pts, cluster_pts)
    preds.insert(0, "rank", np.arange(1, len(preds) + 1))
    preds.to_csv(PRED_CSV, index=False)
    print(f"  wrote {PRED_CSV.name} ({len(preds)} rows)")
    print(preds.head(5).to_string(index=False))

    print("Building 2D Folium map ...")
    fmap = build_map(df, labels, preds.rename(columns={}))
    fmap.save(str(MAP_PATH))
    print(f"  wrote {MAP_PATH.name}")

    print("Building 3D Plotly visualisation ...")
    build_3d(df, labels, lat_grid, lon_grid, density, preds, MAP3D_PATH)
    print(f"  wrote {MAP3D_PATH.name}")

    print("Computing cluster hulls and centroids ...")
    cluster_summaries = build_cluster_summaries(df, labels)

    print("Persisting model state ...")
    grid_scores = density.ravel()
    state = {
        "cluster_summaries": cluster_summaries,
        "eps_m": EPS_METERS,
        "min_samples": MIN_SAMPLES,
        "labels": labels,
        "known_pts": known_pts,
        "cluster_pts": cluster_pts,
        "cluster_labels": labels[cluster_mask],
        "kde_bw_factor": float(kde.factor),
        "grid_scores_sorted": np.sort(grid_scores),
        "density_max": float(density.max()),
        "min_distance_m": MIN_DISTANCE_M,
        "max_cluster_distance_m": MAX_CLUSTER_DISTANCE_M,
        "top_n_threshold": float(preds["density_score"].min()) if len(preds) else 0.0,
        "csv_path": str(CSV_PATH),
        "map_path": str(MAP_PATH),
    }
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(state, f)
    print(f"  wrote {MODEL_PATH.name}")

    print("Done.")


if __name__ == "__main__":
    main()
