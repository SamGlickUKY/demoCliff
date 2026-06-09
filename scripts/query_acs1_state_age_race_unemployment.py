#!/usr/bin/env python3
"""Query ACS 1-year state age-race unemployment rates, 2005-2024.

This script uses ACS 1-year Detailed Tables B23002A through B23002I.  For
2020, it attempts the standard ACS 1-year endpoint and skips the year if the
standard endpoint/tables are unavailable; it does not use the Census 2020 ACS
1-year experimental release.

Default outputs:
  processed_data/acs1_state_age_race_unemployment_2005_2024.parquet
  results/acs1_state_age_race_unemployment_2005_2024.log

A Census API key is read from --api-key, CENSUS_API_KEY, or config/api_keys.
The local legacy config/api_key.ps1 format is also accepted as a fallback.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping
from urllib.parse import urlencode

ACS1_ENDPOINT_TEMPLATE = "https://api.census.gov/data/{year}/acs/acs1"
DEFAULT_START_YEAR = 2005
DEFAULT_END_YEAR = 2024
DEFAULT_OUTPUT = Path(
    "processed_data/acs1_state_age_race_unemployment_2005_2024.parquet"
)
DEFAULT_LOG = Path("results/acs1_state_age_race_unemployment_2005_2024.log")
DEFAULT_API_KEY_FILE = Path("config/api_keys")
LEGACY_API_KEY_FILE = Path("config/api_key.ps1")
USER_AGENT = "acs1-state-age-race-unemployment/1.0"

RACE_TABLES: Mapping[str, str] = {
    "B23002A": "White alone",
    "B23002B": "Black or African American alone",
    "B23002C": "American Indian and Alaska Native alone",
    "B23002D": "Asian alone",
    "B23002E": "Native Hawaiian and Other Pacific Islander alone",
    "B23002F": "Some other race alone",
    "B23002G": "Two or more races",
    "B23002H": "White alone, not Hispanic or Latino",
    "B23002I": "Hispanic or Latino",
}

EXPECTED_AGE_GROUPS = (
    "16 to 19 years",
    "20 to 24 years",
    "25 to 54 years",
    "55 to 64 years",
    "65 to 69 years",
    "70 years and over",
)
AGE_GROUP_ORDER = {age_group: idx for idx, age_group in enumerate(EXPECTED_AGE_GROUPS)}
EXPECTED_SEXES = ("Male", "Female")
EXPECTED_STATUSES = ("employed", "unemployed")
OUTPUT_COLUMNS = (
    "year",
    "state_fips",
    "state_name",
    "race_table",
    "race_ethnicity",
    "age_group",
    "employed",
    "unemployed",
    "civilian_labor_force",
    "unemployment_rate",
)


class CensusAPIError(RuntimeError):
    """Raised when a Census API request fails or returns unusable data."""


@dataclass(frozen=True)
class ParsedVariable:
    variable: str
    sex: str
    age_group: str
    status: str  # "employed" or "unemployed"


@dataclass
class RunLog:
    failed_calls: List[str] = field(default_factory=list)
    missing_variables: List[str] = field(default_factory=list)
    missing_estimate_values: Dict[str, int] = field(default_factory=dict)
    zero_denominator_cells: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def write(self, path: Path, *, output_path: Path, row_count: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: List[str] = []
        lines.append("ACS 1-year state age-race unemployment ingest log")
        lines.append(f"Run completed UTC: {datetime.now(timezone.utc).isoformat()}")
        lines.append(f"Output parquet: {output_path}")
        lines.append(f"Output rows: {row_count:,}")
        lines.append("")

        sections = [
            ("Notes", self.notes),
            ("Failed year-table API calls", self.failed_calls),
            ("Missing variables", self.missing_variables),
        ]
        for title, entries in sections:
            lines.append(title)
            lines.append("-" * len(title))
            if entries:
                lines.extend(f"- {entry}" for entry in entries)
            else:
                lines.append("- None")
            lines.append("")

        title = "Missing estimate values"
        lines.append(title)
        lines.append("-" * len(title))
        if self.missing_estimate_values:
            for key, count in sorted(self.missing_estimate_values.items()):
                lines.append(f"- {key}: {count} null/non-numeric estimate values")
        else:
            lines.append("- None")
        lines.append("")

        title = "Zero-denominator cells"
        lines.append(title)
        lines.append("-" * len(title))
        if self.zero_denominator_cells:
            lines.extend(f"- {entry}" for entry in self.zero_denominator_cells)
        else:
            lines.append("- None")
        lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")


def require_requests_session() -> Any:
    """Create a requests.Session, failing with an actionable message if needed."""

    try:
        import requests  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "This script requires requests. Install it with "
            "`python -m pip install requests`."
        ) from exc

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def require_polars() -> Any:
    """Import polars for parquet writing, with a clear error if unavailable."""

    try:
        import polars as pl  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "This script requires polars for parquet output. Install it with "
            "`python -m pip install polars`."
        ) from exc
    return pl


def parse_api_key_text(text: str) -> str | None:
    """Extract a Census API key from JSON, key=value, PowerShell, or raw text."""

    stripped = text.strip()
    if not stripped:
        return None

    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            for key_name in ("CENSUS_API_KEY", "census_api_key", "api_key", "key"):
                value = parsed.get(key_name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        except json.JSONDecodeError:
            pass

    patterns = [
        r"(?:^|[\s;$])\$?env:CENSUS_API_KEY\s*=\s*['\"]([^'\"]+)['\"]",
        r"(?:^|\n)\s*(?:CENSUS_API_KEY|census_api_key|api_key|key)\s*[:=]\s*['\"]?([^'\"\s#]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, stripped, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()

    # If the file contains only a bare token, use it.
    if "\n" not in stripped and "=" not in stripped and ":" not in stripped:
        return stripped

    return None


def api_key_from_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return parse_api_key_text(path.read_text(encoding="utf-8", errors="ignore"))


def resolve_api_key(cli_key: str | None, api_key_file: Path) -> str:
    """Resolve the Census API key without ever printing it."""

    key = (
        cli_key
        or os.environ.get("CENSUS_API_KEY")
        or api_key_from_file(api_key_file)
        or api_key_from_file(LEGACY_API_KEY_FILE)
    )
    if not key:
        raise RuntimeError(
            "Census API key required. Pass --api-key, set CENSUS_API_KEY, "
            f"or create {api_key_file}."
        )
    return key.strip()


def build_api_url(year: int, table_id: str | None = None) -> str:
    """Build the standard ACS 1-year data or variable-metadata URL."""

    base = ACS1_ENDPOINT_TEMPLATE.format(year=year)
    if table_id is None:
        return base
    return f"{base}/groups/{table_id}.json"


def redacted_query(params: Mapping[str, Any]) -> str:
    safe_params = {key: value for key, value in params.items() if key.lower() != "key"}
    return urlencode(safe_params)


def request_json(
    session: Any,
    url: str,
    params: Mapping[str, Any],
    *,
    label: str,
    timeout: int,
    retries: int,
) -> Any:
    """GET JSON with retries for transient Census API failures."""

    safe_call = f"{url}?{redacted_query(params)}" if params else url
    last_error: str | None = None

    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
        except Exception as exc:  # requests exception types depend on optional import
            last_error = f"request exception: {exc}"
            if attempt < retries:
                time.sleep(5 * attempt)
                continue
            raise CensusAPIError(f"{label} failed for {safe_call}: {last_error}") from exc

        if response.status_code in {429, 500, 502, 503, 504} and attempt < retries:
            last_error = f"HTTP {response.status_code}: {response.text[:500]}"
            time.sleep(10 * attempt)
            continue

        if not response.ok:
            raise CensusAPIError(
                f"{label} failed for {safe_call}: "
                f"HTTP {response.status_code}: {response.text[:1_000]}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise CensusAPIError(
                f"{label} returned non-JSON for {safe_call}: "
                f"{response.text[:1_000]}"
            ) from exc

    raise CensusAPIError(f"{label} failed for {safe_call}: {last_error}")


def fetch_table_metadata(
    session: Any,
    *,
    year: int,
    table_id: str,
    api_key: str,
    timeout: int,
    retries: int,
) -> Mapping[str, Any]:
    """Fetch variable metadata for one ACS detailed table."""

    url = build_api_url(year, table_id)
    params = {"key": api_key}
    data = request_json(
        session,
        url,
        params,
        label=f"metadata year={year} table={table_id}",
        timeout=timeout,
        retries=retries,
    )
    if not isinstance(data, Mapping) or not isinstance(data.get("variables"), Mapping):
        raise CensusAPIError(f"metadata year={year} table={table_id} missing variables")
    return data


def fetch_table_data(
    session: Any,
    *,
    year: int,
    table_id: str,
    api_key: str,
    timeout: int,
    retries: int,
) -> List[List[str]]:
    """Fetch state data with get=NAME,group(table_id)&for=state:* exactly."""

    url = build_api_url(year)
    params = {"get": f"NAME,group({table_id})", "for": "state:*", "key": api_key}
    data = request_json(
        session,
        url,
        params,
        label=f"data year={year} table={table_id}",
        timeout=timeout,
        retries=retries,
    )
    if not isinstance(data, list) or not data or not isinstance(data[0], list):
        raise CensusAPIError(f"data year={year} table={table_id} returned no table rows")
    return data


def clean_label_part(part: str) -> str:
    return re.sub(r"\s+", " ", part.strip().strip(":"))


def parse_acs_label(label: str) -> tuple[str, str, str] | None:
    """Parse an ACS B23002 label into sex, age group, and labor-force status.

    Returns None for totals, Armed Forces rows, civilian subtotal rows, and
    not-in-labor-force rows.  The retained rows are the estimate variables for
    employed and unemployed people in the civilian labor force.  For older age
    groups, ACS labels omit the word "Civilian" because Armed Forces rows are
    not present; those employed/unemployed rows are still included.
    """

    parts = [clean_label_part(part) for part in label.split("!!")]
    parts = [part for part in parts if part]
    if not parts:
        return None

    normalized = [part.lower() for part in parts]
    if "in armed forces" in normalized or "armed forces" in normalized:
        return None
    if "in labor force" not in normalized:
        return None

    final_part = normalized[-1]
    if final_part == "employed":
        status = "employed"
    elif final_part == "unemployed":
        status = "unemployed"
    else:
        return None

    sex_index = None
    sex = None
    for idx, part in enumerate(parts):
        if part in EXPECTED_SEXES:
            sex_index = idx
            sex = part
            break
    if sex_index is None or sex is None:
        return None

    labor_index = normalized.index("in labor force")
    if labor_index <= sex_index + 1:
        return None

    age_group = " ".join(parts[sex_index + 1 : labor_index]).strip()
    if not age_group:
        return None

    return sex, age_group, status


def is_estimate_variable(variable: str, table_id: str) -> bool:
    return re.fullmatch(rf"{re.escape(table_id)}_\d{{3}}E", variable) is not None


def identify_estimate_variables(
    metadata: Mapping[str, Any], *, year: int, table_id: str, run_log: RunLog
) -> Dict[str, ParsedVariable]:
    """Use metadata labels to find employed/unemployed estimate variables."""

    variables = metadata["variables"]
    parsed: Dict[str, ParsedVariable] = {}

    for variable, info in variables.items():
        if not isinstance(variable, str) or not is_estimate_variable(variable, table_id):
            continue
        if not isinstance(info, Mapping):
            continue
        label = str(info.get("label", ""))
        parsed_label = parse_acs_label(label)
        if parsed_label is None:
            continue
        sex, age_group, status = parsed_label
        parsed[variable] = ParsedVariable(
            variable=variable,
            sex=sex,
            age_group=age_group,
            status=status,
        )

    if not parsed:
        run_log.missing_variables.append(
            f"year={year} table={table_id}: no employed/unemployed estimate variables parsed"
        )
        return parsed

    present = {(item.sex, item.age_group, item.status) for item in parsed.values()}
    for sex in EXPECTED_SEXES:
        for age_group in EXPECTED_AGE_GROUPS:
            for status in EXPECTED_STATUSES:
                if (sex, age_group, status) not in present:
                    run_log.missing_variables.append(
                        f"year={year} table={table_id}: missing {sex} "
                        f"{age_group} {status} estimate variable"
                    )

    return parsed


def parse_census_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "null", "None", "N", "-", "**", "***", "(X)"}:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return None


def row_header_index(header: Iterable[str]) -> Dict[str, int]:
    """Map response columns to their first index."""

    index: Dict[str, int] = {}
    for idx, name in enumerate(header):
        index.setdefault(name, idx)
    return index


def aggregate_to_state_year_age_race(
    data: List[List[str]],
    parsed_variables: Mapping[str, ParsedVariable],
    *,
    year: int,
    table_id: str,
    race_ethnicity: str,
    run_log: RunLog,
) -> List[dict[str, Any]]:
    """Aggregate male and female counts to state-year-age-race unemployment rates."""

    header = data[0]
    index = row_header_index(header)
    required_geo = {"NAME", "state"}
    missing_geo = sorted(required_geo - set(index))
    if missing_geo:
        raise CensusAPIError(
            f"data year={year} table={table_id} missing geography columns {missing_geo}"
        )

    variables_in_header = {
        variable: parsed
        for variable, parsed in parsed_variables.items()
        if variable in index
    }
    for variable in sorted(set(parsed_variables) - set(variables_in_header)):
        run_log.missing_variables.append(
            f"year={year} table={table_id}: parsed variable {variable} absent from data response"
        )

    age_groups = sorted(
        {parsed.age_group for parsed in variables_in_header.values()},
        key=lambda value: (AGE_GROUP_ORDER.get(value, 999), value),
    )

    rows: List[dict[str, Any]] = []
    for values in data[1:]:
        state_name = values[index["NAME"]]
        state_fips = values[index["state"]].zfill(2)
        totals: Dict[str, Dict[str, int]] = {
            age_group: {"employed": 0, "unemployed": 0} for age_group in age_groups
        }
        missing_components: Dict[str, Dict[str, int]] = {
            age_group: {"employed": 0, "unemployed": 0} for age_group in age_groups
        }

        for variable, parsed in variables_in_header.items():
            count = parse_census_int(values[index[variable]])
            if count is None:
                missing_components[parsed.age_group][parsed.status] += 1
                key = f"year={year} table={table_id} state={state_fips}"
                run_log.missing_estimate_values[key] = (
                    run_log.missing_estimate_values.get(key, 0) + 1
                )
                continue
            totals[parsed.age_group][parsed.status] += count

        for age_group in age_groups:
            employed = (
                None
                if missing_components[age_group]["employed"]
                else totals[age_group]["employed"]
            )
            unemployed = (
                None
                if missing_components[age_group]["unemployed"]
                else totals[age_group]["unemployed"]
            )
            if employed is None or unemployed is None:
                civilian_labor_force = None
                unemployment_rate = None
            else:
                civilian_labor_force = employed + unemployed
                if civilian_labor_force > 0:
                    unemployment_rate = unemployed / civilian_labor_force
                else:
                    unemployment_rate = None
                    run_log.zero_denominator_cells.append(
                        f"year={year} state={state_fips} table={table_id} "
                        f"race={race_ethnicity} age_group={age_group}"
                    )

            rows.append(
                {
                    "year": year,
                    "state_fips": state_fips,
                    "state_name": state_name,
                    "race_table": table_id,
                    "race_ethnicity": race_ethnicity,
                    "age_group": age_group,
                    "employed": employed,
                    "unemployed": unemployed,
                    "civilian_labor_force": civilian_labor_force,
                    "unemployment_rate": unemployment_rate,
                }
            )

    return rows


def process_year_table(
    session: Any,
    *,
    year: int,
    table_id: str,
    race_ethnicity: str,
    api_key: str,
    timeout: int,
    retries: int,
    run_log: RunLog,
) -> List[dict[str, Any]]:
    """Fetch metadata and data for one year-table and return tidy rows."""

    try:
        metadata = fetch_table_metadata(
            session,
            year=year,
            table_id=table_id,
            api_key=api_key,
            timeout=timeout,
            retries=retries,
        )
    except CensusAPIError as exc:
        run_log.failed_calls.append(
            f"year={year} table={table_id} stage=metadata error={exc}"
        )
        raise

    parsed_variables = identify_estimate_variables(
        metadata, year=year, table_id=table_id, run_log=run_log
    )
    if not parsed_variables:
        return []

    try:
        data = fetch_table_data(
            session,
            year=year,
            table_id=table_id,
            api_key=api_key,
            timeout=timeout,
            retries=retries,
        )
    except CensusAPIError as exc:
        run_log.failed_calls.append(f"year={year} table={table_id} stage=data error={exc}")
        raise

    return aggregate_to_state_year_age_race(
        data,
        parsed_variables,
        year=year,
        table_id=table_id,
        race_ethnicity=race_ethnicity,
        run_log=run_log,
    )


def sort_output_rows(rows: Iterable[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            row["year"],
            row["state_fips"],
            row["race_table"],
            AGE_GROUP_ORDER.get(str(row["age_group"]), 999),
            row["age_group"],
        ),
    )


def write_final_parquet(rows: Iterable[Mapping[str, Any]], output_path: Path) -> int:
    """Write the final tidy dataset as parquet and return the row count."""

    pl = require_polars()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_rows = sort_output_rows(rows)

    schema_overrides = {
        "year": pl.Int64,
        "state_fips": pl.Utf8,
        "state_name": pl.Utf8,
        "race_table": pl.Utf8,
        "race_ethnicity": pl.Utf8,
        "age_group": pl.Utf8,
        "employed": pl.Int64,
        "unemployed": pl.Int64,
        "civilian_labor_force": pl.Int64,
        "unemployment_rate": pl.Float64,
    }
    if sorted_rows:
        df = pl.DataFrame(sorted_rows, schema_overrides=schema_overrides)
    else:
        df = pl.DataFrame(schema=schema_overrides)
    df = df.select(list(OUTPUT_COLUMNS))
    df.write_parquet(output_path)
    return df.height


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Query ACS 1-year Detailed Tables B23002A-I and write state-year-age-race "
            "unemployment rates to parquet."
        )
    )
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR)
    parser.add_argument("--end-year", type=int, default=DEFAULT_END_YEAR)
    parser.add_argument("--api-key", default=None, help="Census API key; otherwise read config/env.")
    parser.add_argument(
        "--api-key-file",
        type=Path,
        default=DEFAULT_API_KEY_FILE,
        help="Path to local Census API key file. Default: config/api_keys.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--timeout", type=int, default=300, help="HTTP timeout in seconds.")
    parser.add_argument("--retries", type=int, default=3, help="HTTP retry attempts.")
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.0,
        help="Optional pause after each successful year-table query.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.start_year > args.end_year:
        raise ValueError("--start-year cannot be greater than --end-year")
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    if args.retries <= 0:
        raise ValueError("--retries must be positive")
    if args.sleep_seconds < 0:
        raise ValueError("--sleep-seconds cannot be negative")

    # Fail early if required Python packages are not available.
    require_polars()
    session = require_requests_session()
    api_key = resolve_api_key(args.api_key, args.api_key_file)
    run_log = RunLog()
    rows: List[dict[str, Any]] = []

    for year in range(args.start_year, args.end_year + 1):
        print(f"Querying ACS 1-year unemployment tables for {year}...", file=sys.stderr)
        year_rows: List[dict[str, Any]] = []
        year_failed = False

        for table_id, race_ethnicity in RACE_TABLES.items():
            try:
                table_rows = process_year_table(
                    session,
                    year=year,
                    table_id=table_id,
                    race_ethnicity=race_ethnicity,
                    api_key=api_key,
                    timeout=args.timeout,
                    retries=args.retries,
                    run_log=run_log,
                )
                year_rows.extend(table_rows)
                if args.sleep_seconds:
                    time.sleep(args.sleep_seconds)
            except CensusAPIError:
                year_failed = True
                if year == 2020:
                    break
                continue
            except Exception as exc:
                year_failed = True
                run_log.failed_calls.append(
                    f"year={year} table={table_id} stage=unexpected error={exc}"
                )
                if year == 2020:
                    break
                continue

        if year == 2020 and year_failed:
            run_log.notes.append(
                "2020 skipped: standard 2020 ACS 1-year estimates are unavailable; "
                "Census released experimental 2020 ACS 1-year data only, and this "
                "script does not use experimental data or impute 2020."
            )
            continue
        if year == 2020 and not year_failed:
            run_log.notes.append(
                "2020 standard ACS 1-year endpoint returned all requested B23002A-I tables; "
                "included returned standard-endpoint data."
            )

        rows.extend(year_rows)

    row_count = write_final_parquet(rows, args.output)
    run_log.write(args.log, output_path=args.output, row_count=row_count)
    print(f"Wrote {row_count:,} rows to {args.output}")
    print(f"Wrote log to {args.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
