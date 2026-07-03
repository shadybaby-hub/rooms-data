"""
Multi-Brand Student Living — Room Database Builder
===================================================
Fetches basepms-rooms from 6 brand APIs, enriches each room with real
availability from the `rooms` endpoint (via the /room/<slug>/ redirect — see
fetch_room_detail), and publishes all data into two combined CSVs sorted A-Z by
property (column B):

  • rooms_<timestamp>.csv      — one row per room type
  • contracts_<timestamp>.csv  — one row per contract / pricing option

The `rooms` endpoint supplies the *real* room availability (quantityAvailable),
amenities, and the authoritative list of *bookable* contracts. A contracts_cache
entry is only truly available (`bookable`) if it matches a `rooms` contract on
start date, end date, length (weeks×7 = minContractDays) and £/week price — the
old contracts_cache `available` flag lists every contract ever created, not what
is actually bookable.

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
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
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

# Browser UA used for every request. REQUIRED: the `/room/<slug>/` frontend route
# (which redirects to the `rooms` REST JSON we enrich from) sits behind a WAF that
# rejects non-browser agents. The basepms-rooms API accepts it too.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# Path to the curl binary (or None). Used only as a fallback for brands whose
# frontend route is behind a Cloudflare JS/TLS-fingerprint challenge that blocks
# python-requests but not curl (e.g. Homes for Students). Present on the CI runner.
CURL_BIN = shutil.which("curl")

# ── Column schemas ─────────────────────────────────────────────────────────────
ROOM_FIELDS = [
    "brand_name", "property", "city", "room_type",
    "available_contracts",   # legacy derived count (contracts_cache) — retire later
    "quantity_available",    # real room availability from the `rooms` endpoint
    "description",
    "thumbnail_url", "image_urls",
    "amenities",             # `|`-joined amenity post_titles from the `rooms` endpoint
]

# Context columns (identifying / run metadata) followed by the 12 fields pulled
# verbatim from each acf.contracts_cache entry. `price` is the only transformed
# field — the API's minor-unit value is converted to £/week (see pence_to_pounds).
# `bookable` / `Contact Form URL` come from matching each cache contract to the
# `rooms` endpoint's contracts (the source of truth for what's actually bookable).
CONTRACT_FIELDS = [
    "brand_name", "property", "city", "room_type",
    "available_contracts",   # legacy derived count — retire later
    "quantity_available",    # real room availability (repeated per contract row)
    "run_time",
    "instalment_id",
    "name",
    "academic_year",
    "price",
    "available",             # contracts_cache `available` flag (unreliable)
    "bookable",              # matched in the `rooms` endpoint = actually bookable
    "Contact Form URL",      # bookNowURL for this contract, or "-" if none
    "start_date", "end_date",
    "contract_length",
    "weeks_remaining",
    "base_hub_url",
    "updated_at",
    "pricing_updated_at",
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


def _to_float(v):
    """Parse a price-ish value to a rounded float, or None if it doesn't parse."""
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _to_int(v):
    """Parse an int-ish value (accepts '357', 357, '357.0'), or None."""
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def rooms_endpoint_contracts(detail: dict) -> dict:
    """From a `rooms`-endpoint room JSON, build the set of *actually bookable*
    contracts, keyed for matching against contracts_cache.

    Returns {(start_date, end_date, min_contract_days, price_pw): bookNowURL}.
    The `rooms` endpoint's acf.contracts[].contract carries the live/bookable
    options; contracts_cache lists every contract ever created, so a cache
    contract is only genuinely available if it matches one of these keys.
    """
    acf = detail.get("acf") or {}
    out = {}
    for wrap in (acf.get("contracts") or []):
        c = (wrap.get("contract") if isinstance(wrap, dict) else None) or {}
        price = _to_float(safe(c, "prices", 0, "pricePerPersonPerWeek"))
        key = (
            str(c.get("startDate", "")).strip(),
            str(c.get("endDate", "")).strip(),
            _to_int(c.get("minContractDays")),
            price,
        )
        out[key] = c.get("bookNowURL") or ""
    return out


def _cache_contract_key(c: dict) -> tuple:
    """Match key for a contracts_cache entry, aligned with the units the `rooms`
    endpoint uses: contract_length (weeks) → days (×7), price (pence) → £/week."""
    weeks = _to_int(c.get("contract_length"))
    return (
        str(c.get("start_date", "")).strip(),
        str(c.get("end_date", "")).strip(),
        weeks * 7 if weeks is not None else None,
        _to_float(pence_to_pounds(c.get("price"))),
    )


def amenity_titles(detail: dict) -> str:
    """`|`-joined amenity post_titles from a `rooms`-endpoint room (or "").

    Each entry is wrapped: acf.amenities[].amenity.post_title (same shape as
    contracts[].contract). Falls back to an unwrapped post_title just in case."""
    am = (detail.get("acf") or {}).get("amenities")
    if not isinstance(am, list):
        return ""
    titles = []
    for a in am:
        if not isinstance(a, dict):
            continue
        inner = a.get("amenity") if isinstance(a.get("amenity"), dict) else a
        title = inner.get("post_title")
        if title:
            titles.append(html.unescape(title))
    return "|".join(titles)


