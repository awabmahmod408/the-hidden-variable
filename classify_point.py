"""
Classify a new coordinate against the fitted Giza DBSCAN+KDE model.

Usage:
    python classify_point.py                # interactive prompt
    python classify_point.py 29.9748 31.1380
    python classify_point.py 29.9748 31.1380 --update-map

Outputs:
    - Assigned cluster id (or "noise / outlier")
    - Distance to the nearest core artifact point (metres)
    - KDE density score and percentile rank among candidate grid cells
    - Verdict: KNOWN AREA | HIGH-POTENTIAL DISCOVERY ZONE | LOW-POTENTIAL
    - Optionally re-opens artifact_map.html and adds a green marker at the point.
"""

from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import numpy as np
from scipy.stats import gaussian_kde

HERE = Path(__file__).parent
MODEL_PATH = HERE / "model_state.pkl"
MAP_PATH = HERE / "artifact_map.html"

EARTH_R = 6_371_000.0


def haversine_m(lat1, lon1, lat2, lon2):
    lat1r, lat2r = np.radians(lat1), np.radians(lat2)
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1r) * np.cos(lat2r) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_R * np.arcsin(np.sqrt(a))


def load_state():
    if not MODEL_PATH.exists():
        sys.exit(
            f"Model state not found at {MODEL_PATH}. "
            "Run `python predict_artifacts.py` first."
        )
    with open(MODEL_PATH, "rb") as f:
        return pickle.load(f)


def classify(lat: float, lon: float, state: dict) -> dict:
    cluster_pts = state["cluster_pts"]
    cluster_labels = state["cluster_labels"]
    known_pts = state["known_pts"]
    kde = gaussian_kde(cluster_pts.T, bw_method=state["kde_bw_factor"])
    eps_m = state["eps_m"]
    min_d = state["min_distance_m"]
    max_cd = state["max_cluster_distance_m"]
    sorted_scores = state["grid_scores_sorted"]
    top_n_thresh = state["top_n_threshold"]

    # Distance to nearest known point.
    d_known = haversine_m(lat, lon, known_pts[:, 0], known_pts[:, 1])
    nearest_known_m = float(d_known.min())

    # Distance to nearest CLUSTERED (core) point + cluster id.
    d_cluster = haversine_m(lat, lon, cluster_pts[:, 0], cluster_pts[:, 1])
    i_nearest = int(np.argmin(d_cluster))
    nearest_cluster_m = float(d_cluster[i_nearest])
    nearest_cluster_id = int(cluster_labels[i_nearest])

    # Within 2*eps ⇒ part of that cluster; else outlier.
    if nearest_cluster_m <= eps_m * 2:
        assigned = nearest_cluster_id
    else:
        assigned = -1  # noise / outlier

    # KDE score + percentile rank.
    score = float(kde(np.array([[lat], [lon]]))[0])
    percentile = float((np.searchsorted(sorted_scores, score) / len(sorted_scores)) * 100)

    # Verdict.
    if nearest_known_m < min_d:
        verdict = "KNOWN AREA (overlaps an existing artifact)"
    elif nearest_cluster_m <= max_cd and score >= top_n_thresh:
        verdict = "HIGH-POTENTIAL DISCOVERY ZONE"
    elif nearest_cluster_m <= max_cd:
        verdict = "PLAUSIBLE — near a cluster but below top-20 threshold"
    else:
        verdict = "LOW-POTENTIAL / OUTSIDE SURVEYED AREA"

    return {
        "lat": lat,
        "lon": lon,
        "assigned_cluster": assigned,
        "nearest_cluster_id": nearest_cluster_id,
        "nearest_cluster_distance_m": nearest_cluster_m,
        "nearest_known_distance_m": nearest_known_m,
        "kde_score": score,
        "kde_percentile": percentile,
        "top20_threshold": float(top_n_thresh),
        "verdict": verdict,
    }


def print_result(r: dict) -> None:
    print()
    print(f"Point:                    lat={r['lat']:.6f}, lon={r['lon']:.6f}")
    assigned = r["assigned_cluster"]
    print(
        f"Assigned cluster:         "
        f"{'noise / outlier' if assigned == -1 else f'Cluster {assigned}'}"
    )
    print(f"Nearest core cluster:     Cluster {r['nearest_cluster_id']} "
          f"({r['nearest_cluster_distance_m']:.2f} m away)")
    print(f"Nearest known artifact:   {r['nearest_known_distance_m']:.2f} m")
    print(f"KDE density score:        {r['kde_score']:.4g}")
    print(f"Score percentile (grid):  {r['kde_percentile']:.1f}%")
    print(f"Top-20 score threshold:   {r['top20_threshold']:.4g}")
    print(f"Verdict:                  {r['verdict']}")
    print()


def update_map(r: dict) -> None:
    """Append a green marker for the classified point to artifact_map.html."""
    if not MAP_PATH.exists():
        print(f"(Map file {MAP_PATH} not found; skipping --update-map.)")
        return

    marker_js = f"""
<script>
(function() {{
  function findMap(retries) {{
    var map = null;
    for (var k in window) {{
      if (k.indexOf('map_') === 0 && window[k] && window[k]._container) {{
        map = window[k]; break;
      }}
    }}
    if (!map) {{ if (retries > 0) return setTimeout(function(){{findMap(retries-1);}}, 200); else return; }}
    var marker = L.marker([{r['lat']}, {r['lon']}],
      {{ icon: L.divIcon({{ className: '', html:
        '<div style="background:#2ca02c;border:2px solid white;border-radius:50%;width:18px;height:18px;box-shadow:0 0 4px #000;"></div>'
      }}) }});
    marker.bindPopup(
      '<b>Classified point</b><br>' +
      'lat: {r['lat']:.6f}<br>lon: {r['lon']:.6f}<br>' +
      'verdict: {r['verdict']}<br>' +
      'score: {r['kde_score']:.4g} (p{r['kde_percentile']:.1f})'
    ).addTo(map);
  }}
  findMap(25);
}})();
</script>
"""
    html = MAP_PATH.read_text(encoding="utf-8")
    marker_block_id = "<!-- classified-points -->"
    if marker_block_id not in html:
        html = html.replace("</body>", f"{marker_block_id}\n</body>")
    html = html.replace(marker_block_id, marker_block_id + "\n" + marker_js)
    MAP_PATH.write_text(html, encoding="utf-8")
    print(f"Appended classified point to {MAP_PATH.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lat", nargs="?", type=float)
    ap.add_argument("lon", nargs="?", type=float)
    ap.add_argument("--update-map", action="store_true")
    args = ap.parse_args()

    if args.lat is None or args.lon is None:
        try:
            args.lat = float(input("Latitude  (WGS-84): ").strip())
            args.lon = float(input("Longitude (WGS-84): ").strip())
        except (ValueError, EOFError):
            sys.exit("Invalid coordinate input.")

    state = load_state()
    result = classify(args.lat, args.lon, state)
    print_result(result)

    if args.update_map:
        update_map(result)


if __name__ == "__main__":
    main()
