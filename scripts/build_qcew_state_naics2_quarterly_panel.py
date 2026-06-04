#!/usr/bin/env python3
"""Build a state x 2-digit NAICS x quarter QCEW employment panel.

Inputs are the BLS QCEW yearly quarterly singlefile ZIPs, named like:
    raw_data/1990_qtrly_singlefile.zip
    raw_data/1991_qtrly_singlefile.zip
    ...

The script reads the ZIPs directly, without extracting the multi-GB CSV files.

Sector employment is calculated at the state-quarter-2-digit-NAICS level by
summing ownership components 1, 2, 3, and 5 for agglvl_code == 54. Quarterly
employment is the average of month1_emplvl, month2_emplvl, and month3_emplvl.
The total state quarterly employment column comes from the all-ownership,
all-industry statewide row: own_code == 0, industry_code == 10,
agglvl_code == 50.
"""

from __future__ import annotations

import argparse
import csv
import io
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, Iterator, TextIO, Tuple

from parquet_io import require_polars, write_rows_parquet

STATE_LEVEL_SUFFIX = "000"
TOTAL_INDUSTRY_CODE = "10"
TOTAL_AGGLVL_CODE = "50"
NAICS_2DIGIT_AGGLVL = "54"
ALL_OWNERSHIP_CODE = "0"
OWNERSHIP_COMPONENTS = {"1", "2", "3", "5"}
QUARTERS = {"1", "2", "3", "4"}

SectorKey = Tuple[str, str, int, str, str]  # state_fips, area_fips, year, qtr, naics2
TotalKey = Tuple[str, str, int, str]  # state_fips, area_fips, year, qtr


def is_state_level_area(area_fips: str) -> bool:
    """Return True for statewide QCEW area codes such as 01000."""

    return (
        len(area_fips) == 5
        and area_fips.isdigit()
        and area_fips.endswith(STATE_LEVEL_SUFFIX)
        and area_fips != "00000"
    )


def parse_int(value: str) -> int:
    return int((value or "").strip() or "0")


def quarter_average_employment(row: list[str], idx: Dict[str, int]) -> float:
    return (
        parse_int(row[idx["month1_emplvl"]])
        + parse_int(row[idx["month2_emplvl"]])
        + parse_int(row[idx["month3_emplvl"]])
    ) / 3


def format_employment(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 2)


