"""
Multi-Brand Student Living — Room Database Builder
===================================================
Fetches basepms-rooms from 6 brand APIs and publishes all data
into two combined CSVs sorted A-Z by property (column B):

  • rooms_<timestamp>.csv      — one row per room type
  • contracts_<timestamp>.csv  — one row per contract / pricing option

Brands covered:
  - Prestige Student Living     api.prestigestudentliving.com
  - Urban Student Life          api.urbanstudentlife.com
  - Evo Student                 api.evostudent.com
  - Universal Student Living    api.universalstudentliving.com
  - Homes for Students          api.wearehomesforstudents.com
  - Essential Student Living    api.essentialstudentliving.com

USAGE
-----
    python multi_brand_rooms_etl.py

Optional env vars:
    PSL_API_TOKEN="Bearer <token>"   — Authorization header (applies to all APIs)
    PSL_ID_RANGE="60000-70000"       — probe individual IDs instead of list endpoint
"""

import csv
import html
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import requests

# Force UTF-8 console output so the box-drawing chars in our progress prints
# don't crash on Windows (default cp1252 console can't encode them).
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# ── Brand API config ───────────────────────────────────────────────────────────
BRANDS = [
    {
        "brand":    "Prestige Student Living",
        "base_url": "https://api.prestigestudentliving.com/wp-json/wp/v2",
        "endpoints": ["rooms", "basepms-rooms"],
    },
    {
        "brand":    "Urban Student Life",
        "base_url": "https://api.urbanstudentlife.com/wp-json/wp/v2",
        "endpoints": ["basepms-rooms"],
    },
    {
        "brand":    "Evo Student",
        "base_url": "https://api.evostudent.com/wp-json/wp/v2",
        "endpoints": ["basepms-rooms"],
    },
    {
        "brand":    "Universal Student Living",
        "base_url": "https://api.universalstudentliving.com/wp-json/wp/v2",
        "endpoints": ["basepms-rooms"],
    },
    {
        "brand":    "Homes for Students",
        "base_url": "https://api.wearehomesforstudents.com/wp-json/wp/v2",
        "endpoints": ["basepms-rooms"],
    },
    {
        "brand":    "Essential Student Living",
        "base_url": "https://api.essentialstudentliving.com/wp-json/wp/v2",
        "endpoints": ["basepms-rooms"],
    },
]

PER_PAGE  = 100
SLEEP_S   = 0.25
TIMEOUT   = 30
OUT_DIR   = "output"

MAX_RETRIES   = 3     # attempts per request before giving up
RETRY_BACKOFF = 3.0   # seconds, multiplied by the attempt number

API_TOKEN = os.getenv("PSL_API_TOKEN", "")
ID_RANGE  = os.getenv("PSL_ID_RANGE", "")

# ── Column schemas ─────────────────────────────────────────────────────────────
ROOM_FIELDS = [
    "brand_name", "property", "city", "room_type",
    "available_contracts",
    "description",
    "thumbnail_url", "image_urls",
]

