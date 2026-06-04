#!/usr/bin/env python3
"""Clean a raw QCEW singlefile to state x 2-digit NAICS quarterly employment.

The raw QCEW quarterly file has monthly employment columns, not a single
quarterly employment column. This script defines quarterly employment as the
average of month1_emplvl, month2_emplvl, and month3_emplvl, then widens quarters
to separate columns in a parquet output.

By default, ownership components 1, 2, 3, and 5 are summed to approximate all
ownerships by 2-digit NAICS sector. Use --ownership private to keep only private
ownership (own_code == 5).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

from parquet_io import require_polars

STATE_LEVEL_SUFFIX = "000"
NAICS_2DIGIT_AGGLVL = "54"
ALL_OWNERSHIP_COMPONENTS = {"1", "2", "3", "5"}
PRIVATE_OWNERSHIP = {"5"}
QUARTERS = ("1", "2", "3", "4")


def clean_qcew(
    input_path: Path,
    output_path: Path,
    ownership_codes: Iterable[str],
    include_suppression_counts: bool,
) -> None:
    pl = require_polars()
    ownership_codes = sorted(set(ownership_codes))

    qtr_employment = (
        pl.col("month1_emplvl").cast(pl.Int64)
        + pl.col("month2_emplvl").cast(pl.Int64)
        + pl.col("month3_emplvl").cast(pl.Int64)
    ) / 3

    qcew = (
        pl.scan_csv(input_path, infer_schema_length=0)
        .filter(
            pl.col("area_fips").str.len_chars().eq(5)
            & pl.col("area_fips").str.contains(r"^\d{5}$")
            & pl.col("area_fips").str.ends_with(STATE_LEVEL_SUFFIX)
            & pl.col("area_fips").ne("00000")
            & pl.col("agglvl_code").eq(NAICS_2DIGIT_AGGLVL)
            & pl.col("own_code").is_in(ownership_codes)
            & pl.col("qtr").is_in(QUARTERS)
        )
        .with_columns(
            pl.col("area_fips").str.slice(0, 2).alias("state_fips"),
            qtr_employment.alias("quarterly_employment"),
            pl.col("disclosure_code")
            .fill_null("")
            .str.strip_chars()
            .ne("")
            .cast(pl.Int64)
            .alias("suppressed_component"),
        )
        .group_by("state_fips", "area_fips", "year", "industry_code", "qtr")
        .agg(
            pl.col("quarterly_employment").sum().alias("employment"),
            pl.col("suppressed_component").sum().alias("suppressed_components"),
        )
    )

    wide_aggs = []
    for qtr in QUARTERS:
        qtr_filter = pl.col("qtr").eq(qtr)
        wide_aggs.append(
            pl.when(qtr_filter.any())
            .then(pl.col("employment").filter(qtr_filter).sum().round(2))
            .otherwise(None)
            .alias(f"employment_q{qtr}")
        )
        if include_suppression_counts:
            wide_aggs.append(
                pl.col("suppressed_components")
                .filter(qtr_filter)
                .sum()
                .alias(f"suppressed_components_q{qtr}")
            )

    output = (
        qcew.group_by("state_fips", "area_fips", "year", "industry_code")
        .agg(wide_aggs)
        .rename({"industry_code": "naics2"})
        .sort(["state_fips", "year", "naics2"])
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.sink_parquet(output_path)
    except AttributeError:  # older polars
        output.collect().write_parquet(output_path)

    n_rows = pl.scan_parquet(output_path).select(pl.len()).collect().item()
    print(f"Wrote {n_rows:,} rows to {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("raw_data/2025.q1-q4.singlefile.csv"),
        help="Input raw QCEW q1-q4 singlefile CSV.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("processed_data/qcew_2025_state_naics2_quarterly_employment.parquet"),
        help="Output parquet for wide state x 2-digit NAICS quarterly employment.",
    )
    parser.add_argument(
        "--ownership",
        choices=("all", "private"),
        default="all",
        help=(
            "Ownership handling. 'all' sums ownership components 1,2,3,5; "
            "'private' keeps own_code 5 only. Default: all."
        ),
    )
    parser.add_argument(
        "--include-suppression-counts",
        action="store_true",
        help="Append counts of nonblank disclosure-code components per quarter.",
    )
    args = parser.parse_args()

    ownership_codes = (
        ALL_OWNERSHIP_COMPONENTS if args.ownership == "all" else PRIVATE_OWNERSHIP
    )
    clean_qcew(
        input_path=args.input,
        output_path=args.output,
        ownership_codes=ownership_codes,
        include_suppression_counts=args.include_suppression_counts,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
