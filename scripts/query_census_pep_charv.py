#!/usr/bin/env python3
"""Compatibility wrapper for the broadened Census PEP state-age ingest.

Use scripts/query_census_pep_state_age.py for the implementation.
"""

from __future__ import annotations

from query_census_pep_state_age import main


if __name__ == "__main__":
    raise SystemExit(main())
