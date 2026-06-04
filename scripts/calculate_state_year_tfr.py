#!/usr/bin/env python3
"""Calculate state-year fertility rates from births and women ages 15-44.

The output follows the project-specific definition requested here:

    fertility_rate = births in the state-year / women ages 15-44 in the state-year

This is commonly called the general fertility rate when multiplied by 1,000,
but the output column `tfr` keeps the requested births-per-woman ratio.

Birth counts are read from the state-year natality file produced by
query_cdc_wonder_births.py. Female population denominators are queried from CDC
WONDER population estimate tables and summed across 5-year age groups 15-19
through 40-44.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple
from urllib.parse import urljoin
from urllib.request import HTTPCookieProcessor, build_opener
from http.cookiejar import CookieJar

from parquet_io import require_polars, write_rows_parquet
from query_cdc_wonder_births import BASE_URL, WonderFormParser, flatten, request

FERTILITY_AGE_GROUPS = ("15-19", "20-24", "25-29", "30-34", "35-39", "40-44")


@dataclass(frozen=True)
class PopulationDataset:
    db: str
    page: str
    start_year: int
    end_year: int
    state_group: str
    state_variable: str
    year_variable: str
    age_group_variable: str
    sex_variable: str
    state_filter_is_hierarchical: bool
    source_label: str


POPULATION_DATASETS = (
    PopulationDataset(
        db="D178",
        page="bridged-race-v2020.html",
        start_year=1995,
        end_year=2020,
        state_group="V2-level1",
        state_variable="V2",
        year_variable="V1",
        age_group_variable="V8",
        sex_variable="V5",
        state_filter_is_hierarchical=True,
        source_label="CDC WONDER Bridged-Race Population Estimates 1990-2020",
    ),
    PopulationDataset(
        db="D203",
        page="single-race-single-year-v2024.html",
        start_year=2021,
        end_year=2024,
        state_group="V2",
        state_variable="V2",
        year_variable="V1",
        age_group_variable="V8",
        sex_variable="V5",
        state_filter_is_hierarchical=False,
        source_label="CDC WONDER Single-Race Population Estimates 2020-2024 by State and Single-Year Age",
    ),
)


def get_population_request_form(
    dataset: PopulationDataset,
) -> Tuple[object, Dict[str, List[str]], str]:
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    form_html = request(opener, urljoin(BASE_URL, dataset.page)).decode(
        "utf-8", errors="ignore"
    )

    parser = WonderFormParser()
    parser.feed(form_html)
    if not parser.form_action:
        raise RuntimeError(f"Could not find WONDER form action for {dataset.db}")

    return opener, parser.params, urljoin(BASE_URL, parser.form_action)


def configure_population_query(
    params: Dict[str, List[str]], dataset: PopulationDataset
) -> Dict[str, List[str]]:
    db = dataset.db
    years = [str(year) for year in range(dataset.start_year, dataset.end_year + 1)]

    params = {key: list(value) for key, value in params.items()}

    # Group by state and year; filter to female population ages 15-44.
    params["B_1"] = [f"{db}.{dataset.state_group}"]
    params["B_2"] = [f"{db}.{dataset.year_variable}"]
    params["B_3"] = ["*None*"]
    params["B_4"] = ["*None*"]
    params["B_5"] = ["*None*"]

    if dataset.state_filter_is_hierarchical:
        params[f"F_{db}.{dataset.state_variable}"] = ["*All*"]
        params[f"V_{db}.{dataset.state_variable}"] = [""]
        params[f"I_{db}.{dataset.state_variable}"] = ["*All* (The United States)\n"]
    else:
        params[f"V_{db}.{dataset.state_variable}"] = ["*All*"]
    params["O_location"] = [f"{db}.{dataset.state_variable}"]

    params[f"V_{db}.{dataset.year_variable}"] = years
    params[f"V_{db}.{dataset.age_group_variable}"] = list(FERTILITY_AGE_GROUPS)
    params[f"V_{db}.{dataset.sex_variable}"] = ["F"]
    params["O_age"] = [f"{db}.{dataset.age_group_variable}"]

    for key in list(params):
        if key.startswith("M_") and key != "M_1":
            del params[key]
    params["M_1"] = [f"{db}.M1"]

    params.pop("O_show_totals", None)
    params["O_export-format"] = ["csv"]
    params["O_change_action-Send-Export Results"] = ["Export Results"]
    params["O_timeout"] = ["600"]
    params["O_title"] = [
        f"Women ages 15-44 by state and year, {dataset.start_year}-{dataset.end_year}"
    ]
    params["action-Send"] = ["Send"]

    return params


def parse_population_csv(csv_bytes: bytes, dataset: PopulationDataset) -> List[dict]:
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(text.splitlines())
    rows: List[dict] = []

    for row in reader:
        state = (row.get("State") or row.get("States") or "").strip()
        state_code = (row.get("State Code") or row.get("States Code") or "").strip()
        year_code = (
            row.get("Yearly July 1st Estimates Code")
            or row.get("Year Code")
            or row.get("Year")
            or ""
        ).strip()
        population_raw = (row.get("Population") or "").strip().replace(",", "")

        # Skip WONDER notes/footer lines.
        if not (state_code.isdigit() and year_code.isdigit() and population_raw.isdigit()):
            continue

        rows.append(
            {
                "state": state,
                "state_code": state_code.zfill(2),
                "year": int(year_code),
                "women_15_44": int(population_raw),
                "population_source_db": dataset.db,
                "population_source": dataset.source_label,
            }
        )

    return rows


def query_population_dataset(dataset: PopulationDataset) -> List[dict]:
    opener, defaults, action_url = get_population_request_form(dataset)
    query_params = configure_population_query(defaults, dataset)
    csv_bytes = request(opener, action_url, flatten(query_params))
    return parse_population_csv(csv_bytes, dataset)


def write_population_parquet(rows: List[dict], output: Path) -> None:
    pl = require_polars()
    write_rows_parquet(
        sorted(rows, key=lambda row: (row["state_code"], row["year"])),
        output,
        schema_overrides={
            "state": pl.Utf8,
            "state_code": pl.Utf8,
            "year": pl.Int64,
            "women_15_44": pl.Int64,
            "population_source_db": pl.Utf8,
            "population_source": pl.Utf8,
        },
    )


def read_births(input_path: Path) -> Dict[Tuple[str, int], dict]:
    pl = require_polars()
    frame = (
        pl.scan_parquet(input_path)
        .select("state", "state_code", "year", "births", "source_db")
        .with_columns(
            pl.col("state_code").cast(pl.Utf8).str.zfill(2),
            pl.col("year").cast(pl.Int64),
            pl.col("births").cast(pl.Int64),
        )
        .collect()
    )
    births: Dict[Tuple[str, int], dict] = {}
    for row in frame.iter_rows(named=True):
        state_code = row["state_code"]
        year = int(row["year"])
        births[(state_code, year)] = {
            "state": row["state"] or "",
            "state_code": state_code,
            "year": year,
            "births": int(row["births"]),
            "births_source_db": row["source_db"] or "",
        }
    return births


def read_population(input_path: Path) -> List[dict]:
    pl = require_polars()
    frame = (
        pl.scan_parquet(input_path)
        .select(
            "state",
            "state_code",
            "year",
            "women_15_44",
            "population_source_db",
            "population_source",
        )
        .with_columns(
            pl.col("state_code").cast(pl.Utf8).str.zfill(2),
            pl.col("year").cast(pl.Int64),
            pl.col("women_15_44").cast(pl.Int64),
        )
        .collect()
    )
    return [
        {
            "state": row["state"] or "",
            "state_code": row["state_code"],
            "year": int(row["year"]),
            "women_15_44": int(row["women_15_44"]),
            "population_source_db": row["population_source_db"] or "",
            "population_source": row["population_source"] or "",
        }
        for row in frame.iter_rows(named=True)
    ]


def validate_population_grid(rows: List[dict], expected_years: set[int]) -> None:
    state_codes = {row["state_code"] for row in rows}
    observed = {(row["state_code"], row["year"]) for row in rows}
    expected = {(state_code, year) for state_code in state_codes for year in expected_years}

    if len(state_codes) != 51:
        raise RuntimeError(f"Expected 51 state/DC codes, observed {len(state_codes)}")
    if observed != expected:
        missing = sorted(expected - observed)[:20]
        extra = sorted(observed - expected)[:20]
        raise RuntimeError(
            f"Unexpected population grid coverage. missing_first20={missing}, extra_first20={extra}"
        )
    if len(rows) != len(observed):
        raise RuntimeError(
            f"Duplicate population rows detected: {len(rows)} rows, "
            f"{len(observed)} unique state/year keys"
        )


def calculate_tfr_rows(births: Dict[Tuple[str, int], dict], population_rows: List[dict]) -> List[dict]:
    output_rows: List[dict] = []
    for pop in population_rows:
        key = (pop["state_code"], pop["year"])
        if key not in births:
            raise RuntimeError(f"Missing births row for state/year {key}")
        birth = births[key]
        women = pop["women_15_44"]
        if women <= 0:
            raise RuntimeError(f"Non-positive women_15_44 denominator for state/year {key}")
        tfr = birth["births"] / women
        output_rows.append(
            {
                "state": birth["state"] or pop["state"],
                "state_code": birth["state_code"],
                "year": birth["year"],
                "births": birth["births"],
                "women_15_44": women,
                "tfr": tfr,
                "fertility_rate_per_1000": tfr * 1000,
                "births_source_db": birth["births_source_db"],
                "population_source_db": pop["population_source_db"],
                "population_source": pop["population_source"],
            }
        )
    return sorted(output_rows, key=lambda row: (row["state_code"], row["year"]))


def write_tfr_parquet(rows: List[dict], output: Path) -> None:
    pl = require_polars()
    write_rows_parquet(
        rows,
        output,
        schema_overrides={
            "state": pl.Utf8,
            "state_code": pl.Utf8,
            "year": pl.Int64,
            "births": pl.Int64,
            "women_15_44": pl.Int64,
            "tfr": pl.Float64,
            "fertility_rate_per_1000": pl.Float64,
            "births_source_db": pl.Utf8,
            "population_source_db": pl.Utf8,
            "population_source": pl.Utf8,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--births",
        type=Path,
        default=Path("processed_data/births_state_year_1995_2024.parquet"),
        help="Input parquet path for all-birth state-year totals.",
    )
    parser.add_argument(
        "--population-output",
        type=Path,
        default=Path("processed_data/women_15_44_state_year_1995_2024.parquet"),
        help="Output parquet path for queried women ages 15-44 denominators.",
    )
    parser.add_argument(
        "--reuse-population",
        action="store_true",
        help="Reuse --population-output instead of querying CDC WONDER population tables.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("processed_data/tfr_state_year_1995_2024.parquet"),
        help="Output parquet path for state-year fertility rates.",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=15.0,
        help="Polite pause between WONDER export requests.",
    )
    args = parser.parse_args()

    expected_years = set(range(1995, 2025))

    if args.reuse_population:
        population_rows = read_population(args.population_output)
    else:
        population_rows: List[dict] = []
        for index, dataset in enumerate(POPULATION_DATASETS):
            if index and args.pause_seconds > 0:
                time.sleep(args.pause_seconds)
            print(
                f"Querying {dataset.db} ({dataset.start_year}-{dataset.end_year}) "
                "women ages 15-44...",
                file=sys.stderr,
            )
            population_rows.extend(query_population_dataset(dataset))
        validate_population_grid(population_rows, expected_years)
        write_population_parquet(population_rows, args.population_output)
        print(f"Wrote {len(population_rows):,} rows to {args.population_output}")

    validate_population_grid(population_rows, expected_years)
    births = read_births(args.births)
    tfr_rows = calculate_tfr_rows(births, population_rows)
    write_tfr_parquet(tfr_rows, args.output)
    print(f"Wrote {len(tfr_rows):,} rows to {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