CONTRACT_FIELDS = [
    "brand_name", "property", "city", "room_type",
    "available_contracts",
    "contract_title",
    "academic_year",
    "price_pw", "currency_symbol",
    "available",
    "start_date", "end_date",
    "contract_length_weeks",
    "base_hub_url",
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    text  = html.unescape(text or "")
    clean = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", clean).strip()


def safe(d, *keys, default=""):
    for k in keys:
        if not isinstance(d, (dict, list)):
            return default
        try:
            d = d[k]
        except (KeyError, IndexError, TypeError):
            return default
    return d if d is not None else default


def is_available(c) -> bool:
    """Whether a contract dict is currently available (the API's `available`
    flag, normalised from bool / 'true' / 1 / 'yes')."""
    return str(c.get("available", "")).strip().lower() in ("true", "1", "yes")


def pence_to_pounds(p):
    """basepms publishes contract 'price' in minor units (pence) — convert to
    £/week. Values that don't parse are passed through untouched."""
    try:
        v = float(p) / 100
    except (TypeError, ValueError):
        return p
    return f"{v:.2f}".rstrip("0").rstrip(".")


def city_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        return parse_qs(urlparse(url).query).get("city", [""])[0]
    except Exception:
        return ""


def make_session() -> requests.Session:
    s = requests.Session()
    headers = {"User-Agent": "MultiBrandRoomDB/1.0"}
    if API_TOKEN:
        headers["Authorization"] = API_TOKEN
    s.headers.update(headers)
    return s


# ── Fetchers ───────────────────────────────────────────────────────────────────

def get_with_retry(session, url, params=None):
    """GET with retry/backoff on transient errors (timeouts, conn drops, 5xx, 429).

    A single timed-out request used to make us drop an entire brand, which then
    looked like a mass "removed" event in the change log. Retrying first avoids
    that. 4xx (other than 429) are not retried — they won't fix themselves.
    """
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            resp = getattr(e, "response", None)
            if resp is not None and 400 <= resp.status_code < 500 and resp.status_code != 429:
                raise  # client error — retrying won't help
            last_exc = e
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF * attempt
                print(f"      retry {attempt}/{MAX_RETRIES - 1} after: {e} (waiting {wait:g}s)")
                time.sleep(wait)
    raise last_exc


def fetch_all_pages(session, endpoint_url, label):
    r = get_with_retry(session, endpoint_url, {"per_page": PER_PAGE, "page": 1})
    total_pages = int(r.headers.get("X-WP-TotalPages", 1))
    total_items = int(r.headers.get("X-WP-Total", 0))
    print(f"    {total_items} items across {total_pages} pages")
    items = r.json()
    for page in range(2, total_pages + 1):
        time.sleep(SLEEP_S)
        r = get_with_retry(session, endpoint_url, {"per_page": PER_PAGE, "page": page})
        items.extend(r.json())
        print(f"      page {page}/{total_pages} — {len(items)} fetched")
    return items


def fetch_by_id_range(session, endpoint_url, id_start, id_end):
    items = []
    total = id_end - id_start + 1
    print(f"    ID scan {id_start}–{id_end} ({total} probes)")
    for i, rid in enumerate(range(id_start, id_end + 1), 1):
        try:
            r = session.get(f"{endpoint_url}/{rid}", timeout=TIMEOUT)
            if r.status_code == 200:
                items.append(r.json())
                print(f"      ✓ {rid} ({len(items)} found)")
            time.sleep(SLEEP_S)
        except requests.RequestException:
            pass
        if i % 500 == 0:
            print(f"      … {i}/{total} IDs probed")
    return items


# ── Parser ─────────────────────────────────────────────────────────────────────

def parse_room(item: dict, brand_name: str) -> tuple[dict, list[dict]]:
    acf   = item.get("acf") or {}
    addr  = acf.get("propertyAddress") or {}
    media = acf.get("media") or {}
    brand = acf.get("brand") or {}

    # Title → room_type + property  ("Room Type, Property Name")
    title     = html.unescape(safe(item, "title", "rendered"))
    parts     = title.split(",", 1)
    room_type = parts[0].strip()
    property_ = html.unescape(parts[1].strip()) if len(parts) > 1 else \
                html.unescape(safe(addr, "propertyName"))

    # Brand name — prefer API field, fall back to config
    resolved_brand = (
        brand.get("name") or
        safe(item, "property", "acf", "locationBrand", "name") or
        brand_name
    )

    # Description
    desc = (
        strip_html(safe(item, "content", "rendered")) or
        strip_html(acf.get("description", ""))
    )

    # Media
    thumbnail_url = (
        acf.get("basepms_thumbnail_url") or
        safe(media, "photos", 0, "url") or ""
    )
    raw_images = acf.get("basepms_images") or safe(media, "photos") or []
    if raw_images is False:
        raw_images = []
    image_urls = "|".join(
        img["url"] for img in raw_images
        if isinstance(img, dict) and img.get("url")
    )

    # Contracts
    raw_contracts = (
        [c.get("contract", c) for c in (acf.get("contracts") or [])] or
        acf.get("contracts_cache") or []
    )

    # Currency
    pc  = acf.get("pricing_cache") or {}
    sym = (
        pc.get("currency_symbol") or
        safe(item, "property", "acf", "locationCurrency") or
        "£"
    )

    # City — address first, fall back to first contract URL
    city = (
        html.unescape(safe(addr, "city")) or
        city_from_url(safe(raw_contracts, 0, "base_hub_url"))
    )

    # Count of currently-available contracts (pricing options) for this room.
    # The API exposes no stock count, only a per-contract `available` flag.
    avail_contracts = sum(1 for c in raw_contracts if is_available(c))

    room_row = {
        "brand_name":          resolved_brand,
        "property":            property_,
        "city":                city,
        "room_type":           room_type or html.unescape(acf.get("roomType", "")),
        "available_contracts": avail_contracts,
        "description":         desc,
        "thumbnail_url":       thumbnail_url,
        "image_urls":          image_urls,
    }

    contract_rows = []
    for c in raw_contracts:
        prices    = c.get("prices") or [{}]
        raw_price = c.get("price")  # minor units; pricePerPersonPerWeek is already £
        price_pw  = pence_to_pounds(raw_price) if raw_price else \
                    safe(prices, 0, "pricePerPersonPerWeek")
        hub_url  = c.get("base_hub_url", "")
        contract_city = city or city_from_url(hub_url)

        contract_rows.append({
            "brand_name":            resolved_brand,
            "property":              property_,
            "city":                  contract_city,
            "room_type":             room_type,
            "available_contracts":   avail_contracts,
            "contract_title":        c.get("title") or c.get("name", ""),
            "academic_year":         c.get("academic_year") or c.get("academicYear", ""),
            "price_pw":              price_pw,
            "currency_symbol":       sym,
            "available":             c.get("available", ""),
            "start_date":            c.get("start_date") or c.get("startDate", ""),
            "end_date":              c.get("end_date") or c.get("endDate", ""),
            "contract_length_weeks": c.get("contract_length") or c.get("minContractDays", ""),
            "base_hub_url":          hub_url,
        })

    return room_row, contract_rows


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    ts            = datetime.now().strftime("%Y%m%d_%H%M%S")
    rooms_csv     = os.path.join(OUT_DIR, f"rooms_{ts}.csv")
    contracts_csv = os.path.join(OUT_DIR, f"contracts_{ts}.csv")

    session = make_session()
    all_rooms, all_contracts = [], []

    id_start = id_end = None
    if ID_RANGE:
        s, e     = ID_RANGE.split("-")
        id_start = int(s)
        id_end   = int(e)

    for brand_cfg in BRANDS:
        brand_name = brand_cfg["brand"]
        base_url   = brand_cfg["base_url"]
        print(f"\n━━ {brand_name}")

        for ep in brand_cfg["endpoints"]:
            endpoint_url = f"{base_url}/{ep}"
            print(f"  → {endpoint_url}")
            items = []
            try:
                if id_start:
                    items = fetch_by_id_range(session, endpoint_url, id_start, id_end)
                else:
                    items = fetch_all_pages(session, endpoint_url, ep)
            except requests.HTTPError as e:
                print(f"  !! HTTP {e.response.status_code} — skipping")
                continue
            except Exception as e:
                print(f"  !! Error: {e} — skipping")
                continue

            for item in items:
                try:
                    rr, crs = parse_room(item, brand_name)
                    all_rooms.append(rr)
                    all_contracts.extend(crs)
                except Exception as e:
                    print(f"  !! Parse error id={item.get('id')}: {e}")

    # Sort A-Z by property then room_type
    all_rooms.sort(key=lambda r: (r["property"].lower(), r["room_type"].lower()))
    all_contracts.sort(key=lambda r: (r["property"].lower(), r["room_type"].lower()))

    with open(rooms_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ROOM_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rooms)

    with open(contracts_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CONTRACT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_contracts)

    properties = {r["property"] for r in all_rooms if r["property"]}
    cities     = {r["city"]     for r in all_rooms if r["city"]}
    brands     = {r["brand_name"] for r in all_rooms}

    print(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  COMPLETE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Brands          : {len(brands)}
  Room types      : {len(all_rooms)}
  Contracts       : {len(all_contracts)}
  Properties      : {len(properties)}
  Cities          : {len(cities)}
  Rooms CSV       : {rooms_csv}
  Contracts CSV   : {contracts_csv}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""")


if __name__ == "__main__":
    main()
