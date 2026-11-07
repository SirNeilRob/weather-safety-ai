"""Offline ZIP -> coordinates lookup.

Source: US Census Bureau 2024 Gazetteer, ZIP Code Tabulation Areas
(2024_Gaz_zcta_national.txt, public domain), reduced to ``zip,lat,lon`` from
the GEOID, INTPTLAT and INTPTLONG columns and gzipped into
``app/data/zcta.csv.gz``. The file has no city names; those come from NWS.
"""

from __future__ import annotations

import csv
import gzip
from functools import lru_cache
from pathlib import Path

ZCTA_PATH = Path(__file__).resolve().parent.parent / "data" / "zcta.csv.gz"

# USPS assigns Florida the 320xx-349xx range. Checked against the Census file:
# all 1013 ZCTAs starting 32/33/34 lie inside Florida's lat/lon bounds.
FLORIDA_PREFIXES = ("32", "33", "34")


@lru_cache(maxsize=1)
def _table() -> dict[str, tuple[float, float]]:
    with gzip.open(ZCTA_PATH, "rt", encoding="utf-8", newline="") as handle:
        return {row["zip"]: (float(row["lat"]), float(row["lon"])) for row in csv.DictReader(handle)}


def normalize(zip_code: str) -> str | None:
    value = str(zip_code or "").strip()
    return value if len(value) == 5 and value.isdigit() else None


def is_florida(zip_code: str) -> bool:
    return zip_code.startswith(FLORIDA_PREFIXES)


def lookup(zip_code: str) -> tuple[float, float] | None:
    """Census internal point for a ZIP, or None if the Census has no such ZCTA."""
    return _table().get(zip_code)