def city_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        return parse_qs(urlparse(url).query).get("city", [""])[0]
    except Exception:
        return ""


def make_session() -> requests.Session:
    s = requests.Session()
    headers = {"User-Agent": BROWSER_UA}
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


# Spelled-out numbers → digits, for slug normalisation. The two endpoints disagree
# not only on punctuation but on whether a number is a digit or a word ('1-bed' vs
# 'one-bed', 'fourth-floor' vs '4th-floor'), so we canonicalise both sides. Ordinals
# map to the digit-ordinal form ('fourth'→'4th'), which stays distinct from the
# cardinal ('four'→'4') so the two never collide. Mapped as whole tokens only (see
# _norm_slug) — never as substrings — so 'twodio', 'mezzanine' etc. stay intact.
NUMBER_WORDS = {
    # cardinals
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11",
    "twelve": "12",
    # ordinals
    "first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th",
    "fifth": "5th", "sixth": "6th", "seventh": "7th", "eighth": "8th",
    "ninth": "9th", "tenth": "10th", "eleventh": "11th", "twelfth": "12th",
}


def _norm_slug(s: str) -> str:
    """Normalise a slug for cross-endpoint matching: lowercase, map spelled-out
    numbers to digits (whole tokens only), then strip everything but a-z0-9.
    basepms-rooms and the `rooms` post type slugify titles slightly differently —
    in punctuation ('classic-ensuite' vs 'classic-en-suite') and in digit-vs-word
    counts ('one-bed' vs '1-bed') — and this collapses both to the same key."""
    toks = re.split(r"[^a-z0-9]+", (s or "").lower())
    return "".join(NUMBER_WORDS.get(t, t) for t in toks if t)


def _is_room_post(d) -> bool:
    """True only for an actual `rooms` REST item. When a slug doesn't resolve, the
    /room/<slug>/ redirect falls back to the /wp-json/ API index (no `id`), which
    we must NOT treat as room detail (it would blank quantity_available yet still
    set a misleading enquire_status)."""
    return isinstance(d, dict) and isinstance(d.get("id"), int) and "acf" in d


def room_slug_map(session, host):
    """{normalised_slug: real rooms-post slug} for a brand, from its room sitemap.
    Lets us map a basepms-rooms slug to the (differently-slugified) `rooms` post
    slug so the /room/<slug>/ redirect lands on the right item. Empty if the
    sitemap can't be read (callers then fall back to the basepms slug as-is)."""
    index = _fetch_text(session, f"https://{host}/sitemap_index.xml")
    subs = re.findall(r"<loc>([^<]*/room-sitemap\d*\.xml)</loc>", index)
    if not subs:  # unpaginated / no index — try the single default file
        subs = [f"https://{host}/room-sitemap.xml"]
    out = {}
    for sub in subs:
        xml = _fetch_text(session, sub)
        for slug in re.findall(r"/room/([^/<]+)/?</loc>", xml):
            out[_norm_slug(slug)] = slug
    return out


def fetch_room_detail(session, host, slug):
    """Fetch a room's `rooms`-endpoint JSON via the `/room/<slug>/` frontend URL,
    which 301-redirects to `/wp-json/wp/v2/rooms/<id>`. The `rooms` collection
    can't be listed (X-WP-Total 0) and its ids differ from basepms-rooms, so the
    shared `slug` + this redirect is the only way in.

    Primary path is python-requests. Some brands' frontend sits behind a
    Cloudflare challenge that blocks requests (by TLS fingerprint) but not curl,
    so on a non-JSON/error response we retry once with curl. Returns {} unless the
    response is a real room post (see _is_room_post), so an unresolved slug — or a
    fetch failure — degrades gracefully to the basepms-rooms data."""
    if not slug:
        return {}
    url = f"https://{host}/room/{slug}/"
    try:
        r = get_with_retry(session, url)
        if "application/json" in r.headers.get("content-type", ""):
            d = r.json()
            if _is_room_post(d):
                return d
        # else: likely a Cloudflare interstitial (HTML) — fall through to curl
    except (requests.RequestException, ValueError):
        pass
    d = _curl_json(url)
    return d if _is_room_post(d) else {}


def _curl_get(url):
    """Return the response body bytes for `url` via curl (following redirects), or
    None. curl clears the Cloudflare TLS-fingerprint challenge that blocks
    python-requests on some brands."""
    if not CURL_BIN:
        return None
    try:
        p = subprocess.run(
            [CURL_BIN, "-sL", "-A", BROWSER_UA, "--max-time", str(TIMEOUT), url],
            capture_output=True, timeout=TIMEOUT + 10,
        )
        if p.returncode == 0 and p.stdout:
            return p.stdout
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def _curl_json(url):
    """curl-fetch `url` and parse JSON, or {} on any failure."""
    body = _curl_get(url)
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return {}


