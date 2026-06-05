# PROJECT OVERVIEW — ROOMS data

> ⚠️ **KEEP THIS FILE UPDATED.** This document is your memory. Every time you change
> `etl.py`, the GitHub Actions workflow, the brand list, the CSV columns, or anything
> structural — **come back here and update the matching section before you commit.**
> If this file ever disagrees with the code, the code is right and this file is stale.
> See the [Maintenance checklist](#maintenance-checklist) at the bottom.

_Last updated: 2026-06-05 (UTF-8 console fix; workflow no longer commits gitignored timestamped CSVs + adds contents:write)_

---

## 1. What is this project? (the one-paragraph version)

This is a small **ETL pipeline** (Extract, Transform, Load) that scrapes **student
accommodation room and pricing data** from the public WordPress REST APIs of **6
student-living brands**, flattens it into a clean tabular shape, and writes out **two
combined CSV files** — one listing every room type, one listing every contract/pricing
option. It runs **automatically once a day via GitHub Actions** and commits the fresh
CSVs back into the repo, so there is always an up-to-date snapshot of room availability
and prices across all brands in one place.

The whole thing is **one Python script** (`etl.py`, ~350 lines) with **one dependency**
(`requests`). There is no database, no web server, no framework — just "hit APIs → parse
JSON → write CSV."

---

## 2. Why does it exist? (the business reason)

All 6 brands are part of the same group (Homes for Students family) and each runs the
same underlying booking engine (**basepms** — you can see `basepms.com` URLs in the
data). Each brand exposes its rooms through a WordPress API endpoint called
`basepms-rooms`. The data is scattered across 6 separate sites with no single combined
view.

This project gives you **one daily-refreshed, sorted CSV per data type** so you can:
- compare prices per week across brands/properties/cities,
- see what room types and contracts are currently available,
- feed the data into a spreadsheet, BI tool, or further analysis.

---

## 3. The 6 brands it pulls from

| Brand | API base |
|---|---|
| Prestige Student Living | `api.prestigestudentliving.com` |
| Urban Student Life | `api.urbanstudentlife.com` |
| Evo Student | `api.evostudent.com` |
| Universal Student Living | `api.universalstudentliving.com` |
| Homes for Students | `api.wearehomesforstudents.com` |
| Essential Student Living | `api.essentialstudentliving.com` |

All use the WordPress REST shape: `https://<api>/wp-json/wp/v2/<endpoint>`.
The endpoint is **`basepms-rooms`** for every brand. Prestige additionally has a
plain `rooms` endpoint, so it is configured to hit both.

This list lives in the `BRANDS` array near the top of `etl.py` — **that array is the
single source of truth for which brands are scraped.**

---

## 4. How the script works, step by step

The flow is all in `etl.py`. Top to bottom:

1. **Config (lines ~37–96)**
   - `BRANDS` — the list of brands + their base URLs + which endpoints to hit.
   - Tuning constants: `PER_PAGE=100`, `SLEEP_S=0.25` (politeness delay between pages),
     `TIMEOUT=30`, `OUT_DIR="output"`.
   - Two optional environment variables (read once at startup):
     - `PSL_API_TOKEN` → sent as the `Authorization` header on every request (e.g.
       `"Bearer xxxx"`). Leave unset if the APIs are public (they appear to be).
     - `PSL_ID_RANGE` → e.g. `"60000-70000"`. If set, the script switches from
       "list every page" mode to "probe each individual ID one by one" mode. Rarely
       needed; it's a fallback for when the list endpoint misbehaves.
   - `ROOM_FIELDS` and `CONTRACT_FIELDS` — define the exact CSV columns and their order.
   - Right after the imports, the script calls `sys.stdout/stderr.reconfigure(encoding="utf-8")`.
     This is a **Windows fix**: the progress prints use box-drawing chars (`━`) that the
     default Windows console (cp1252) can't encode, which used to crash the run. The CSVs
     were always written as UTF-8; this just makes the console output safe too.

2. **Helpers (lines ~98–132)**
   - `strip_html()` — unescapes HTML entities and removes tags from description text.
   - `safe(d, *keys)` — safely walks nested dicts/lists without crashing on missing keys
     (returns `""` instead of throwing). Used heavily because the API JSON is messy and
     inconsistent between brands.
   - `city_from_url()` — pulls the `city` query param out of a booking URL as a fallback
     when the address record has no city.
   - `make_session()` — builds a `requests.Session` with the User-Agent and optional auth.

3. **Fetchers (lines ~135–168)**
   - `fetch_all_pages()` — the normal path. Reads the WordPress pagination headers
     (`X-WP-TotalPages`, `X-WP-Total`) and loops through every page, accumulating items.
   - `fetch_by_id_range()` — the `PSL_ID_RANGE` fallback path; hits `/endpoint/<id>` for
     each id in the range and keeps the 200s.

4. **The parser — `parse_room()` (lines ~173–266)** — this is the heart of the
   "Transform" step and the messiest part. For each raw API item it:
   - Splits the title `"Room Type, Property Name"` into `room_type` + `property`.
   - Resolves the brand name (prefers the API's own field, falls back to config).
   - Cleans the description (HTML stripped).
   - Pulls thumbnail + a `|`-joined list of image URLs.
   - Extracts the contracts array (tries several differently-named fields because brands
     differ — `contracts`, `contracts_cache`, `prices`, etc.).
   - Works out currency symbol and city with multiple fallbacks.
   - Returns **one room row** plus **a list of contract rows** (one per pricing option).
   - The many `or` fallbacks exist precisely because each brand's JSON is shaped slightly
     differently — that's why the code looks defensive/repetitive.

5. **`main()` (lines ~271–344)**
   - Makes the `output/` dir, builds a timestamp `YYYYMMDD_HHMMSS`.
   - Loops every brand → every endpoint → fetches → parses → collects all rooms and
     contracts into two big lists. Errors per brand/item are caught and logged, not fatal.
   - **Sorts both lists A–Z by `(property, room_type)`.**
   - Writes `output/rooms_<timestamp>.csv` and `output/contracts_<timestamp>.csv`
     using `csv.DictWriter` (with `extrasaction="ignore"` so extra dict keys don't break).
   - Prints a summary (brand count, room count, contract count, properties, cities).

---

## 5. Output files & their columns

Written into `output/`, named with a run timestamp. The GitHub Action additionally
copies the newest of each into stable names `rooms_latest.csv` / `contracts_latest.csv`
so downstream consumers always know where to look.

**`rooms_<timestamp>.csv`** — one row per room type:
`brand_name` · `property` · `city` · `room_type` · `quantity_available` ·
`description` · `thumbnail_url` · `image_urls`

**`contracts_<timestamp>.csv`** — one row per contract / pricing option:
`brand_name` · `property` · `city` · `room_type` · `contract_title` · `academic_year` ·
`price_pw` · `currency_symbol` · `available` · `start_date` · `end_date` ·
`contract_length_weeks` · `base_hub_url`

> Sense of scale (last observed run, 2026-06-05): ~1,500 room rows and ~3,170 contract
> rows across the 6 brands. `image_urls` is a single `|`-separated string. `price_pw` is
> the price **per person per week**.

---

## 6. How it runs automatically (GitHub Actions)

Workflow file: `.github/workflows/run_etl.yml`, named **"Room Database ETL"**.

- **Trigger:** daily cron at **06:00 UTC**, plus manual `workflow_dispatch` from the
  Actions tab.
- **Steps:** checkout → set up Python 3.11 → `pip install -r requirements.txt` →
  `python etl.py` (with `PSL_API_TOKEN` injected from repo secrets) → copy newest CSVs
  to `*_latest.csv` → commit & push the CSVs back to the repo (as `github-actions[bot]`).
- Only the `*_latest.csv` files are committed back. The timestamped run files are
  gitignored, so they are **not** committed (an earlier version tried to and the run
  failed because `git add` rejects ignored paths). The job declares
  `permissions: contents: write` so it can push.

If you ever need the token: GitHub repo → Settings → Secrets and variables → Actions →
secret named `PSL_API_TOKEN`.

---

## 7. Files in this repo (what's what)

| Path | What it is |
|---|---|
| `etl.py` | **The whole pipeline.** Everything happens here. |
| `requirements.txt` | One line: `requests==2.32.3`. The only dependency. |
| `README.md` | Public-facing setup/usage doc (git init, secrets, how to run). |
| `.github/workflows/run_etl.yml` | The daily GitHub Actions automation. |
| `.gitignore` | Ignores Python cruft AND the local timestamped CSVs (keeps `*_latest`). |
| `output/` | Generated CSVs (rooms + contracts, timestamped). |
| `ROOMS data.pyproj` | Visual Studio Python project file (just for opening in VS). |
| `PROJECT_OVERVIEW.md` | **This file.** Your future-self memory. |

Note: the `.pyproj` references a `practice python\rooms-data.py` file that isn't present
in the folder — ignore it, it's a leftover VS reference, not part of the pipeline.

---

## 8. How to run it locally

```powershell
pip install -r requirements.txt
python etl.py
```

Outputs land in `output/rooms_<timestamp>.csv` and `output/contracts_<timestamp>.csv`.
No token is needed unless the APIs start requiring auth — then set it first:

```powershell
$env:PSL_API_TOKEN = "Bearer <your_token>"
python etl.py
```

---

## 9. Common things "future you" will want to do

- **Add a new brand** → add an entry to the `BRANDS` list in `etl.py` (brand name, base
  URL ending in `/wp-json/wp/v2`, and `endpoints: ["basepms-rooms"]`). Next run picks it
  up automatically.
- **Add/rename a CSV column** → edit `ROOM_FIELDS` / `CONTRACT_FIELDS` AND make sure the
  matching key is produced inside `parse_room()`.
- **Change the schedule** → edit the `cron` line in `.github/workflows/run_etl.yml`.
- **A brand's data looks wrong/empty** → its JSON shape probably differs; look at the
  fallback `or` chains in `parse_room()` and add the new field name there.
- **Be gentler / faster on the APIs** → adjust `SLEEP_S` and `PER_PAGE` constants.

---

## Maintenance checklist

Before you `git commit`, if you touched any of these, update the matching section above:

- [ ] Changed `BRANDS` → update **§3** and the brand table.
- [ ] Changed `ROOM_FIELDS` / `CONTRACT_FIELDS` or parser output → update **§5**.
- [ ] Changed the fetch/parse logic or constants → update **§4**.
- [ ] Changed the GitHub workflow (schedule, steps, secrets) → update **§6**.
- [ ] Added/removed files → update **§7**.
- [ ] Bump the **_Last updated_** date at the top.

_If you forget, the next "what is this project again?" moment will be painful. Keep it current._
