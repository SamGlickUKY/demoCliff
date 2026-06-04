#!/usr/bin/env python3
"""Query Census PEP state-year-age population counts for all available years.

This ingest harmonizes the Census Population Estimates Program API vintages and
Census downloadable files that expose state age counts:

* 1990-1999: 1990/pep/int_charagegroups, county age-group rows aggregated to
  states. The API exposes age groups, not single-year ages.
* 2000-2009: 2000/pep/int_charagegroups, state age-group rows.
* 2010-2019: 2019/pep/charage, state single-year ages.
* 2020-2023: 2023/pep/charv, state single-year ages.
* 2024: Vintage 2024 downloadable PEP CSV, state single-year ages.

The default output therefore mixes age-group rows for 1990-2009 with
single-year rows for 2010-2024. Use --single-year-only to keep only years where
single-year state ages are available.

A Census API key is required for the API-backed years. Provide one with
--api-key, CENSUS_API_KEY, or a local config/api_key.ps1 containing a
CENSUS_API_KEY entry.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from parquet_io import require_polars, write_rows_parquet

ENDPOINT_1990_GROUPS = "https://api.census.gov/data/1990/pep/int_charagegroups"
ENDPOINT_2000_GROUPS = "https://api.census.gov/data/2000/pep/int_charagegroups"
ENDPOINT_2019_SINGLE = "https://api.census.gov/data/2019/pep/charage"
ENDPOINT_2023_SINGLE = "https://api.census.gov/data/2023/pep/charv"
PEP_2024_ALLDATA6_CSV_URL = (
    "https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/"
    "state/asrh/sc-est2024-alldata6.csv"
)

DEFAULT_START_YEAR = 1990
DEFAULT_END_YEAR = 2024

STATE_NAMES_51 = {
    "01": "Alabama",
    "02": "Alaska",
    "04": "Arizona",
    "05": "Arkansas",
    "06": "California",
    "08": "Colorado",
    "09": "Connecticut",
    "10": "Delaware",
    "11": "District of Columbia",
    "12": "Florida",
    "13": "Georgia",
    "15": "Hawaii",
    "16": "Idaho",
    "17": "Illinois",
    "18": "Indiana",
    "19": "Iowa",
    "20": "Kansas",
    "21": "Kentucky",
    "22": "Louisiana",
    "23": "Maine",
    "24": "Maryland",
    "25": "Massachusetts",
    "26": "Michigan",
    "27": "Minnesota",
    "28": "Mississippi",
    "29": "Missouri",
    "30": "Montana",
    "31": "Nebraska",
    "32": "Nevada",
    "33": "New Hampshire",
    "34": "New Jersey",
    "35": "New Mexico",
    "36": "New York",
    "37": "North Carolina",
    "38": "North Dakota",
    "39": "Ohio",
    "40": "Oklahoma",
    "41": "Oregon",
    "42": "Pennsylvania",
    "44": "Rhode Island",
    "45": "South Carolina",
    "46": "South Dakota",
    "47": "Tennessee",
    "48": "Texas",
    "49": "Utah",
    "50": "Vermont",
    "51": "Virginia",
    "53": "Washington",
    "54": "West Virginia",
    "55": "Wisconsin",
    "56": "Wyoming",
}

SEX_LABELS = {
    "0": "Both Male and Female",
    "1": "Male",
    "2": "Female",
}

# 1990 intercensal county table codes combine bridged race and sex:
# 01/03/05/07 are male race categories; 02/04/06/08 are female categories.
RACE_SEX_CODES_BY_SEX = {
    "0": {"01", "02", "03", "04", "05", "06", "07", "08"},
    "1": {"01", "03", "05", "07"},
    "2": {"02", "04", "06", "08"},
}


@dataclass(frozen=True)
class AgeInfo:
    age: int
    age_start: int
    age_end: int | None
    age_topcoded: int
    age_label: str
    age_granularity: str


@dataclass(frozen=True)
class OutputRow:
    state_fips: str
    state_name: str
    year: int
    age: int
    age_start: int
    age_end: int | None
    age_topcoded: int
    age_label: str
    age_granularity: str
    population: int
    source_age_code: str
    source_age_desc: str
    sex: str
    sex_desc: str
    universe: str
    source: str
    source_date_code: str
    source_date_desc: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def api_key_from_local_config(path: Path = Path("config/api_key.ps1")) -> str | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"CENSUS_API_KEY\s*=\s*['\"]([^'\"]+)['\"]", text)
    return match.group(1) if match else None


def resolve_api_key(cli_key: str | None) -> str:
    key = cli_key or os.environ.get("CENSUS_API_KEY") or api_key_from_local_config()
    if not key:
        raise RuntimeError(
            "Census API key required. Set CENSUS_API_KEY or pass --api-key."
        )
    return key


def fetch_json(endpoint: str, params: Dict[str, str], label: str) -> List[List[str]]:
    url = f"{endpoint}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "pep-state-age-ingest/1.0"})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=300) as response:
                raw_bytes = response.read()
                if response.status == 204 or not raw_bytes:
                    return []
                raw = raw_bytes.decode("utf-8", errors="replace")
                return json.loads(raw)
        except HTTPError as exc:
            if exc.code == 204:
                return []
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                body = exc.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"Census API error for {label}: {exc}; {body}") from exc
            time.sleep(10 * (attempt + 1))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Census API did not return JSON for {label}") from exc
    raise RuntimeError(f"unreachable retry state for {label}")


def header_index(header: List[str]) -> Dict[str, int]:
    # Predicate columns can be repeated in the response. Use the first instance,
    # which corresponds to requested variables in get=.
    return {name: header.index(name) for name in set(header)}


def age_1990_group(code: str) -> AgeInfo | None:
    if code == "00":
        return AgeInfo(0, 0, 0, 0, "Under age 1", "age_group")
    if code == "01":
        return AgeInfo(1, 1, 4, 0, "Ages 1-4", "age_group")
    if code.isdigit():
        value = int(code)
        if 2 <= value <= 17:
            start = (value - 1) * 5
            return AgeInfo(start, start, start + 4, 0, f"Ages {start}-{start + 4}", "age_group")
        if value == 18:
            return AgeInfo(85, 85, None, 1, "85+", "age_group")
    return None


def age_2000_group(code: str) -> AgeInfo | None:
    if not code.isdigit():
        return None
    value = int(code)
    if value == 1:
        return AgeInfo(0, 0, 4, 0, "Ages 0-4", "age_group")
    if 2 <= value <= 17:
        start = (value - 1) * 5
        return AgeInfo(start, start, start + 4, 0, f"Ages {start}-{start + 4}", "age_group")
    if value == 18:
        return AgeInfo(85, 85, None, 1, "85+", "age_group")
    return None


def age_2019_single(code: str) -> AgeInfo | None:
    if not code.isdigit():
        return None
    value = int(code)
    if 0 <= value <= 84:
        label = "Under age 1" if value == 0 else f"Age {value}"
        return AgeInfo(value, value, value, 0, label, "single_year")
    if value == 85:
        return AgeInfo(85, 85, None, 1, "85+", "single_year")
    return None


def age_2023_single(code: str) -> AgeInfo | None:
    if code == "0001":
        return AgeInfo(0, 0, 0, 0, "Under age 1", "single_year")
    if code == "8599":
        return AgeInfo(85, 85, None, 1, "85+", "single_year")
    if len(code) == 4 and code.endswith("00") and code[:2].isdigit():
        value = int(code[:2])
        if 1 <= value <= 84:
            return AgeInfo(value, value, value, 0, f"Age {value}", "single_year")
        if value == 85:
            return AgeInfo(85, 85, 85, 0, "Age 85", "single_year")
    return None


def age_2024_csv_single(code: str) -> AgeInfo | None:
    # The downloadable Vintage 2024 CSV uses numeric age codes 0-85, with 85
    # representing 85 years and over.
    return age_2019_single(code)


def make_output_row(
    *,
    state_fips: str,
    state_name: str,
    year: int,
    age_info: AgeInfo,
    population: int,
    source_age_code: str,
    source_age_desc: str,
    sex: str,
    universe: str,
    source: str,
    source_date_code: str,
    source_date_desc: str,
    sex_desc: str | None = None,
) -> OutputRow:
    return OutputRow(
        state_fips=state_fips.zfill(2),
        state_name=state_name,
        year=year,
        age=age_info.age,
        age_start=age_info.age_start,
        age_end=age_info.age_end,
        age_topcoded=age_info.age_topcoded,
        age_label=age_info.age_label,
        age_granularity=age_info.age_granularity,
        population=population,
        source_age_code=source_age_code,
        source_age_desc=source_age_desc,
        sex=sex,
        sex_desc=sex_desc or SEX_LABELS.get(sex, sex),
        universe=universe,
        source=source,
        source_date_code=source_date_code,
        source_date_desc=source_date_desc,
    )


def query_1990_age_groups(
    *, api_key: str, sex: str, universe: str, start_year: int, end_year: int
) -> List[OutputRow]:
    if end_year < 1990 or start_year > 1999:
        return []
    if sex not in RACE_SEX_CODES_BY_SEX:
        raise RuntimeError("1990 age-group source supports sex codes 0, 1, or 2 only")

    wanted_years = set(range(max(start_year, 1990), min(end_year, 1999) + 1))
    allowed_race_sex = RACE_SEX_CODES_BY_SEX[sex]
    output: List[OutputRow] = []

    for state_fips, state_name in STATE_NAMES_51.items():
        print(f"Querying 1990 PEP county age groups for {state_name}...", file=sys.stderr)
        params = {
            "get": "POP,YEAR,AGEGRP,RACE_SEX,HISP",
            "for": "county:*",
            "in": f"state:{state_fips}",
            "key": api_key,
        }
        data = fetch_json(ENDPOINT_1990_GROUPS, params, f"1990 age groups {state_fips}")
        if not data:
            continue
        idx = header_index(data[0])
        totals: Dict[Tuple[int, str], int] = {}
        for values in data[1:]:
            race_sex = values[idx["RACE_SEX"]]
            hisp = values[idx["HISP"]]
            if race_sex not in allowed_race_sex or hisp not in {"1", "2"}:
                continue
            year = 1900 + int(values[idx["YEAR"]])
            if year not in wanted_years:
                continue
            age_code = values[idx["AGEGRP"]]
            if age_1990_group(age_code) is None:
                continue
            key = (year, age_code)
            totals[key] = totals.get(key, 0) + int(values[idx["POP"]])

        for (year, age_code), population in totals.items():
            age_info = age_1990_group(age_code)
            if age_info is None:
                continue
            output.append(
                make_output_row(
                    state_fips=state_fips,
                    state_name=state_name,
                    year=year,
                    age_info=age_info,
                    population=population,
                    source_age_code=age_code,
                    source_age_desc=age_info.age_label,
                    sex=sex,
                    universe=universe,
                    source="1990/pep/int_charagegroups",
                    source_date_code=str(year),
                    source_date_desc=f"7/1/{year} Population Estimate",
                )
            )
    return output


def query_2000_age_groups(
    *, api_key: str, sex: str, universe: str, start_year: int, end_year: int
) -> List[OutputRow]:
    if end_year < 2000 or start_year > 2009:
        return []
    output: List[OutputRow] = []
    wanted_date_codes = {
        str(year - 1998): year
        for year in range(max(start_year, 2000), min(end_year, 2009) + 1)
    }

    for date_code, year in sorted(wanted_date_codes.items(), key=lambda item: item[1]):
        print(f"Querying 2000 PEP state age groups for {year}...", file=sys.stderr)
        params = {
            "get": "GEONAME,POP,DATE_DESC,AGEGROUP,SEX,RACE,HISP",
            "DATE_": date_code,
            "for": "state:*",
            "key": api_key,
        }
        data = fetch_json(ENDPOINT_2000_GROUPS, params, f"2000 age groups {year}")
        if not data:
            continue
        idx = header_index(data[0])
        for values in data[1:]:
            if values[idx["SEX"]] != sex:
                continue
            if values[idx["RACE"]] != "0" or values[idx["HISP"]] != "0":
                continue
            age_code = values[idx["AGEGROUP"]]
            age_info = age_2000_group(age_code)
            if age_info is None:
                continue
            output.append(
                make_output_row(
                    state_fips=values[idx["state"]],
                    state_name=values[idx["GEONAME"]],
                    year=year,
                    age_info=age_info,
                    population=int(values[idx["POP"]]),
                    source_age_code=age_code,
                    source_age_desc=age_info.age_label,
                    sex=sex,
                    universe=universe,
                    source="2000/pep/int_charagegroups",
                    source_date_code=date_code,
                    source_date_desc=values[idx["DATE_DESC"]],
                )
            )
    return output


def query_2019_single_years(
    *, api_key: str, sex: str, universe: str, start_year: int, end_year: int
) -> List[OutputRow]:
    if end_year < 2010 or start_year > 2019:
        return []
    output: List[OutputRow] = []
    wanted_date_codes = {
        str(year - 2007): year
        for year in range(max(start_year, 2010), min(end_year, 2019) + 1)
    }

    for date_code, year in sorted(wanted_date_codes.items(), key=lambda item: item[1]):
        print(f"Querying 2019 PEP state single-year ages for {year}...", file=sys.stderr)
        params = {
            "get": "NAME,POP,AGE,AGE_DESC,SEX,DATE_CODE,DATE_DESC,UNIVERSE",
            "for": "state:*",
            "SEX": sex,
            "DATE_CODE": date_code,
            "RACE": "0",
            "HISP": "0",
            "key": api_key,
        }
        data = fetch_json(ENDPOINT_2019_SINGLE, params, f"2019 single-year ages {year}")
        if not data:
            continue
        idx = header_index(data[0])
        for values in data[1:]:
            age_code = values[idx["AGE"]]
            age_info = age_2019_single(age_code)
            if age_info is None:
                continue
            output.append(
                make_output_row(
                    state_fips=values[idx["state"]],
                    state_name=values[idx["NAME"]],
                    year=year,
                    age_info=age_info,
                    population=int(values[idx["POP"]]),
                    source_age_code=age_code,
                    source_age_desc=values[idx["AGE_DESC"]],
                    sex=sex,
                    sex_desc=SEX_LABELS.get(sex),
                    universe=values[idx.get("UNIVERSE", -1)] if "UNIVERSE" in idx else universe,
                    source="2019/pep/charage",
                    source_date_code=date_code,
                    source_date_desc=values[idx["DATE_DESC"]],
                )
            )
    return output


def query_2023_single_years(
    *,
    api_key: str,
    sex: str,
    universe: str,
    month: str,
    start_year: int,
    end_year: int,
) -> List[OutputRow]:
    if end_year < 2020 or start_year > 2023:
        return []
    output: List[OutputRow] = []
    for year in range(max(start_year, 2020), min(end_year, 2023) + 1):
        print(f"Querying 2023 PEP CHARV state single-year ages for {year}...", file=sys.stderr)
        params = {
            "get": "NAME,POP,AGE,AGE_DESC,SEX,SEX_DESC,YEAR",
            "for": "state:*",
            "YEAR": str(year),
            "MONTH": month,
            "SEX": sex,
            "UNIVERSE": universe,
            "POPGROUP": "001",
            "HISP": "0",
            "key": api_key,
        }
        data = fetch_json(ENDPOINT_2023_SINGLE, params, f"2023 CHARV {year}")
        if not data:
            continue
        idx = header_index(data[0])
        for values in data[1:]:
            age_code = values[idx["AGE"]]
            age_info = age_2023_single(age_code)
            if age_info is None:
                continue
            output.append(
                make_output_row(
                    state_fips=values[idx["state"]],
                    state_name=values[idx["NAME"]],
                    year=year,
                    age_info=age_info,
                    population=int(values[idx["POP"]]),
                    source_age_code=age_code,
                    source_age_desc=values[idx["AGE_DESC"]],
                    sex=sex,
                    sex_desc=values[idx["SEX_DESC"]],
                    universe=universe,
                    source="2023/pep/charv",
                    source_date_code=f"{year}-M{month}",
                    source_date_desc=f"7/1/{year} Population Estimate" if month == "7" else f"MONTH={month} {year}",
                )
            )
    return output


def query_2024_single_years_from_csv(
    *, sex: str, universe: str, start_year: int, end_year: int
) -> List[OutputRow]:
    if end_year < 2024 or start_year > 2024:
        return []

    print("Querying Vintage 2024 PEP downloadable state single-year ages...", file=sys.stderr)
    req = Request(
        PEP_2024_ALLDATA6_CSV_URL,
        headers={"User-Agent": "pep-state-age-ingest/1.0"},
    )
    totals: Dict[Tuple[str, str, str], int] = {}

    with urlopen(req, timeout=300) as response:
        text = io.TextIOWrapper(response, encoding="utf-8-sig", errors="replace")
        reader = csv.DictReader(text)
        for row in reader:
            if row.get("SUMLEV") != "040":
                continue
            if row.get("SEX") != sex:
                continue
            # ORIGIN=0 is all Hispanic origins. The all-race total is not a row
            # in alldata6, so sum race categories 1-6 within ORIGIN=0.
            if row.get("ORIGIN") != "0":
                continue
            age_code = row.get("AGE", "")
            if age_2024_csv_single(age_code) is None:
                continue
            pop_raw = row.get("POPESTIMATE2024", "")
            if not pop_raw.isdigit():
                continue
            key = (row["STATE"].zfill(2), row["NAME"], age_code)
            totals[key] = totals.get(key, 0) + int(pop_raw)

    output: List[OutputRow] = []
    for (state_fips, state_name, age_code), population in totals.items():
        age_info = age_2024_csv_single(age_code)
        if age_info is None:
            continue
        output.append(
            make_output_row(
                state_fips=state_fips,
                state_name=state_name,
                year=2024,
                age_info=age_info,
                population=population,
                source_age_code=age_code,
                source_age_desc=age_info.age_label,
                sex=sex,
                sex_desc=SEX_LABELS.get(sex),
                universe=universe,
                source="2024 PEP sc-est2024-alldata6.csv",
                source_date_code="2024",
                source_date_desc="7/1/2024 Population Estimate",
            )
        )
    return output


def sort_rows(rows: Iterable[OutputRow]) -> List[OutputRow]:
    return sorted(
        rows,
        key=lambda row: (
            row.state_fips,
            row.year,
            row.age_start,
            -1 if row.age_end is None else row.age_end,
            row.age_granularity,
        ),
    )


def validate(rows: List[OutputRow], start_year: int, end_year: int, single_year_only: bool) -> None:
    if not rows:
        raise RuntimeError("No rows returned")

    duplicate_key_counts: Dict[Tuple[str, int, str, int, int | None, str], int] = {}
    for row in rows:
        key = (
            row.state_fips,
            row.year,
            row.age_granularity,
            row.age_start,
            row.age_end,
            row.source,
        )
        duplicate_key_counts[key] = duplicate_key_counts.get(key, 0) + 1
    duplicates = [key for key, count in duplicate_key_counts.items() if count > 1]
    if duplicates:
        raise RuntimeError(f"Duplicate output keys detected: {duplicates[:20]}")

    observed_years = {row.year for row in rows}
    expected_years = set(range(start_year, end_year + 1))
    if single_year_only:
        expected_years &= set(range(2010, 2025))
    else:
        expected_years &= set(range(1990, 2025))
    if observed_years != expected_years:
        raise RuntimeError(
            f"Unexpected year coverage. missing={sorted(expected_years - observed_years)}, "
            f"extra={sorted(observed_years - expected_years)}"
        )

    counts: Dict[Tuple[str, int, str], int] = {}
    for row in rows:
        key = (row.state_fips, row.year, row.age_granularity)
        counts[key] = counts.get(key, 0) + 1

    bad_counts = []
    for (state, year, granularity), count in counts.items():
        if granularity == "single_year" and count != 86:
            bad_counts.append((state, year, granularity, count, 86))
        elif granularity == "age_group":
            expected = 19 if year < 2000 else 18
            if count != expected:
                bad_counts.append((state, year, granularity, count, expected))
    if bad_counts:
        raise RuntimeError(f"Unexpected age-row counts: {bad_counts[:20]}")


def write_parquet(rows: List[OutputRow], output: Path) -> None:
    pl = require_polars()
    write_rows_parquet(
        [row.as_dict() for row in rows],
        output,
        schema_overrides={
            "state_fips": pl.Utf8,
            "state_name": pl.Utf8,
            "year": pl.Int64,
            "age": pl.Int64,
            "age_start": pl.Int64,
            "age_end": pl.Int64,
            "age_topcoded": pl.Int64,
            "age_label": pl.Utf8,
            "age_granularity": pl.Utf8,
            "population": pl.Int64,
            "source_age_code": pl.Utf8,
            "source_age_desc": pl.Utf8,
            "sex": pl.Utf8,
            "sex_desc": pl.Utf8,
            "universe": pl.Utf8,
            "source": pl.Utf8,
            "source_date_code": pl.Utf8,
            "source_date_desc": pl.Utf8,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR)
    parser.add_argument("--end-year", type=int, default=DEFAULT_END_YEAR)
    parser.add_argument(
        "--sex",
        default="0",
        choices=sorted(SEX_LABELS),
        help="Census sex code. Default 0 = both sexes; use 2 for female.",
    )
    parser.add_argument(
        "--universe",
        default="R",
        help="Population universe for endpoints that expose it. Default R = resident.",
    )
    parser.add_argument(
        "--month",
        default="7",
        help="Month code for 2023/pep/charv. Default 7 = July 1 annual estimate.",
    )
    parser.add_argument(
        "--single-year-only",
        action="store_true",
        help="Only query 2010-2024 sources with single-year state ages.",
    )
    parser.add_argument(
        "--api-key",
        help="Census API key. Defaults to CENSUS_API_KEY or local config/api_key.ps1.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("processed_data/census_pep_state_year_age_1990_2024.parquet"),
        help="Output parquet path.",
    )
    args = parser.parse_args()

    if args.start_year > args.end_year:
        parser.error("--start-year cannot be after --end-year")

    needs_api_key = (
        (not args.single_year_only and args.start_year <= 2009 and args.end_year >= 1990)
        or (args.start_year <= 2023 and args.end_year >= 2010)
    )
    api_key = resolve_api_key(args.api_key) if needs_api_key else ""
    rows: List[OutputRow] = []

    if not args.single_year_only:
        rows.extend(
            query_1990_age_groups(
                api_key=api_key,
                sex=args.sex,
                universe=args.universe,
                start_year=args.start_year,
                end_year=args.end_year,
            )
        )
        rows.extend(
            query_2000_age_groups(
                api_key=api_key,
                sex=args.sex,
                universe=args.universe,
                start_year=args.start_year,
                end_year=args.end_year,
            )
        )

    rows.extend(
        query_2019_single_years(
            api_key=api_key,
            sex=args.sex,
            universe=args.universe,
            start_year=args.start_year,
            end_year=args.end_year,
        )
    )
    rows.extend(
        query_2023_single_years(
            api_key=api_key,
            sex=args.sex,
            universe=args.universe,
            month=args.month,
            start_year=args.start_year,
            end_year=args.end_year,
        )
    )
    rows.extend(
        query_2024_single_years_from_csv(
            sex=args.sex,
            universe=args.universe,
            start_year=args.start_year,
            end_year=args.end_year,
        )
    )

    rows = sort_rows(rows)
    validate(rows, args.start_year, args.end_year, args.single_year_only)
    write_parquet(rows, args.output)
    print(f"Wrote {len(rows):,} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
