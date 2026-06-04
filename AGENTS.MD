# Project Agent Rules

1. Keep raw source artifacts in `raw_data/` and derived analysis files in `processed_data/`.
2. Do not write new files under `data/`; treat that directory as deprecated legacy state.
3. Raw vendor downloads may remain in their source format (ZIP/CSV/API response) when no parquet source exists.
4. All processed tabular outputs must be parquet (`.parquet`), not CSV.
5. Scripts should default to reading processed inputs from `processed_data/` and raw inputs from `raw_data/`.
6. Python tabular I/O should use `polars`; prefer `scan_parquet()`/lazy frames for reads.
7. R tabular I/O should use `arrow` and `data.table`; prefer Arrow datasets/lazy dplyr queries where possible.
8. Only collect lazy queries at the final point needed for modeling, plotting, or writing.
9. Preserve stable schemas, column names, and sort order unless a task explicitly requests otherwise.
10. Results, figures, logs, and regression text outputs belong in `results/`.
11. If an external service only returns CSV, parse it at the boundary and immediately write parquet for reuse.
12. Keep command-line defaults aligned with the folder and parquet conventions above.
