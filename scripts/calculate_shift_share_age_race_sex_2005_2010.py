#!/usr/bin/env python3
"""Calculate an ACS age-race-sex employment shift-share exposure by state.

For each state s, the main variable is:

    sum_g (state s employment share in age-race-sex group g in 2005
           * national 2005-2010 percent change in group g employment)

where g indexes ACS B23002 age, race, and sex bins.  The race bins use the
mutually exclusive ACS race-alone/two-or-more tables B23002A-G; Hispanic-origin
tables B23002H-I are intentionally omitted to avoid overlapping race/ethnicity
bins when forming state employment shares. Missing ACS fine-cell employment
estimates are treated as zero for sparse age-race-sex cells.

Default outputs:
  processed_data/state_shift_share_age_race_sex_2005_2010.parquet
  processed_data/state_shift_share_age_race_sex_2005_2010_components.parquet
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode

ACS1_ENDPOINT_TEMPLATE = "https://api.census.gov/data/{year}/acs/acs1"
DEFAULT_OUTPUT = Path("processed_data/state_shift_share_age_race_sex_2005_2010.parquet")
DEFAULT_COMPONENTS_OUTPUT = Path(
    "processed_data/state_shift_share_age_race_sex_2005_2010_components.parquet"
)
DEFAULT_API_KEY_FILE = Path("config/api_keys")
LEGACY_API_KEY_FILE = Path("config/api_key.ps1")
USER_AGENT = "age-race-sex-shift-share/1.0"

BASE_YEAR = 2005
END_YEAR = 2010
STATE_FIPS_51 = {
    "01", "02", "04", "05", "06", "08", "09", "10", "11", "12",
    "13", "15", "16", "17", "18", "19", "20", "21", "22", "23",
    "24", "25", "26", "27", "28", "29", "30", "31", "32", "33",
    "34", "35", "36", "37", "38", "39", "40", "41", "42", "44",
    "45", "46", "47", "48", "49", "50", "51", "53", "54", "55",
    "56",
}

STATE_ABBREV = {
    "Alabama": "AL",
    "Alaska": "AK",
    "Arizona": "AZ",
    "Arkansas": "AR",
    "California": "CA",
    "Colorado": "CO",
    "Connecticut": "CT",
    "Delaware": "DE",
    "District of Columbia": "DC",
    "Florida": "FL",
    "Georgia": "GA",
    "Hawaii": "HI",
    "Idaho": "ID",
    "Illinois": "IL",
    "Indiana": "IN",
    "Iowa": "IA",
    "Kansas": "KS",
    "Kentucky": "KY",
    "Louisiana": "LA",
    "Maine": "ME",
    "Maryland": "MD",
    "Massachusetts": "MA",
    "Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS",
    "Missouri": "MO",
    "Montana": "MT",
    "Nebraska": "NE",
    "Nevada": "NV",
    "New Hampshire": "NH",
    "New Jersey": "NJ",
    "New Mexico": "NM",
    "New York": "NY",
    "North Carolina": "NC",
    "North Dakota": "ND",
    "Ohio": "OH",
    "Oklahoma": "OK",
    "Oregon": "OR",
    "Pennsylvania": "PA",
    "Rhode Island": "RI",
    "South Carolina": "SC",
    "South Dakota": "SD",
    "Tennessee": "TN",
    "Texas": "TX",
    "Utah": "UT",
    "Vermont": "VT",
    "Virginia": "VA",
    "Washington": "WA",
    "West Virginia": "WV",
    "Wisconsin": "WI",
    "Wyoming": "WY",
}

# Mutually exclusive race-alone/two-or-more ACS B23002 tables.  B23002H/I are
# ethnicity tables and overlap with A-G, so they are not used for shares.
RACE_TABLES: Mapping[str, str] = {
    "B23002A": "White alone",
    "B23002B": "Black or African American alone",
    "B23002C": "American Indian and Alaska Native alone",
    "B23002D": "Asian alone",
    "B23002E": "Native Hawaiian and Other Pacific Islander alone",
    "B23002F": "Some other race alone",
    "B23002G": "Two or more races",
}
RACE_ORDER = {table: idx for idx, table in enumerate(RACE_TABLES)}
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
SEX_ORDER = {sex: idx for idx, sex in enumerate(EXPECTED_SEXES)}
GROUP_COLS = ["race_table", "race", "sex", "age_group"]


class CensusAPIError(RuntimeError):
    """Raised when a Census API request fails or returns unusable data."""


@dataclass(frozen=True)
class ParsedVariable:
    variable: str
    sex: str
    age_group: str


def require_requests_session() -> Any:
    try:
        import requests  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "This script requires requests. Install it with `python -m pip install requests`."
        ) from exc

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def require_polars() -> Any:
    try:
        import polars as pl  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "This script requires polars for parquet output. Install it with "
            "`python -m pip install polars`."
        ) from exc
    return pl


def find_repo_root() -> Path:
    starts = [Path.cwd(), Path(__file__).resolve().parent]
    for start in starts:
        current = start.resolve()
        while True:
            if (current / "AGENTS.MD").exists() and (current / "processed_data").is_dir():
                return current
            if current.parent == current:
                break
            current = current.parent
    return Path.cwd().resolve()


def parse_api_key_text(text: str) -> str | None:
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

    if "\n" not in stripped and "=" not in stripped and ":" not in stripped:
        return stripped

    return None


def api_key_from_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return parse_api_key_text(path.read_text(encoding="utf-8", errors="ignore"))


def resolve_api_key(cli_key: str | None, api_key_file: Path) -> str:
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
                f"{label} returned non-JSON for {safe_call}: {response.text[:1_000]}"
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
    data = request_json(
        session,
        build_api_url(year, table_id),
        {"key": api_key},
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
) -> list[list[str]]:
    data = request_json(
        session,
        build_api_url(year),
        {"get": f"NAME,group({table_id})", "for": "state:*", "key": api_key},
        label=f"data year={year} table={table_id}",
        timeout=timeout,
        retries=retries,
    )
    if not isinstance(data, list) or not data or not isinstance(data[0], list):
        raise CensusAPIError(f"data year={year} table={table_id} returned no table rows")
    return data


def clean_label_part(part: str) -> str:
    return re.sub(r"\s+", " ", part.strip().strip(":"))


def parse_acs_label(label: str) -> tuple[str, str] | None:
    """Parse a B23002 label into sex and age group for employed rows only."""

    parts = [clean_label_part(part) for part in label.split("!!")]
    parts = [part for part in parts if part]
    if not parts:
        return None

    normalized = [part.lower() for part in parts]
    if "in armed forces" in normalized or "armed forces" in normalized:
        return None
    if "in labor force" not in normalized:
        return None
    if normalized[-1] != "employed":
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
    if age_group not in AGE_GROUP_ORDER:
        return None

    return sex, age_group


def is_estimate_variable(variable: str, table_id: str) -> bool:
    return re.fullmatch(rf"{re.escape(table_id)}_\d{{3}}E", variable) is not None


def identify_employment_variables(metadata: Mapping[str, Any], table_id: str) -> dict[str, ParsedVariable]:
    variables = metadata["variables"]
    parsed: dict[str, ParsedVariable] = {}

    for variable, info in variables.items():
        if not isinstance(variable, str) or not is_estimate_variable(variable, table_id):
            continue
        if not isinstance(info, Mapping):
            continue
        parsed_label = parse_acs_label(str(info.get("label", "")))
        if parsed_label is None:
            continue
        sex, age_group = parsed_label
        parsed[variable] = ParsedVariable(variable=variable, sex=sex, age_group=age_group)

    present = {(item.sex, item.age_group) for item in parsed.values()}
    expected = {(sex, age_group) for sex in EXPECTED_SEXES for age_group in EXPECTED_AGE_GROUPS}
    missing = sorted(
        expected - present,
        key=lambda item: (SEX_ORDER[item[0]], AGE_GROUP_ORDER[item[1]]),
    )
    if missing:
        missing_text = ", ".join(f"{sex} {age}" for sex, age in missing)
        raise CensusAPIError(f"table={table_id}: missing employed estimate variables: {missing_text}")

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


def row_header_index(header: Iterable[str]) -> dict[str, int]:
    index: dict[str, int] = {}
    for idx, name in enumerate(header):
        index.setdefault(name, idx)
    return index


def parse_table_rows(
    data: list[list[str]],
    parsed_variables: Mapping[str, ParsedVariable],
    *,
    year: int,
    table_id: str,
    race: str,
) -> list[dict[str, Any]]:
    header = data[0]
    index = row_header_index(header)
    required_geo = {"NAME", "state"}
    missing_geo = sorted(required_geo - set(index))
    if missing_geo:
        raise CensusAPIError(
            f"data year={year} table={table_id} missing geography columns {missing_geo}"
        )

    rows: list[dict[str, Any]] = []
    for values in data[1:]:
        state_fips = values[index["state"]].zfill(2)
        if state_fips not in STATE_FIPS_51:
            continue
        state_name = values[index["NAME"]]

        for variable, parsed in parsed_variables.items():
            if variable not in index:
                raise CensusAPIError(
                    f"data year={year} table={table_id} missing parsed variable {variable}"
                )
            employed_raw = parse_census_int(values[index[variable]])
            employed_imputed_zero = employed_raw is None
            employed = 0 if employed_raw is None else employed_raw
            rows.append(
                {
                    "year": year,
                    "state_fips": state_fips,
                    "state": state_name,
                    "state_label": STATE_ABBREV[state_name],
                    "race_table": table_id,
                    "race": race,
                    "sex": parsed.sex,
                    "age_group": parsed.age_group,
                    "race_order": RACE_ORDER[table_id],
                    "sex_order": SEX_ORDER[parsed.sex],
                    "age_group_order": AGE_GROUP_ORDER[parsed.age_group],
                    "employed": employed,
                    "employment_imputed_zero": employed_imputed_zero,
                }
            )

    return rows


def fetch_age_race_sex_employment(
    *,
    api_key: str,
    timeout: int,
    retries: int,
) -> list[dict[str, Any]]:
    session = require_requests_session()
    rows: list[dict[str, Any]] = []

    for year in (BASE_YEAR, END_YEAR):
        for table_id, race in RACE_TABLES.items():
            metadata = fetch_table_metadata(
                session,
                year=year,
                table_id=table_id,
                api_key=api_key,
                timeout=timeout,
                retries=retries,
            )
            parsed_variables = identify_employment_variables(metadata, table_id)
            data = fetch_table_data(
                session,
                year=year,
                table_id=table_id,
                api_key=api_key,
                timeout=timeout,
                retries=retries,
            )
            rows.extend(
                parse_table_rows(
                    data,
                    parsed_variables,
                    year=year,
                    table_id=table_id,
                    race=race,
                )
            )
            print(f"Fetched year={year} table={table_id}")

    return rows


def sort_employment_df(df: Any) -> Any:
    return df.sort(["year", "state_fips", "race_order", "sex_order", "age_group_order"])


def calculate_shift_share(rows: list[dict[str, Any]]) -> tuple[Any, Any]:
    pl = require_polars()
    schema = {
        "year": pl.Int64,
        "state_fips": pl.Utf8,
        "state": pl.Utf8,
        "state_label": pl.Utf8,
        "race_table": pl.Utf8,
        "race": pl.Utf8,
        "sex": pl.Utf8,
        "age_group": pl.Utf8,
        "race_order": pl.Int64,
        "sex_order": pl.Int64,
        "age_group_order": pl.Int64,
        "employed": pl.Int64,
        "employment_imputed_zero": pl.Boolean,
    }
    employment = sort_employment_df(pl.DataFrame(rows, schema=schema))

    expected_rows = 2 * len(STATE_FIPS_51) * len(RACE_TABLES) * len(EXPECTED_SEXES) * len(EXPECTED_AGE_GROUPS)
    if employment.height != expected_rows:
        raise RuntimeError(f"Expected {expected_rows} employment rows; observed {employment.height}")

    rows_per_state_year = (
        employment.group_by(["state_fips", "year"])
        .len()
        .select(pl.col("len").min().alias("min"), pl.col("len").max().alias("max"))
        .row(0)
    )
    expected_groups = len(RACE_TABLES) * len(EXPECTED_SEXES) * len(EXPECTED_AGE_GROUPS)
    if rows_per_state_year != (expected_groups, expected_groups):
        raise RuntimeError(
            "Unexpected age-race-sex group counts per state-year: "
            f"min={rows_per_state_year[0]} max={rows_per_state_year[1]} expected={expected_groups}"
        )

    base = (
        employment.filter(pl.col("year") == BASE_YEAR)
        .rename({"employed": "state_group_employment_2005"})
        .select(
            [
                "state_fips",
                "state",
                "state_label",
                "race_table",
                "race",
                "sex",
                "age_group",
                "race_order",
                "sex_order",
                "age_group_order",
                "state_group_employment_2005",
                "employment_imputed_zero",
            ]
        )
        .rename({"employment_imputed_zero": "state_group_employment_2005_imputed_zero"})
    )
    state_totals = base.group_by("state_fips").agg(
        pl.sum("state_group_employment_2005").alias("total_age_race_sex_employment_2005")
    )
    base = base.join(state_totals, on="state_fips", how="left").with_columns(
        (
            pl.col("state_group_employment_2005")
            / pl.col("total_age_race_sex_employment_2005")
        ).alias("state_age_race_sex_employment_share_2005")
    )

    national = employment.group_by(GROUP_COLS).agg(
        pl.col("employed")
        .filter(pl.col("year") == BASE_YEAR)
        .sum()
        .alias("national_group_employment_2005"),
        pl.col("employed")
        .filter(pl.col("year") == END_YEAR)
        .sum()
        .alias("national_group_employment_2010"),
    )
    national = national.with_columns(
        (
            pl.col("national_group_employment_2010")
            - pl.col("national_group_employment_2005")
        ).alias("national_group_employment_change_2005_2010"),
        pl.when(pl.col("national_group_employment_2005") > 0)
        .then(
            100
            * (
                pl.col("national_group_employment_2010")
                / pl.col("national_group_employment_2005")
                - 1
            )
        )
        .otherwise(None)
        .alias("national_group_employment_pct_change_2005_2010"),
    )

    components = base.join(national, on=GROUP_COLS, how="left").with_columns(
        (
            pl.col("state_age_race_sex_employment_share_2005")
            * pl.col("national_group_employment_pct_change_2005_2010")
        ).alias("shift_share_age_race_sex_component_2005_2010"),
        (
            pl.col("state_age_race_sex_employment_share_2005")
            * pl.col("national_group_employment_change_2005_2010")
        ).alias("shift_share_age_race_sex_employment_change_component_2005_2010"),
    )

    shift_share = components.group_by(["state_fips", "state", "state_label"]).agg(
        pl.first("total_age_race_sex_employment_2005"),
        pl.sum("state_group_employment_2005").alias("covered_group_employment_2005"),
        pl.sum("state_age_race_sex_employment_share_2005").alias("age_race_sex_employment_share_sum_2005"),
        pl.col("state_age_race_sex_employment_share_2005")
        .is_not_null()
        .sum()
        .alias("age_race_sex_group_count_2005"),
        (
            pl.col("state_age_race_sex_employment_share_2005").is_not_null()
            & pl.col("national_group_employment_pct_change_2005_2010").is_not_null()
        )
        .sum()
        .alias("age_race_sex_group_count_with_national_change"),
        pl.sum("shift_share_age_race_sex_component_2005_2010").alias(
            "shift_share_age_race_sex_2005_2010"
        ),
        pl.sum("shift_share_age_race_sex_employment_change_component_2005_2010").alias(
            "shift_share_age_race_sex_employment_change_2005_2010"
        ),
    )

    national_total_2005 = int(
        employment.filter(pl.col("year") == BASE_YEAR).select(pl.sum("employed")).item()
    )
    national_total_2010 = int(
        employment.filter(pl.col("year") == END_YEAR).select(pl.sum("employed")).item()
    )
    national_total_change = national_total_2010 - national_total_2005
    national_total_pct_change = 100 * (national_total_2010 / national_total_2005 - 1)

    shift_share = shift_share.with_columns(
        pl.lit(len(RACE_TABLES) * len(EXPECTED_SEXES) * len(EXPECTED_AGE_GROUPS)).alias(
            "national_age_race_sex_group_count"
        ),
        pl.lit(national_total_2005).alias("national_age_race_sex_employment_2005"),
        pl.lit(national_total_2010).alias("national_age_race_sex_employment_2010"),
        pl.lit(national_total_change).alias("national_age_race_sex_employment_change_2005_2010"),
        pl.lit(national_total_pct_change).alias(
            "national_age_race_sex_employment_pct_change_2005_2010"
        ),
    ).select(
        [
            "state_fips",
            "state",
            "state_label",
            "total_age_race_sex_employment_2005",
            "covered_group_employment_2005",
            "age_race_sex_employment_share_sum_2005",
            "age_race_sex_group_count_2005",
            "age_race_sex_group_count_with_national_change",
            "national_age_race_sex_group_count",
            "national_age_race_sex_employment_2005",
            "national_age_race_sex_employment_2010",
            "national_age_race_sex_employment_change_2005_2010",
            "national_age_race_sex_employment_pct_change_2005_2010",
            "shift_share_age_race_sex_2005_2010",
            "shift_share_age_race_sex_employment_change_2005_2010",
        ]
    ).sort("state_fips")

    if shift_share.height != len(STATE_FIPS_51):
        raise RuntimeError(
            f"Expected {len(STATE_FIPS_51)} states/DC in shift-share output; "
            f"observed {shift_share.height}"
        )
    share_range = shift_share.select(
        pl.col("age_race_sex_employment_share_sum_2005").min().alias("share_min"),
        pl.col("age_race_sex_employment_share_sum_2005").max().alias("share_max"),
    ).row(0)
    if not all(abs(value - 1) < 1e-10 for value in share_range):
        raise RuntimeError(
            "State age-race-sex employment shares do not sum to one: "
            f"min={share_range[0]} max={share_range[1]}"
        )
    unique_shift_share = shift_share.select(
        pl.col("shift_share_age_race_sex_2005_2010").round(12).n_unique()
    ).item()
    if unique_shift_share == 1:
        raise RuntimeError("Shift-share variable has no state variation; check the formula")

    components = components.select(
        [
            "state_fips",
            "state",
            "state_label",
            "race_table",
            "race",
            "sex",
            "age_group",
            "race_order",
            "sex_order",
            "age_group_order",
            "state_group_employment_2005",
            "state_group_employment_2005_imputed_zero",
            "total_age_race_sex_employment_2005",
            "state_age_race_sex_employment_share_2005",
            "national_group_employment_2005",
            "national_group_employment_2010",
            "national_group_employment_change_2005_2010",
            "national_group_employment_pct_change_2005_2010",
            "shift_share_age_race_sex_component_2005_2010",
            "shift_share_age_race_sex_employment_change_component_2005_2010",
        ]
    ).sort(["state_fips", "race_order", "sex_order", "age_group_order"])

    return shift_share, components


def write_parquet(df: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calculate state age-race-sex employment shift-share exposure."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--components-output", type=Path, default=DEFAULT_COMPONENTS_OUTPUT)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--api-key-file", type=Path, default=DEFAULT_API_KEY_FILE)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=3)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = find_repo_root()
    output = args.output if args.output.is_absolute() else repo_root / args.output
    components_output = (
        args.components_output
        if args.components_output.is_absolute()
        else repo_root / args.components_output
    )
    api_key_file = (
        args.api_key_file if args.api_key_file.is_absolute() else repo_root / args.api_key_file
    )

    api_key = resolve_api_key(args.api_key, api_key_file)
    rows = fetch_age_race_sex_employment(
        api_key=api_key,
        timeout=args.timeout,
        retries=args.retries,
    )
    shift_share, components = calculate_shift_share(rows)

    write_parquet(shift_share, output)
    write_parquet(components, components_output)

    ss_min, ss_max = shift_share.select(
        pl.col("shift_share_age_race_sex_2005_2010").min().alias("ss_min"),
        pl.col("shift_share_age_race_sex_2005_2010").max().alias("ss_max"),
    ).row(0)
    share_min, share_max = shift_share.select(
        pl.col("age_race_sex_employment_share_sum_2005").min().alias("share_min"),
        pl.col("age_race_sex_employment_share_sum_2005").max().alias("share_max"),
    ).row(0)
    nat_pct = shift_share.select(
        pl.first("national_age_race_sex_employment_pct_change_2005_2010")
    ).item()
    imputed_count = components.select(pl.sum("state_group_employment_2005_imputed_zero")).item()

    print(f"Wrote {output}")
    print(f"Wrote {components_output}")
    print(f"National age-race-sex group count: {len(RACE_TABLES) * len(EXPECTED_SEXES) * len(EXPECTED_AGE_GROUPS)}")
    print(f"2005 state-group missing ACS employment cells imputed as zero: {imputed_count}")
    print(f"National age-race-sex employment percent change, 2005-2010: {nat_pct:.2f}")
    print(f"2005 state age-race-sex employment share sum range: {share_min:.4f} to {share_max:.4f}")
    print(f"Age-race-sex shift-share range: {ss_min:.4f} to {ss_max:.4f}")


if __name__ == "__main__":
    # Import polars once at module end so main diagnostics can use `pl`.
    pl = require_polars()
    main()
