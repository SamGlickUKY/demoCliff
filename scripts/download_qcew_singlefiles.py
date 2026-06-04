#!/usr/bin/env python3
"""Download BLS QCEW quarterly singlefile ZIPs for a range of years.

BLS URL pattern:
https://data.bls.gov/cew/data/files/{year}/csv/{year}_qtrly_singlefile.zip
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = "https://data.bls.gov/cew/data/files/{year}/csv/{year}_qtrly_singlefile.zip"
USER_AGENT = "Mozilla/5.0 qcew-singlefile-downloader/1.0"
CHUNK_SIZE = 1024 * 1024


def qcew_url(year: int) -> str:
    return BASE_URL.format(year=year)


def remote_size(url: str) -> int | None:
    req = Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=60) as response:
        value = response.headers.get("Content-Length")
        return int(value) if value and value.isdigit() else None


def download(url: str, output_path: Path, overwrite: bool, retries: int) -> bool:
    size = remote_size(url)
    if output_path.exists() and not overwrite:
        if size is None or output_path.stat().st_size == size:
            print(f"Skipping {output_path.name}; already downloaded.")
            return False
        print(
            f"Re-downloading {output_path.name}; local size "
            f"{output_path.stat().st_size:,} != remote size {size:,}."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = output_path.with_suffix(output_path.suffix + ".part")

    for attempt in range(1, retries + 1):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=120) as response, part_path.open("wb") as handle:
                downloaded = 0
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)

            if size is not None and part_path.stat().st_size != size:
                raise RuntimeError(
                    f"downloaded {part_path.stat().st_size:,} bytes; expected {size:,}"
                )
            part_path.replace(output_path)
            print(f"Downloaded {output_path.name} ({output_path.stat().st_size:,} bytes).")
            return True
        except (HTTPError, URLError, RuntimeError, TimeoutError) as exc:
            print(f"Attempt {attempt}/{retries} failed for {url}: {exc}")
            if attempt == retries:
                raise
            time.sleep(2 * attempt)

    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=1990)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--output-dir", type=Path, default=Path("raw_data"))
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args()

    for year in range(args.start_year, args.end_year + 1):
        url = qcew_url(year)
        output_path = args.output_dir / f"{year}_qtrly_singlefile.zip"
        print(f"{year}: {url}")
        download(url, output_path, overwrite=args.overwrite, retries=args.retries)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
