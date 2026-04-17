# Datasets

## Supported format

[Open Context](https://opencontext.org/query/) CSV exports. The preprocessor (`preprocess_dataset.load_and_clean`) recognises these columns and renames them:

| Open Context column          | Internal name      |
|------------------------------|--------------------|
| `Item Label`                 | `label`            |
| `Item Category`              | `category`         |
| `Latitude (WGS 84)`          | `lat`              |
| `Longitude (WGS 84)`         | `lon`              |
| `Early BCE/CE`               | `early`            |
| `Late BCE/CE`                | `late`             |
| `URI`                        | `uri`              |
| `Citation URI`               | `citation_uri`     |
| `Project`                    | `project`          |
| `Project URI`                | `project_uri`      |
| `Context URI`                | `context_uri`      |
| `icon`                       | `icon`             |
| `Thumbnail`                  | `thumbnail`        |
| `Published Date`             | `published`        |
| `Updated Date`               | `updated`          |
| `Context`, `Context [2]`, …  | joined → `context_path` |

Any additional columns are preserved but unused.

## Cleaning rules

1. UTF-8 BOM stripped (`encoding="utf-8-sig"`).
2. `lat`, `lon`, `early`, `late` coerced to numeric; NaN lat/lon rows dropped.
3. Coordinates outside [-90, 90] × [-180, 180] removed.
4. `(0, 0)` records removed (common placeholder for "unknown").
5. Dedup on `(lat, lon, label)` — Open Context often lists the same artifact multiple times under different contexts.
6. String columns: NaN → `""`.
7. Minimum 10 surviving rows — fewer raises `ValueError`.

## Adding datasets

**Drop-in**: put any Open Context CSV into `other datasets/`. It appears in the header dropdown on the next open.

**Upload via UI**: header → ↻ Try a different dataset → ⬆ Upload CSV. The server saves the file into `other datasets/` (auto-renaming on name collision) and loads it.

**Programmatic**: `POST /api/load_dataset` with `{"path": "other datasets/your.csv"}`.

Path resolution is sandboxed to the project root — you can't load a file outside the folder.

## Shipped datasets

| File | Rows (raw) | Rows (clean) | Area |
|---|---|---|---|
| `open-context-2537-records.csv` | 2,537 | 2,537 | ARCE Sphinx Project — Giza plateau (~29.975°N, 31.138°E) |
| `other datasets/open-context-7232-records.csv` | 7,232 | 7,216 | Realities of Life on Elephantine (~24.085°N, 32.886°E) |

## Performance envelope

KDE eval is O(n_cluster_points × n_grid_cells). Adaptive grid resolution:

| cluster points | grid | typical load time |
|---|---|---|
| ≤ 1,500   | 300 × 300 | 1–2 s |
| ≤ 3,500   | 220 × 220 | 2–3 s |
| ≤ 6,000   | 170 × 170 | 3–5 s |
| > 6,000   | 130 × 130 | 5–7 s |

CV recall (3 folds) runs separately on a `setTimeout` after the map paints, so it never blocks the initial render.
