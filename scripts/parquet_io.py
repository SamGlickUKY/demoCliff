"""Shared parquet I/O helpers for project scripts."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping, Sequence


def require_polars():
    try:
        import polars as pl  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "This script requires polars for parquet I/O. Install it with "
            "`python -m pip install polars`."
        ) from exc
    return pl


def write_rows_parquet(
    rows: Iterable[Mapping[str, object]],
    output_path: Path,
    *,
    schema_overrides: Mapping[str, object] | None = None,
    sort_by: Sequence[str] | None = None,
) -> int:
    """Write row dictionaries to a parquet file and return the row count."""

    pl = require_polars()
    row_list = list(rows)
    df = pl.DataFrame(row_list, schema_overrides=schema_overrides)
    if sort_by:
        df = df.sort(list(sort_by))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(output_path)
    return df.height
