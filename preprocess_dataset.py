"""Preprocess an Open Context export CSV into the schema used by the Flask app.

Usage (standalone):
    py preprocess_dataset.py "other datasets/open-context-7232-records.csv"

The function `load_and_clean(path)` returns a DataFrame with these columns:
    lat, lon, label, category, early, late,
    uri, citation_uri, project, project_uri,
    context_path, context_uri, icon, thumbnail, published, updated
plus any original unmapped columns (preserved for downstream use).

The Flask app calls `load_and_clean` directly when you swap datasets through
the "Try a different dataset" button.
"""

from __future__ import annotations

from pathlib import Path
import pandas as pd


RENAME = {
    "Item Label": "label",
    "Item Category": "category",
    "Latitude (WGS 84)": "lat",
    "Longitude (WGS 84)": "lon",
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


def load_and_clean(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")
    df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)

    # Normalize column names we recognize; leave others intact.
    df = df.rename(columns={k: v for k, v in RENAME.items() if k in df.columns})

    # Synthesize context_path from multi-level Context columns.
    ctx_cols = [c for c in df.columns if c == "Context" or c.startswith("Context [")]
    if ctx_cols:
        df["context_path"] = (
            df[ctx_cols]
            .fillna("")
            .astype(str)
            .agg(lambda r: "/".join(x for x in r if x), axis=1)
        )
    else:
        df["context_path"] = ""

    # Coerce numeric columns we depend on.
    for col in ("lat", "lon"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("early", "late"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Drop rows without coordinates — clustering needs both.
    if "lat" not in df.columns or "lon" not in df.columns:
        raise ValueError("CSV must contain WGS-84 latitude/longitude columns.")
    df = df.dropna(subset=["lat", "lon"]).copy()

    # Filter obvious junk coords (0,0 and wildly out-of-range).
    df = df[(df["lat"].between(-90, 90)) & (df["lon"].between(-180, 180))]
    df = df[~((df["lat"] == 0) & (df["lon"] == 0))]

    # Deduplicate exact duplicate rows (same coords + same label) — common in
    # Open Context exports where a record is listed multiple times under
    # different context paths.
    if "label" in df.columns:
        df = df.drop_duplicates(subset=["lat", "lon", "label"], keep="first")

    # Canonicalize string columns (NaN → empty).
    for c in ("label", "category", "uri", "citation_uri", "project", "project_uri",
              "context_uri", "icon", "thumbnail", "published", "updated", "context_path"):
        if c in df.columns:
            df[c] = df[c].fillna("").astype(str)

    df = df.reset_index(drop=True)

    # Final sanity — need enough points to cluster.
    if len(df) < 10:
        raise ValueError(f"Only {len(df)} usable rows — need at least 10 for clustering.")

    return df


def summary(df: pd.DataFrame) -> dict:
    return {
        "rows": int(len(df)),
        "unique_coords": int(df[["lat", "lon"]].drop_duplicates().shape[0]),
        "lat_range": [float(df["lat"].min()), float(df["lat"].max())],
        "lon_range": [float(df["lon"].min()), float(df["lon"].max())],
        "center": [float(df["lat"].mean()), float(df["lon"].mean())],
        "categories": sorted(c for c in df.get("category", pd.Series(dtype=str)).unique() if c),
    }


if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) < 2:
        print("Usage: py preprocess_dataset.py <csv path>")
        sys.exit(1)
    d = load_and_clean(sys.argv[1])
    print(json.dumps(summary(d), indent=2))
