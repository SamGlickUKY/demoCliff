#!/usr/bin/env python3
"""Build a state-year births dataset from CDC WONDER Natality tables.

CDC WONDER's XML API currently rejects state/county groupings for natality
(vital statistics) databases. To produce the requested state-year file, this
script uses the same CDC WONDER request form endpoints and CSV export action
that the browser UI uses after accepting the data-use restrictions.

The script uses only the Python standard library.
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
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener

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
    with opener.open(req, timeout=300) as response:
        return response.read()


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


def query_dataset(dataset: WonderDataset) -> List[dict]:
    opener, defaults, action_url = get_request_form(dataset)
    query_params = configure_births_query(defaults, dataset)
    csv_bytes = request(opener, action_url, flatten(query_params))
    return parse_wonder_csv(csv_bytes, dataset.db)


def write_csv(rows: List[dict], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda row: (row["state_code"], row["year"]))
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["state", "state_code", "year", "births", "source_db"]
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/births_state_year_1995_2024.csv"),
        help="Output CSV path.",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=15.0,
        help="Polite pause between WONDER export requests.",
    )
    args = parser.parse_args()

    all_rows: List[dict] = []
    for index, dataset in enumerate(NATALITY_DATASETS):
        if index and args.pause_seconds > 0:
            time.sleep(args.pause_seconds)
        print(
            f"Querying {dataset.db} ({dataset.start_year}-{dataset.end_year})...",
            file=sys.stderr,
        )
        all_rows.extend(query_dataset(dataset))

    expected_years = set(range(1995, 2025))
    observed_years = {row["year"] for row in all_rows}
    if observed_years != expected_years:
        missing = sorted(expected_years - observed_years)
        extra = sorted(observed_years - expected_years)
        raise RuntimeError(f"Unexpected year coverage. missing={missing}, extra={extra}")

    write_csv(all_rows, args.output)
    print(f"Wrote {len(all_rows):,} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
