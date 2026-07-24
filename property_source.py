"""
property_source.py
------------------
Single source of truth for the property roster used by hfs-scraper,
properties-data, and rooms-data.

Reads the separation-layer "properties_sync" tab **live on every run** (which is
fed from the confidential "The Hive" master tab via IMPORTRANGE+QUERY — the service
account never touches The Hive), applies the canonical selection rule, normalises
brand codes, validates the result, and returns a clean list of properties.

Canonical rule
--------------
A row is a property to scrape iff:
    - column D (Type)  == "Property"
    - column A (Brand) is one of the 6 brands (HFS, PSL, ESL, UVSL, USL, EVO)

Data taken per row: A=brand, B=name, C=url  (City in col L is kept too).

Public API
----------
    from property_source import get_properties
    props = get_properties()      # -> [{'brand','name','url','city'}, ...]

CLI (prove-out / report)
------------------------
    py -3 property_source.py                 # live read + validation report
    py -3 property_source.py --csv FILE.csv  # offline read from a CSV export
    py -3 property_source.py --json          # also dump the list as JSON

Auth (live path)
----------------
Uses a dedicated read-only Google service account. Point PROPERTIES_SA_JSON at the
key file (default: ./service_account.json) and share "The Hive" (read-only) with the
service account's email. The key is a secret — keep it OUT of git.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

# --- Source of truth -------------------------------------------------------
# We read a SEPARATION-LAYER sheet, not the confidential "The Hive" directly.
# The "properties_sync" tab pulls only Brand/Property/URL/Type/City for
# Type=='Property' rows from The Hive via IMPORTRANGE+QUERY, so the service
# account never has access to The Hive itself.
SHEET_ID   = "1gnSbSo2yEc4kSTwEDeyaz8XK4ASucM0Bb5JaChqXf_8"   # intermediary sheet
MASTER_TAB = "properties_sync"
READ_RANGE = "A:E"             # Brand, Property, URL, Type, City

# Sheet brand code -> internal code used in brands.py across the projects
BRAND_MAP = {
    "HFS":  "hfs",
    "PSL":  "psl",
    "ESL":  "esl",
    "UVSL": "usvl",            # sheet spells it UVSL; code uses usvl
    "USL":  "usl",
    "EVO":  "evo",
}

# Column indexes (0-based) within READ_RANGE (intermediary layout A..E)
COL_BRAND, COL_NAME, COL_URL, COL_TYPE = 0, 1, 2, 3
COL_CITY = 4                   # column E in the intermediary

# --- Validation guard ------------------------------------------------------
MIN_ROWS      = 150            # hard floor: below this the sheet is "suspect"
MIN_FRACTION  = 0.90          # and must be >= 90% of the last-good cached count

CACHE_FILE = Path(os.getenv("PROPERTIES_CACHE",
                            Path(__file__).with_name("properties_cache.json")))
SA_JSON    = os.getenv("PROPERTIES_SA_JSON",
                       str(Path(__file__).with_name("service_account.json")))


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def _fetch_rows_live() -> list[list[str]]:
    """Read the master tab live via the Google Sheets API (service account)."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:                       # pragma: no cover
        raise RuntimeError(
            "Live read needs 'gspread' and 'google-auth'. "
            "Install: pip install gspread google-auth"
        ) from exc

    if not Path(SA_JSON).exists():
        raise RuntimeError(
            f"Service-account key not found at {SA_JSON}. Set PROPERTIES_SA_JSON, "
            f"and share 'The Hive' (read-only) with the service account's email."
        )

    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds  = Credentials.from_service_account_file(SA_JSON, scopes=scopes)
    gc     = gspread.authorize(creds)
    ws     = gc.open_by_key(SHEET_ID).worksheet(MASTER_TAB)
    return ws.get_values(READ_RANGE)


