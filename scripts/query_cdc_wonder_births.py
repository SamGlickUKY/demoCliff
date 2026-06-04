#!/usr/bin/env python3
"""Build state-year birth datasets from CDC WONDER Natality tables.

CDC WONDER's XML API currently rejects state/county groupings for natality
(vital statistics) databases. To produce the requested state-year files, this
script uses the same CDC WONDER request form endpoints and CSV export action
that the browser UI uses after accepting the data-use restrictions.

Outputs include all-birth state-year totals and state-year counts by maternal
age group and race/ethnicity (non-Hispanic White, non-Hispanic Black,
Hispanic, non-Hispanic Asian/Pacific Islander, and non-Hispanic American
Indian/Alaska Native where WONDER does not suppress small cells). Race source
metadata is included because D66 switches from bridged race to single race for
2020-forward national natality files.

Raw WONDER exports are parsed at the API boundary; reusable outputs are written as parquet.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Dict, Iterable, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener

from parquet_io import require_polars, write_rows_parquet

BASE_URL = "https://wonder.cdc.gov"
USER_AGENT = "Mozilla/5.0 (compatible; cdc-wonder-births-script/1.0)"


@dataclass(frozen=True)
class WonderDataset:
    db: str
    page: str
    start_year: int
    end_year: int


NATALITY_DATASETS = [
    WonderDataset("D10", "natality-v2002.html", 1995, 2002),
    WonderDataset("D27", "natality-v2006.html", 2003, 2006),
    WonderDataset("D66", "natality-current.html", 2007, 2024),
]

AGE_GROUPS = ("15-19", "20-24", "25-29", "30-34", "35-39", "40-44")


@dataclass(frozen=True)
class AgeRaceCategory:
    code: str
    label: str


AGE_RACE_CATEGORIES = (
    AgeRaceCategory("nh_white", "Non-Hispanic White"),
    AgeRaceCategory("nh_black", "Non-Hispanic Black"),
    AgeRaceCategory("hispanic", "Hispanic"),
    AgeRaceCategory("nh_api", "Non-Hispanic Asian/Pacific Islander"),
    AgeRaceCategory("nh_aian", "Non-Hispanic American Indian/Alaska Native"),
)


@dataclass(frozen=True)
class AgeRaceQuerySpec:
    dataset: WonderDataset
    category: AgeRaceCategory
    hispanic_variable: str | None = None
    hispanic_values: Tuple[str, ...] = ()
    race_variable: str | None = None
    race_values: Tuple[str, ...] = ()
    race_source: str = ""


D10_HISPANIC_VALUES = ("2148-5", "2180-8", "2182-4", "4", "5")
D10_API_VALUES = ("2034-7", "2036-2", "2076-8", "2039-6", "2028-9")

RACE_VALUES = {
    "aian": ("1002-5",),
    "api_bridged": ("A-PI",),
    "asian_or_nhopi_single": ("A", "NHOPI"),
    "black": ("2054-5",),
    "white": ("2106-3",),
}


class WonderFormParser(HTMLParser):
    """Extract default form fields and the session-specific form action."""

    def __init__(self) -> None:
        super().__init__()
        self.params: Dict[str, List[str]] = {}
        self.form_action: str | None = None
        self._select: dict | None = None
        self._textarea_name: str | None = None
        self._textarea_text: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, str | None]]) -> None:
        attr = {k: (v if v is not None else "") for k, v in attrs}

        if tag == "form" and attr.get("id") == "wonderform":
            self.form_action = attr.get("action")
            return

        if tag == "input":
            name = attr.get("name")
            input_type = attr.get("type", "text").lower()
            if not name or input_type in {"submit", "button", "reset"}:
                return
            if input_type in {"checkbox", "radio"}:
                if "checked" in attr:
                    self.params.setdefault(name, []).append(attr.get("value", "on"))
            else:
                self.params.setdefault(name, []).append(attr.get("value", ""))
            return

        if tag == "select" and attr.get("name"):
            self._select = {
                "name": attr["name"],
                "multiple": "multiple" in attr,
                "options": [],
            }
            return

        if tag == "option" and self._select is not None:
            self._select["options"].append(
                (attr.get("value", ""), "selected" in attr)
            )
            return

        if tag == "textarea" and attr.get("name"):
            self._textarea_name = attr["name"]
            self._textarea_text = []

    def handle_data(self, data: str) -> None:
        if self._textarea_name is not None:
            self._textarea_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "select" and self._select is not None:
            options = self._select["options"]
            values = [value for value, selected in options if selected]
            if not values and not self._select["multiple"] and options:
                values = [options[0][0]]
            self.params[self._select["name"]] = values
            self._select = None
            return

        if tag == "textarea" and self._textarea_name is not None:
            self.params.setdefault(self._textarea_name, []).append(
                "".join(self._textarea_text)
            )
            self._textarea_name = None
            self._textarea_text = []


def request(opener, url: str, data: Iterable[Tuple[str, str]] | None = None) -> bytes:
    encoded = None if data is None else urlencode(list(data), doseq=True).encode("utf-8")
    req = Request(
        url,
        data=encoded,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": url,
        },
    )

    for attempt in range(3):
        try:
            with opener.open(req, timeout=300) as response:
                return response.read()
        except HTTPError as exc:
            retryable = exc.code in {429, 500, 502, 503, 504}
            if not retryable or attempt == 2:
                raise
            time.sleep(10 * (attempt + 1))
        except (TimeoutError, URLError):
            if attempt == 2:
                raise
            time.sleep(10 * (attempt + 1))

    raise RuntimeError("unreachable request retry state")


def get_request_form(dataset: WonderDataset) -> Tuple[object, Dict[str, List[str]], str]:
    opener = build_opener(HTTPCookieProcessor(CookieJar()))

    # Visit the public landing page, then accept the data-use restrictions.
    request(opener, urljoin(BASE_URL, dataset.page))
    form_html = request(
        opener,
        urljoin(BASE_URL, f"/controller/datarequest/{dataset.db}"),
        data=[("stage", "about"), ("saved_id", ""), ("action-I Agree", "I Agree")],
    ).decode("utf-8", errors="ignore")

    parser = WonderFormParser()
    parser.feed(form_html)
    if not parser.form_action:
        raise RuntimeError(f"Could not find WONDER form action for {dataset.db}")

    return opener, parser.params, urljoin(BASE_URL, parser.form_action)


def configure_births_query(
    params: Dict[str, List[str]], dataset: WonderDataset
) -> Dict[str, List[str]]:
    db = dataset.db
    years = [str(year) for year in range(dataset.start_year, dataset.end_year + 1)]

    params = {key: list(value) for key, value in params.items()}

    # Group by maternal residence state and birth year.
    params["B_1"] = [f"{db}.V21-level1"]
    params["B_2"] = [f"{db}.V20"]
    params["B_3"] = ["*None*"]
    params["B_4"] = ["*None*"]
    params["B_5"] = ["*None*"]

    # Select all states and the years covered by this WONDER database.
    params[f"F_{db}.V21"] = ["*All*"]
    params[f"V_{db}.V21"] = [""]
    params[f"I_{db}.V21"] = ["*All* (The United States)\n"]
    params["O_location"] = [f"{db}.V21"]
    params[f"V_{db}.V20"] = years

    # Keep only the Births measure.
    for key in list(params):
        if key.startswith("M_") and key != "M_1":
            del params[key]
    params["M_1"] = [f"{db}.M1"]

    # Export CSV and omit total rows.
    params.pop("O_show_totals", None)
    params["O_export-format"] = ["csv"]
    params["O_change_action-Send-Export Results"] = ["Export Results"]
    params["O_timeout"] = ["600"]
    params["O_title"] = [f"Births by state and year, {dataset.start_year}-{dataset.end_year}"]
    params["action-Send"] = ["Send"]

    return params


def age_race_query_specs() -> List[AgeRaceQuerySpec]:
    """Return the WONDER requests needed for consistent age-by-race counts."""

    category = {item.code: item for item in AGE_RACE_CATEGORIES}
    specs: List[AgeRaceQuerySpec] = []

    d10 = WonderDataset("D10", "natality-v2002.html", 1995, 2002)
    specs.extend(
        [
            AgeRaceQuerySpec(
                d10,
                category["nh_white"],
                hispanic_variable="V4",
                hispanic_values=("6",),
                race_source="D10 mother's Hispanic-origin race category",
            ),
            AgeRaceQuerySpec(
                d10,
                category["nh_black"],
                hispanic_variable="V4",
                hispanic_values=("7",),
                race_source="D10 mother's Hispanic-origin race category",
            ),
            AgeRaceQuerySpec(
                d10,
                category["hispanic"],
                hispanic_variable="V4",
                hispanic_values=D10_HISPANIC_VALUES,
                race_source="D10 mother's Hispanic origin components",
            ),
            AgeRaceQuerySpec(
                d10,
                category["nh_api"],
                hispanic_variable="V4",
                hispanic_values=("8",),
                race_variable="V2",
                race_values=D10_API_VALUES,
                race_source="D10 mother's race within non-Hispanic other races",
            ),
            AgeRaceQuerySpec(
                d10,
                category["nh_aian"],
                hispanic_variable="V4",
                hispanic_values=("8",),
                race_variable="V2",
                race_values=RACE_VALUES["aian"],
                race_source="D10 mother's race within non-Hispanic other races",
            ),
        ]
    )

    d27 = WonderDataset("D27", "natality-v2006.html", 2003, 2006)
    for code, race_values in [
        ("nh_white", RACE_VALUES["white"]),
        ("nh_black", RACE_VALUES["black"]),
        ("nh_api", RACE_VALUES["api_bridged"]),
        ("nh_aian", RACE_VALUES["aian"]),
    ]:
        specs.append(
            AgeRaceQuerySpec(
                d27,
                category[code],
                hispanic_variable="V43",
                hispanic_values=("2186-5",),
                race_variable="V2",
                race_values=race_values,
                race_source="bridged race",
            )
        )
    specs.append(
        AgeRaceQuerySpec(
            d27,
            category["hispanic"],
            hispanic_variable="V43",
            hispanic_values=("2135-2",),
            race_source="all races",
        )
    )

    # Bridged race remains available in D66 through 2019. Beginning in 2020,
    # WONDER no longer includes bridged race in the national birth file, so the
    # current-period requests use single race and aggregate Asian plus NHOPI.
    for d66, race_var, api_values, race_source in [
        (
            WonderDataset("D66", "natality-current.html", 2007, 2013),
            "V2",
            RACE_VALUES["api_bridged"],
            "bridged race",
        ),
        (
            WonderDataset("D66", "natality-current.html", 2014, 2019),
            "V2",
            RACE_VALUES["api_bridged"],
            "bridged race",
        ),
        (
            WonderDataset("D66", "natality-current.html", 2020, 2024),
            "V42",
            RACE_VALUES["asian_or_nhopi_single"],
            "single race",
        ),
    ]:
        for code, race_values in [
            ("nh_white", RACE_VALUES["white"]),
            ("nh_black", RACE_VALUES["black"]),
            ("nh_api", api_values),
            ("nh_aian", RACE_VALUES["aian"]),
        ]:
            specs.append(
                AgeRaceQuerySpec(
                    d66,
                    category[code],
                    hispanic_variable="V43",
                    hispanic_values=("2186-5",),
                    race_variable=race_var,
                    race_values=race_values,
                    race_source=race_source,
                )
            )
        specs.append(
            AgeRaceQuerySpec(
                d66,
                category["hispanic"],
                hispanic_variable="V43",
                hispanic_values=("2135-2",),
                race_source="all races",
            )
        )

    return specs


def configure_age_race_births_query(
    params: Dict[str, List[str]], spec: AgeRaceQuerySpec
) -> Dict[str, List[str]]:
    db = spec.dataset.db
    years = [str(year) for year in range(spec.dataset.start_year, spec.dataset.end_year + 1)]

    params = {key: list(value) for key, value in params.items()}

    # Group by maternal residence state, birth year, and six requested maternal
    # age groups. Race/ethnicity is filtered per request and labeled in output.
    params["B_1"] = [f"{db}.V21-level1"]
    params["B_2"] = [f"{db}.V20"]
    params["B_3"] = [f"{db}.V1"]
    params["B_4"] = ["*None*"]
    params["B_5"] = ["*None*"]

    params[f"F_{db}.V21"] = ["*All*"]
    params[f"V_{db}.V21"] = [""]
    params[f"I_{db}.V21"] = ["*All* (The United States)\n"]
    params["O_location"] = [f"{db}.V21"]
    params[f"V_{db}.V20"] = years
    params[f"V_{db}.V1"] = list(AGE_GROUPS)
    if "O_age" in params:
        params["O_age"] = [f"{db}.V1"]

    if spec.hispanic_variable:
        params[f"V_{db}.{spec.hispanic_variable}"] = list(spec.hispanic_values)
    if spec.race_variable:
        params[f"V_{db}.{spec.race_variable}"] = list(spec.race_values)

    for key in list(params):
        if key.startswith("M_") and key != "M_1":
            del params[key]
    params["M_1"] = [f"{db}.M1"]

    params.pop("O_show_totals", None)
    params["O_show_zeros"] = ["true"]
    params["O_show_suppressed"] = ["true"]
    params["O_export-format"] = ["csv"]
    params["O_change_action-Send-Export Results"] = ["Export Results"]
    params["O_timeout"] = ["600"]
    params["O_title"] = [
        f"Births by state, year, age, and race/ethnicity: "
        f"{spec.category.label}, {spec.dataset.start_year}-{spec.dataset.end_year}"
    ]
    params["action-Send"] = ["Send"]

    return params


def flatten(params: Dict[str, List[str]]) -> List[Tuple[str, str]]:
    return [(key, value) for key, values in params.items() for value in values]


def parse_wonder_csv(csv_bytes: bytes, source_db: str) -> List[dict]:
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows: List[dict] = []

    for row in reader:
        state_code = (row.get("State Code") or "").strip()
        year_code = (row.get("Year Code") or "").strip()
        births = (row.get("Births") or "").strip().replace(",", "")

        # Skip notes/footer lines in WONDER exports.
        if not (state_code.isdigit() and year_code.isdigit() and births.isdigit()):
            continue

        rows.append(
            {
                "state": (row.get("State") or "").strip(),
                "state_code": state_code,
                "year": int(year_code),
                "births": int(births),
                "source_db": source_db,
            }
        )

    return rows


def parse_wonder_age_race_csv(csv_bytes: bytes, spec: AgeRaceQuerySpec) -> List[dict]:
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows: List[dict] = []

    for row in reader:
        state_code = (row.get("State Code") or "").strip()
        year_code = (row.get("Year Code") or "").strip()
        age_code = (
            row.get("Age of Mother 9 Code")
            or row.get("Age of Mother Code")
            or row.get("Age of Mother")
            or ""
        ).strip()
        births_raw = (row.get("Births") or "").strip().replace(",", "")

        # Skip notes/footer lines in WONDER exports.
        if not (state_code.isdigit() and year_code.isdigit() and age_code in AGE_GROUPS):
            continue

        suppressed = births_raw.lower() == "suppressed"
        if births_raw.isdigit():
            births = int(births_raw)
        elif suppressed:
            births = None
        else:
            continue

        rows.append(
            {
                "state": (row.get("State") or "").strip(),
                "state_code": state_code,
                "year": int(year_code),
                "age_group": (
                    row.get("Age of Mother 9")
                    or row.get("Age of Mother")
                    or age_code
                ).strip(),
                "age_group_code": age_code,
                "race_ethnicity": spec.category.code,
                "race_ethnicity_label": spec.category.label,
                "births": births,
                "suppressed": int(suppressed),
                "source_db": spec.dataset.db,
                "source_race_variable": spec.race_variable or "",
                "source_hispanic_variable": spec.hispanic_variable or "",
                "source_race_definition": spec.race_source,
            }
        )

    return rows


def query_dataset(dataset: WonderDataset) -> List[dict]:
    opener, defaults, action_url = get_request_form(dataset)
    query_params = configure_births_query(defaults, dataset)
    csv_bytes = request(opener, action_url, flatten(query_params))
    return parse_wonder_csv(csv_bytes, dataset.db)


def query_age_race_dataset(spec: AgeRaceQuerySpec) -> List[dict]:
    opener, defaults, action_url = get_request_form(spec.dataset)
    query_params = configure_age_race_births_query(defaults, spec)
    csv_bytes = request(opener, action_url, flatten(query_params))
    return parse_wonder_age_race_csv(csv_bytes, spec)


def write_parquet(rows: List[dict], output: Path) -> None:
    pl = require_polars()
    write_rows_parquet(
        sorted(rows, key=lambda row: (row["state_code"], row["year"])),
        output,
        schema_overrides={
            "state": pl.Utf8,
            "state_code": pl.Utf8,
            "year": pl.Int64,
            "births": pl.Int64,
            "source_db": pl.Utf8,
        },
    )


def validate_age_race_grid(rows: List[dict], expected_years: set[int]) -> None:
    state_codes = {row["state_code"] for row in rows}
    if len(state_codes) != 51:
        raise RuntimeError(f"Expected 51 state/DC codes, observed {len(state_codes)}")

    categories = {item.code for item in AGE_RACE_CATEGORIES}
    observed = {
        (row["state_code"], row["year"], row["age_group_code"], row["race_ethnicity"])
        for row in rows
    }
    expected = {
        (state_code, year, age_group, category)
        for state_code in state_codes
        for year in expected_years
        for age_group in AGE_GROUPS
        for category in categories
    }
    if observed != expected:
        missing = sorted(expected - observed)[:20]
        extra = sorted(observed - expected)[:20]
        raise RuntimeError(
            "Unexpected age-race grid coverage. "
            f"missing_first20={missing}, extra_first20={extra}"
        )

    if len(rows) != len(observed):
        raise RuntimeError(
            f"Duplicate age-race rows detected: {len(rows)} rows, "
            f"{len(observed)} unique state/year/age/category keys"
        )


def write_age_race_parquet(rows: List[dict], output: Path) -> None:
    pl = require_polars()
    age_order = {age: index for index, age in enumerate(AGE_GROUPS)}
    category_order = {item.code: index for index, item in enumerate(AGE_RACE_CATEGORIES)}
    sorted_rows = sorted(
        rows,
        key=lambda row: (
            row["state_code"],
            row["year"],
            age_order[row["age_group_code"]],
            category_order[row["race_ethnicity"]],
        ),
    )
    write_rows_parquet(
        sorted_rows,
        output,
        schema_overrides={
            "state": pl.Utf8,
            "state_code": pl.Utf8,
            "year": pl.Int64,
            "age_group": pl.Utf8,
            "age_group_code": pl.Utf8,
            "race_ethnicity": pl.Utf8,
            "race_ethnicity_label": pl.Utf8,
            "births": pl.Int64,
            "suppressed": pl.Int64,
            "source_db": pl.Utf8,
            "source_race_variable": pl.Utf8,
            "source_hispanic_variable": pl.Utf8,
            "source_race_definition": pl.Utf8,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("processed_data/births_state_year_1995_2024.parquet"),
        help="Output parquet path for all-birth state-year totals.",
    )
    parser.add_argument(
        "--age-race-output",
        type=Path,
        default=Path("processed_data/births_state_year_age_race_1995_2024.parquet"),
        help="Output parquet path for state-year-age-race/ethnicity birth counts.",
    )
    parser.add_argument(
        "--skip-total",
        action="store_true",
        help="Do not rebuild the all-birth state-year totals file.",
    )
    parser.add_argument(
        "--skip-age-race",
        action="store_true",
        help="Do not build the state-year-age-race/ethnicity file.",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=15.0,
        help="Polite pause between WONDER export requests.",
    )
    args = parser.parse_args()

    if args.skip_total and args.skip_age_race:
        parser.error("nothing to do: both --skip-total and --skip-age-race were set")

    expected_years = set(range(1995, 2025))
    request_count = 0

    if not args.skip_total:
        all_rows: List[dict] = []
        for dataset in NATALITY_DATASETS:
            if request_count and args.pause_seconds > 0:
                time.sleep(args.pause_seconds)
            request_count += 1
            print(
                f"Querying {dataset.db} ({dataset.start_year}-{dataset.end_year}) totals...",
                file=sys.stderr,
            )
            all_rows.extend(query_dataset(dataset))

        observed_years = {row["year"] for row in all_rows}
        if observed_years != expected_years:
            missing = sorted(expected_years - observed_years)
            extra = sorted(observed_years - expected_years)
            raise RuntimeError(
                f"Unexpected total-birth year coverage. missing={missing}, extra={extra}"
            )

        write_parquet(all_rows, args.output)
        print(f"Wrote {len(all_rows):,} rows to {args.output}")

    if not args.skip_age_race:
        age_race_rows: List[dict] = []
        for spec in age_race_query_specs():
            if request_count and args.pause_seconds > 0:
                time.sleep(args.pause_seconds)
            request_count += 1
            print(
                f"Querying {spec.dataset.db} ({spec.dataset.start_year}-{spec.dataset.end_year}) "
                f"{spec.category.label} by age...",
                file=sys.stderr,
            )
            age_race_rows.extend(query_age_race_dataset(spec))

        validate_age_race_grid(age_race_rows, expected_years)

        write_age_race_parquet(age_race_rows, args.age_race_output)
        print(f"Wrote {len(age_race_rows):,} rows to {args.age_race_output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
