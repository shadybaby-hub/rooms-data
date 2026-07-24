"""
rooms_discrepancy_report.py
---------------------------
Two-way check between rooms-data's fetched properties and the master list
(The Hive, via the separation-layer sheet / property_source):

    API_ONLY   property in the basepms/rooms data but NOT in The Hive  -> review / add
    HIVE_ONLY  property in The Hive but NOT in the rooms data           -> missing / block / name diff

rooms-data rows have no URL or slug — only brand_name, property, city — so matching
is by brand + normalised property name. Name-format differences (e.g. "en-suite" vs
"ensuite", apostrophes) can therefore show up as mismatches to reconcile in The Hive.

Writes:  rooms_discrepancy_report.csv
Wired into etl.py (runs after each ETL, non-fatal). Also runnable standalone:

    py -3 rooms_discrepancy_report.py [--rooms output/rooms_latest.csv]
    PROPERTIES_CSV=export.csv py -3 rooms_discrepancy_report.py   # offline master read
"""

from __future__ import annotations

import argparse
import csv
import os
import re
from collections import defaultdict

from property_source import get_properties

OUT_CSV = "rooms_discrepancy_report.csv"
MARKER  = "/student-accommodation/"

# rooms-data uses full brand names; map them to the master's brand codes.
NAME_TO_CODE = {
    "homes for students":       "hfs",
    "prestige student living":  "psl",
    "essential student living": "esl",
    "universal student living": "usvl",
    "urban student life":       "usl",
    "evo student":              "evo",
}
BRANDS = ["hfs", "psl", "esl", "usvl", "usl", "evo"]


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def brand_base(master_rows: list[dict]) -> str:
    for p in master_rows:
        i = p["url"].find(MARKER)
        if i >= 0:
            return p["url"][:i + len(MARKER)]
    return ""


def load_rooms(path: str) -> dict[str, dict[str, dict]]:
    """{brand_code: {normalised_name: {'name','city'}}} of distinct properties."""
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            code = NAME_TO_CODE.get(norm(r.get("brand_name", "")))
            if not code:
                continue
            name = r.get("property", "")
            out[code].setdefault(norm(name), {"name": name, "city": r.get("city", "")})
    return out


def generate(rooms_csv: str = os.path.join("output", "rooms_latest.csv"),
             out_csv: str = OUT_CSV) -> dict:
    master = get_properties()
    m_by_brand: dict[str, list[dict]] = defaultdict(list)
    for p in master:
        m_by_brand[p["brand"]].append(p)
    rooms = load_rooms(rooms_csv)

    rows_out: list[dict] = []
    print("=" * 74)
    print("  ROOMS vs MASTER — TWO-WAY DISCREPANCY REPORT")
    print(f"  (rooms source: {rooms_csv})")
    print("=" * 74)
    print(f"  {'brand':<6}{'master':>7}{'rooms':>7}{'matched':>9}"
          f"{'API_ONLY':>10}{'HIVE_ONLY':>11}")
    print("-" * 74)

    for brand in BRANDS:
        m    = m_by_brand.get(brand, [])
        rp   = rooms.get(brand, {})
        base = brand_base(m)
        m_names = {norm(p["name"]): p for p in m}

        matched = 0
        for nkey, info in rp.items():
            if nkey in m_names:
                matched += 1
            else:
                built = f"{base}{slugify(info['city'])}/{slugify(info['name'])}"
                rows_out.append({
                    "brand": brand, "direction": "API_ONLY",
                    "name": info["name"], "city": info["city"], "url": built,
                    "url_source": "constructed", "note": "in rooms data, NOT in The Hive",
                })
        for nkey, p in m_names.items():
            if nkey not in rp:
                rows_out.append({
                    "brand": brand, "direction": "HIVE_ONLY",
                    "name": p["name"], "city": p["city"], "url": p["url"],
                    "url_source": "master", "note": "in The Hive, NOT in rooms data",
                })

        api_only = sum(1 for r in rows_out if r["brand"] == brand and r["direction"] == "API_ONLY")
        hive_only = sum(1 for r in rows_out if r["brand"] == brand and r["direction"] == "HIVE_ONLY")
        print(f"  {brand:<6}{len(m):>7}{len(rp):>7}{matched:>9}{api_only:>10}{hive_only:>11}")

    print("-" * 74)
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["brand", "direction", "name", "city",
                                          "url", "url_source", "note"])
        w.writeheader()
        w.writerows(rows_out)

    api_only = [r for r in rows_out if r["direction"] == "API_ONLY"]
    hive_only = [r for r in rows_out if r["direction"] == "HIVE_ONLY"]
    print(f"  wrote {len(rows_out)} discrepancy rows -> {out_csv}")
    if api_only:
        print("\n  In rooms data but NOT in The Hive (review / add):")
        for r in api_only:
            print(f"    - [{r['brand']}] {r['name']}  [{r['city']}]")
    if hive_only:
        print("\n  In The Hive but NOT in rooms data (missing / name diff / block?):")
        for r in hive_only:
            print(f"    - [{r['brand']}] {r['name']}  [{r['city']}]")
    return {"discrepancies": len(rows_out),
            "api_only": len(api_only), "hive_only": len(hive_only)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rooms", default=os.path.join("output", "rooms_latest.csv"),
                    help="path to the rooms CSV to check")
    args = ap.parse_args()
    generate(rooms_csv=args.rooms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
