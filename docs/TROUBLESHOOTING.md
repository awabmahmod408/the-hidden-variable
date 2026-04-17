# Troubleshooting

### Map is blank / no records visible
Flask probably wasn't restarted after editing a template. `debug=False` caches Jinja. **Ctrl+C in the terminal, then `py app.py`**, then hard-refresh.

### "Try a different dataset" button isn't in the header
Browser cached an old `index.html`. Open DevTools → Network → tick **"Disable cache"** → Ctrl+Shift+R. If `document.getElementById('dataset-toggle')` still returns `null`, the served HTML is stale → restart Flask.

### Dataset swap takes forever
Large datasets (7k+ rows) hit the O(n × grid) KDE cost. The adaptive grid keeps load < 7 s; if yours takes longer, lower `GRID_RESOLUTION` in `predict_artifacts.py` or reduce the high-N tier in `kde_on_grid`.

### CV pill stuck on "…"
CV runs async after dataset swap. If it stays pending >30 s, check the Flask console for a traceback — a too-small post-filter subset can fail CV (needs 3+ clusters after holdout).

### Upload returns "Preprocess failed"
The CSV is missing `Latitude (WGS 84)` / `Longitude (WGS 84)` columns, or has fewer than 10 valid rows. Verify it's an Open Context export.

### `hdbscan` import error
Don't install the pypi `hdbscan` package — it fails to build on Windows. The app uses `sklearn.cluster.HDBSCAN` (shipped with sklearn ≥ 1.3). `pip install -U scikit-learn`.

### Port 5000 already in use
`taskkill /F /IM python.exe` on Windows, or kill the older Flask process manually. Multiple background Python procs can survive after Ctrl+C if started via another tool.

### Predictions look identical after swapping datasets
Check the CV pill. If it shows a ridiculous number or "err", the new dataset has a geography the current `eps_m`/`min_cluster_size` doesn't suit. Open the Tuning tab, nudge parameters until silhouette climbs above ~0.9.