def _fetch_rows_from_csv(path: str) -> list[list[str]]:
    """Read rows from a CSV export of the master tab (offline / testing)."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    return list(csv.reader(io.StringIO(raw)))


# ---------------------------------------------------------------------------
# Rule + normalisation
# ---------------------------------------------------------------------------
def _cell(row: list[str], i: int) -> str:
    return row[i].strip() if len(row) > i else ""


def apply_rule(rows: list[list[str]]) -> list[dict[str, str]]:
    """Filter to real properties in the 6 brands and normalise brand codes."""
    out: list[dict[str, str]] = []
    for row in rows:
        brand_raw = _cell(row, COL_BRAND)
        if _cell(row, COL_TYPE) != "Property":
            continue
        code = BRAND_MAP.get(brand_raw.upper())
        if not code:
            continue
        out.append({
            "brand": code,
            "name":  _cell(row, COL_NAME),
            "url":   _cell(row, COL_URL),
            "city":  _cell(row, COL_CITY),
        })
    out.sort(key=lambda p: (p["brand"], p["name"].lower()))
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate(props: list[dict[str, str]], cache_count: int | None) -> list[str]:
    """Return a list of problems. Empty list == healthy."""
    problems: list[str] = []
    n = len(props)

    if n < MIN_ROWS:
        problems.append(f"row count {n} is below hard floor {MIN_ROWS}")
    if cache_count and n < cache_count * MIN_FRACTION:
        problems.append(
            f"row count {n} dropped >{int((1-MIN_FRACTION)*100)}% vs last good "
            f"({cache_count})"
        )

    # every brand should have at least one property
    by_brand: dict[str, int] = {}
    for p in props:
        by_brand[p["brand"]] = by_brand.get(p["brand"], 0) + 1
    for code in set(BRAND_MAP.values()):
        if by_brand.get(code, 0) == 0:
            problems.append(f"brand '{code}' has 0 properties")

    # required fields present + duplicate detection
    seen_url: set[str] = set()
    for p in props:
        if not p["name"] or not p["url"]:
            problems.append(f"row missing name/url: {p}")
        key = p["url"].rstrip("/").lower()
        if key in seen_url:
            problems.append(f"duplicate url: {p['url']}")
        seen_url.add(key)
    return problems


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def _load_cache() -> list[dict[str, str]] | None:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
    return None


def _save_cache(props: list[dict[str, str]]) -> None:
    CACHE_FILE.write_text(json.dumps(props, indent=2, ensure_ascii=False),
                          encoding="utf-8")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def get_properties(csv_path: str | None = None) -> list[dict[str, str]]:
    """
    Return the canonical property list, read live from the sheet (or from a CSV
    export if csv_path is given). On a suspect/failed read, fall back to the last
    good cache and warn loudly — never returns a truncated list silently.
    """
    cache = _load_cache()
    cache_count = len(cache) if cache else None

    # Offline/test override: PROPERTIES_CSV points at a CSV export of the master
    # tab. Lets the pipeline run without the service account (CI, local testing).
    csv_path = csv_path or os.getenv("PROPERTIES_CSV") or None

    try:
        rows = _fetch_rows_from_csv(csv_path) if csv_path else _fetch_rows_live()
        props = apply_rule(rows)
    except Exception as exc:
        if cache:
            print(f"WARNING: live read failed ({exc}); using cached list "
                  f"({cache_count} properties).", file=sys.stderr)
            return cache
        raise

    problems = validate(props, cache_count)
    if problems:
        print("WARNING: property list failed validation:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        if cache:
            print(f"Using cached list ({cache_count} properties) instead.",
                  file=sys.stderr)
            return cache
        print("No cache to fall back to — refusing to proceed.", file=sys.stderr)
        raise RuntimeError("property list failed validation and no cache exists")

    _save_cache(props)
    return props


# ---------------------------------------------------------------------------
# CLI report
# ---------------------------------------------------------------------------
def _report(props: list[dict[str, str]], cache: list[dict[str, str]] | None) -> None:
    from collections import Counter
    by = Counter(p["brand"] for p in props)
    print("=" * 52)
    print("  PROPERTY SOURCE — validation report")
    print("=" * 52)
    for code in sorted(by):
        print(f"  {code:<6} {by[code]:>4}")
    print("-" * 52)
    print(f"  {'TOTAL':<6} {len(props):>4}")

    if cache:
        prev = {p["url"].rstrip("/").lower() for p in cache}
        cur  = {p["url"].rstrip("/").lower() for p in props}
        added   = [p for p in props if p["url"].rstrip("/").lower() not in prev]
        removed = [p for p in cache if p["url"].rstrip("/").lower() not in cur]
        print("-" * 52)
        print(f"  changes vs last cache: +{len(added)} / -{len(removed)}")
        for p in added[:10]:
            print(f"    + {p['brand']:<5} {p['name']}")
        for p in removed[:10]:
            print(f"    - {p['brand']:<5} {p['name']}")
    print("=" * 52)


def main() -> int:
    ap = argparse.ArgumentParser(description="Read/validate the master property list")
    ap.add_argument("--csv", help="read from a CSV export instead of the live sheet")
    ap.add_argument("--json", action="store_true", help="dump the list as JSON")
    args = ap.parse_args()

    cache_before = _load_cache()
    props = get_properties(csv_path=args.csv)
    _report(props, cache_before)
    if args.json:
        print(json.dumps(props, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