def open_qcew_csv(input_dir: Path, year: int) -> tuple[TextIO, object]:
    """Open a yearly QCEW CSV from ZIP if available; otherwise from CSV.

    Returns (text_handle, owner). Close owner when finished. For ZIP inputs,
    closing owner also closes the underlying compressed member.
    """

    zip_path = input_dir / f"{year}_qtrly_singlefile.zip"
    csv_path = input_dir / f"{year}.q1-q4.singlefile.csv"

    if zip_path.exists():
        zf = zipfile.ZipFile(zip_path)
        members = [name for name in zf.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            zf.close()
            raise ValueError(f"Expected one CSV in {zip_path}, found {members}")
        binary = zf.open(members[0], "r")
        text = io.TextIOWrapper(binary, encoding="utf-8", errors="replace", newline="")
        return text, zf

    if csv_path.exists():
        text = csv_path.open(newline="", encoding="utf-8", errors="replace")
        return text, text

    raise FileNotFoundError(f"No QCEW ZIP or CSV found for {year} in {input_dir}")


def iter_qcew_rows(input_dir: Path, year: int) -> Iterator[list[str]]:
    text, owner = open_qcew_csv(input_dir, year)
    try:
        reader = csv.reader(text)
        header = next(reader)
        idx = {name: position for position, name in enumerate(header)}
        required = {
            "area_fips",
            "own_code",
            "industry_code",
            "agglvl_code",
            "year",
            "qtr",
            "disclosure_code",
            "month1_emplvl",
            "month2_emplvl",
            "month3_emplvl",
        }
        missing = required - set(idx)
        if missing:
            raise ValueError(f"{year} QCEW file is missing columns: {sorted(missing)}")
        yield header
        yield from reader
    finally:
        text.close()
        if owner is not text:
            owner.close()


def process_year(
    input_dir: Path, year: int
) -> tuple[Dict[SectorKey, float], Dict[TotalKey, float], Dict[SectorKey, int], Dict[SectorKey, int]]:
    sector_employment: DefaultDict[SectorKey, float] = defaultdict(float)
    total_employment: Dict[TotalKey, float] = {}
    suppressed_components: DefaultDict[SectorKey, int] = defaultdict(int)
    ownership_components: DefaultDict[SectorKey, int] = defaultdict(int)

    rows = iter_qcew_rows(input_dir, year)
    header = next(rows)
    idx = {name: position for position, name in enumerate(header)}

    for row in rows:
        area_fips = row[idx["area_fips"]]
        if not is_state_level_area(area_fips):
            continue

        qtr = row[idx["qtr"]]
        if qtr not in QUARTERS:
            continue

        state_fips = area_fips[:2]
        own_code = row[idx["own_code"]]
        industry_code = row[idx["industry_code"]]
        agglvl_code = row[idx["agglvl_code"]]
        row_year = int(row[idx["year"]])
        qtr_employment = quarter_average_employment(row, idx)

        if (
            own_code == ALL_OWNERSHIP_CODE
            and industry_code == TOTAL_INDUSTRY_CODE
            and agglvl_code == TOTAL_AGGLVL_CODE
        ):
            total_employment[(state_fips, area_fips, row_year, qtr)] = qtr_employment
            continue

        if agglvl_code != NAICS_2DIGIT_AGGLVL:
            continue
        if own_code not in OWNERSHIP_COMPONENTS:
            continue

        key = (state_fips, area_fips, row_year, qtr, industry_code)
        sector_employment[key] += qtr_employment
        ownership_components[key] += 1
        if row[idx["disclosure_code"]].strip():
            suppressed_components[key] += 1

    return (
        dict(sector_employment),
        total_employment,
        dict(suppressed_components),
        dict(ownership_components),
    )


def write_panel(input_dir: Path, output_path: Path, years: Iterable[int]) -> None:
    pl = require_polars()
    output_rows: list[dict[str, object]] = []

    for year in years:
        print(f"Processing {year}...")
        (
            sector_employment,
            total_employment,
            suppressed_components,
            ownership_components,
        ) = process_year(input_dir, year)

        for key in sorted(sector_employment, key=lambda k: (k[2], k[0], k[4], k[3])):
            state_fips, area_fips, row_year, qtr, naics2 = key
            total_key = (state_fips, area_fips, row_year, qtr)
            n_suppressed = suppressed_components.get(key, 0)
            output_rows.append(
                {
                    "state_fips": state_fips,
                    "area_fips": area_fips,
                    "year": row_year,
                    "quarter": int(qtr),
                    "year_quarter": f"{row_year}Q{qtr}",
                    "naics2": naics2,
                    "employment": format_employment(sector_employment[key]),
                    "state_total_employment": format_employment(
                        total_employment.get(total_key)
                    ),
                    "ownership_components": ownership_components.get(key, 0),
                    "suppressed_components": n_suppressed,
                    "any_suppressed": int(n_suppressed > 0),
                }
            )

    total_rows = write_rows_parquet(
        output_rows,
        output_path,
        schema_overrides={
            "state_fips": pl.Utf8,
            "area_fips": pl.Utf8,
            "year": pl.Int64,
            "quarter": pl.Int64,
            "year_quarter": pl.Utf8,
            "naics2": pl.Utf8,
            "employment": pl.Float64,
            "state_total_employment": pl.Float64,
            "ownership_components": pl.Int64,
            "suppressed_components": pl.Int64,
            "any_suppressed": pl.Int64,
        },
    )
    print(f"Wrote {total_rows:,} rows to {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=Path("raw_data"))
    parser.add_argument("--output", type=Path, default=Path("processed_data/qcew_state_naics2_quarterly_panel_1990_2025.parquet"))
    parser.add_argument("--start-year", type=int, default=1990)
    parser.add_argument("--end-year", type=int, default=2025)
    args = parser.parse_args()

    write_panel(args.input_dir, args.output, range(args.start_year, args.end_year + 1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