def _fetch_text(session, url):
    """GET text (e.g. sitemap XML): python-requests first, curl fallback for
    brands whose frontend is behind the Cloudflare challenge. "" on failure."""
    try:
        r = get_with_retry(session, url)
        body = r.text
        if body.lstrip().startswith("<"):
            return body
    except (requests.RequestException, ValueError):
        pass
    body = _curl_get(url)
    return body.decode("utf-8", "replace") if body else ""


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

def parse_room(item: dict, brand_name: str, run_time: str,
               detail: dict | None = None) -> tuple[dict, list[dict]]:
    acf   = item.get("acf") or {}
    addr  = acf.get("propertyAddress") or {}
    media = acf.get("media") or {}
    brand = acf.get("brand") or {}

    # `rooms`-endpoint enrichment (real availability / amenities / bookable). When
    # the detail fetch failed/was blocked we get {} — leave the new fields BLANK
    # (not 0/false) so a transient fetch failure doesn't read as "sold out" in the
    # change log (same reasoning as the brand-vanish guard).
    detail       = detail or {}
    has_detail   = bool(detail)
    detail_acf   = detail.get("acf") or {}
    quantity_available = detail_acf.get("quantityAvailable", "") if has_detail else ""
    amenities    = amenity_titles(detail) if has_detail else ""
    bookable_map = rooms_endpoint_contracts(detail) if has_detail else {}

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

    # Contracts — the fundamentally-changed system exposes a flat per-instalment
    # array at acf.contracts_cache. Keep the legacy `contracts` path as a fallback
    # for any brand that hasn't migrated yet.
    raw_contracts = (
        [c.get("contract", c) for c in (acf.get("contracts") or [])] or
        acf.get("contracts_cache") or []
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
        "quantity_available":  quantity_available,
        "description":         desc,
        "thumbnail_url":       thumbnail_url,
        "image_urls":          image_urls,
        "amenities":           amenities,
    }

    contract_rows = []
    for c in raw_contracts:
        hub_url       = c.get("base_hub_url", "")
        contract_city = city or city_from_url(hub_url)

        # Is this cache contract actually bookable? True iff it matches one of the
        # `rooms` endpoint's live contracts on (start, end, length-in-days, £/week).
        if has_detail:
            book_url = bookable_map.get(_cache_contract_key(c))
            bookable = book_url is not None
            bookable_str  = "Enabled" if bookable else "Disabled"
            # Booking link: the bookNowURL when there is one, else "-".
            contract_enquire = book_url if book_url else "-"
        else:
            bookable_str  = ""   # unknown — detail fetch failed
            contract_enquire = ""

        contract_rows.append({
            # context / run metadata
            "brand_name":          resolved_brand,
            "property":            property_,
            "city":                contract_city,
            "room_type":           room_type,
            "available_contracts": avail_contracts,
            "quantity_available":  quantity_available,
            "run_time":            run_time,
            # the 12 fields, verbatim from the contracts_cache entry
            "instalment_id":       c.get("instalment_id", ""),
            "name":                c.get("name", ""),
            "academic_year":       c.get("academic_year", ""),
            "price":               pence_to_pounds(c.get("price")),  # minor units → £/week
            "available":           str(is_available(c)).lower(),
            "bookable":            bookable_str,
            "Contact Form URL":    contract_enquire,
            "start_date":          c.get("start_date", ""),
            "end_date":            c.get("end_date", ""),
            "contract_length":     c.get("contract_length", ""),
            "weeks_remaining":     c.get("weeks_remaining", ""),
            "base_hub_url":        hub_url,
            "updated_at":          c.get("updated_at", ""),
            "pricing_updated_at":  c.get("pricing_updated_at", ""),
        })

    return room_row, contract_rows


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    now           = datetime.now(timezone.utc)
    ts            = now.strftime("%Y%m%d_%H%M%S")
    run_time      = now.strftime("%Y-%m-%dT%H:%M:%SZ")  # stamped on every contract row
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
        host       = urlparse(base_url).netloc
        print(f"\n━━ {brand_name}")

        # Map basepms slugs → real `rooms` post slugs (they slugify titles
        # differently, e.g. ensuite vs en-suite). Built once per brand.
        slug_map = room_slug_map(session, host)
        print(f"  slug map: {len(slug_map)} rooms-post slugs")

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

            # Enrich each room from the `rooms` endpoint (real availability etc.),
            # matched by slug via the /room/<slug>/ redirect. One extra GET per
            # room — throttled like page fetches. Missing detail degrades gracefully.
            detail_ok = 0
            for item in items:
                try:
                    slug = item.get("slug", "")
                    # Resolve to the real rooms-post slug; fall back to the
                    # basepms slug if the sitemap had no match.
                    real_slug = slug_map.get(_norm_slug(slug), slug)
                    detail = fetch_room_detail(session, host, real_slug)
                    if detail:
                        detail_ok += 1
                    if slug:
                        time.sleep(SLEEP_S)
                    rr, crs = parse_room(item, brand_name, run_time, detail)
                    all_rooms.append(rr)
                    all_contracts.extend(crs)
                except Exception as e:
                    print(f"  !! Parse error id={item.get('id')}: {e}")
            if items:
                print(f"    room detail enriched: {detail_ok}/{len(items)}")

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
