# PROJECT OVERVIEW — rooms-data

> ⚠️ **KEEP THIS FILE UPDATED.** This document is your memory. Every time you change
> `etl.py`, the GitHub Actions workflow, the brand list, the CSV columns, or anything
> structural — **come back here and update the matching section before you commit.**
> If this file ever disagrees with the code, the code is right and this file is stale.
> See the [Maintenance checklist](#maintenance-checklist) at the bottom.

_Last updated: 2026-07-01 (**contracts_cache schema overhaul** — the basepms API changed fundamentally: contract/pricing data now comes as a flat per-instalment array at `acf.contracts_cache`. The contracts CSV now carries the 12 instalment fields verbatim — `instalment_id`, `name`, `academic_year`, `price`, `available`, `start_date`, `end_date`, `contract_length`, `weeks_remaining`, `base_hub_url`, `updated_at`, `pricing_updated_at` — plus context columns `brand_name`/`property`/`city`/`room_type`/`available_contracts`/`run_time`. `price` is still converted pence→£/week; `available` is normalised to `true`/`false`. Dropped columns: `contract_title` (→ `name`), `price_pw` (→ `price`), `contract_length_weeks` (→ `contract_length`), `currency_symbol` (removed — all brands are £). `make_report.py` now keys contracts on the stable unique `instalment_id` (was the composite title key) and diffs the `price` field, rendering each contract by its descriptive `name`. `publish_to_sheets.py` matches contracts within a group by `instalment_id` first (then `name`, then `start_date`) and reads the `price`/`contract_length` columns. Rooms schema unchanged. **Transition note:** the first run after this change shows one-off churn — the previous `contracts_latest.csv` lacks `instalment_id`, so every contract reads as "new" until the second run.). Earlier — 2026-06-15 (documented that room-level `acf.available` is an unreliable source artifact — only correctly synced some days, e.g. accurate 2026-06-12 but uniform `False` most days; derive availability from `available_contracts > 0`; `quantity_available` replaced by `available_contracts` — the count of currently-available contracts per room (the APIs expose no stock count, only a per-contract `available` flag); change tracking + Change Field label renamed to "Available Contracts" accordingly; Contracts change tab now filters legacy `(added)`/`(removed)` Change Field values via `CONTRACT_CHANGE_FIELD_EXCLUDE`; Room change tab no longer logs `quantity_available` / `(added)` / `(removed)` (history purged too); room quantity changes moved to the Contracts change tab, one row per room with blank Year/Duration via `room_quantity_changes()`; `quantity_available` added to the contracts schema at column E — room-level count repeated per contract, not change-watched; `Latest Room Data` tab now explodes `image_urls` to one row per URL via `explode_on()` — display-only, CSV unchanged; doc-consistency pass: §1 now says three scripts incl. `publish_to_sheets.py` and lists all deps; §4 line ranges refreshed and `pence_to_pounds` added to the helpers list). Earlier — 2026-06-10 (folder renamed `ROOMS data` → `rooms-data`; VS .sln/.pyproj renamed to match; post-rename round trip verified; Google Sheet renamed to `rooms-data`; room change tab now also watches `description` / `thumbnail_url` / `image_urls`; contract changes re-keyed to property/city/room/year/duration with start/end dates watched and New Contract / Sold Out change types; `price_pw` converted pence → pounds at the ETL with units-switch guards in both change logs)_

---

## 1. What is this project? (the one-paragraph version)

This is a small **ETL pipeline** (Extract, Transform, Load) that scrapes **student
accommodation room and pricing data** from the public WordPress REST APIs of **6
student-living brands**, flattens it into a clean tabular shape, and writes out **two
combined CSV files** — one listing every room type, one listing every contract/pricing
option. It runs **automatically every weekday (Mon–Fri) via GitHub Actions** and commits
the fresh CSVs back into the repo, so there is always an up-to-date snapshot of room
availability and prices across all brands in one place. Each weekday run also writes a
dated **change report** (snapshot + changelog) into `reports/`.

It is **three Python scripts** — `etl.py` (~400 lines, the scraper), `make_report.py`
(the weekday diff/report generator), and `publish_to_sheets.py` (the Google Sheets
publisher). Dependencies: `etl.py` needs only `requests`; `make_report.py` is pure stdlib;
`publish_to_sheets.py` adds `gspread` + `google-auth` (three packages total in
`requirements.txt`). There is no database, no web server, no framework — just "hit APIs →
parse JSON → write CSV → diff against yesterday → push to a Sheet."

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

1. **Config (lines ~46–108)**
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

2. **Helpers (lines ~110–154)**
   - `strip_html()` — unescapes HTML entities and removes tags from description text.
   - `safe(d, *keys)` — safely walks nested dicts/lists without crashing on missing keys
     (returns `""` instead of throwing). Used heavily because the API JSON is messy and
     inconsistent between brands.
   - `pence_to_pounds()` — converts the API's minor-unit `price` (pence) to £/week,
     passing through anything that doesn't parse. This is the pence→pounds conversion
     referenced in §5.
   - `city_from_url()` — pulls the `city` query param out of a booking URL as a fallback
     when the address record has no city.
   - `make_session()` — builds a `requests.Session` with the User-Agent and optional auth.

3. **Fetchers (lines ~157–213)**
   - `fetch_all_pages()` — the normal path. Reads the WordPress pagination headers
     (`X-WP-TotalPages`, `X-WP-Total`) and loops through every page, accumulating items.
   - `fetch_by_id_range()` — the `PSL_ID_RANGE` fallback path; hits `/endpoint/<id>` for
     each id in the range and keeps the 200s.
   - `get_with_retry()` — wraps every page GET with retry/backoff (`MAX_RETRIES`,
     `RETRY_BACKOFF`) on transient errors (timeouts, conn drops, 5xx, 429). Added because a
     single timeout used to drop a whole brand, which then showed up as mass "removed" rows.

4. **The parser — `parse_room()` (lines ~218–313)** — this is the heart of the
   "Transform" step and the messiest part. For each raw API item it:
   - Splits the title `"Room Type, Property Name"` into `room_type` + `property`.
   - Resolves the brand name (prefers the API's own field, falls back to config).
   - Cleans the description (HTML stripped).
   - Pulls thumbnail + a `|`-joined list of image URLs.
   - Extracts the contracts array — now the flat per-instalment array at
     `acf.contracts_cache` (the legacy `acf.contracts` path is kept only as a fallback for
     any brand that hasn't migrated). Each entry already has the 12 fields; they are copied
     verbatim except `price` (pence→£/week via `pence_to_pounds`) and `available`
     (normalised to `true`/`false`), plus the run's `run_time` and room-level context.
   - Works out city with fallbacks (address → first contract's `base_hub_url`). Currency is
     no longer emitted (all brands are £).
   - Returns **one room row** plus **a list of contract rows** (one per instalment).
   - The many `or` fallbacks exist precisely because each brand's JSON is shaped slightly
     differently — that's why the code looks defensive/repetitive.

5. **`main()` (lines ~318–395)**
   - Makes the `output/` dir, builds a timestamp `YYYYMMDD_HHMMSS`.
   - Loops every brand → every endpoint → fetches → parses → collects all rooms and
     contracts into two big lists. Errors per brand/item are caught and logged, not fatal.
   - **Sorts both lists A–Z by `(property, room_type)`.**
   - Writes `output/rooms_<timestamp>.csv` and `output/contracts_<timestamp>.csv`
     using `csv.DictWriter` (with `extrasaction="ignore"` so extra dict keys don't break).
   - Prints a summary (brand count, room count, contract count, properties, cities).

6. **`make_report.py` (separate script, run by CI after `etl.py`)**
   - Compares the previous run's `*_latest.csv` (stashed by the workflow before they're
     overwritten) against the new `*_latest.csv`.
   - Writes `reports/<date>/rooms.csv` + `contracts.csv` (dated snapshot copies) and
     `reports/<date>/changes.md` (the changelog). See §5 for the report contents.
   - Pure stdlib (`csv`), no dependencies. Handles a missing "previous" (first run) by
     emitting a snapshot-only report. Usage:
     `python make_report.py <date> <prev_rooms> <prev_contracts> <new_rooms> <new_contracts> <reports_root>`.

---

## 5. Output files & their columns

`etl.py` writes into `output/`, named with a run timestamp. The GitHub Action additionally
copies the newest of each into stable names `rooms_latest.csv` / `contracts_latest.csv`
so downstream consumers always know where to look. **`output/` keeps every dated run** —
the timestamped CSVs are committed too (they are no longer gitignored), so `output/`
accumulates the full history of raw runs alongside the two `*_latest.csv` pointers.

**`rooms_<timestamp>.csv`** (and `rooms_latest.csv`) — one row per room type:
`brand_name` · `property` · `city` · `room_type` · `available_contracts` ·
`description` · `thumbnail_url` · `image_urls`

**`contracts_<timestamp>.csv`** (and `contracts_latest.csv`) — one row per contract / instalment.
Context columns first, then the 12 fields pulled verbatim from each `acf.contracts_cache` entry:
`brand_name` · `property` · `city` · `room_type` · `available_contracts` · `run_time` ·
`instalment_id` · `name` · `academic_year` · `price` · `available` · `start_date` · `end_date` ·
`contract_length` · `weeks_remaining` · `base_hub_url` · `updated_at` · `pricing_updated_at`
(`price` is £/week — the API's minor-unit value ÷100. `available` is `true`/`false`.
`run_time` is the run's UTC timestamp, stamped on every contract row.)
(`available_contracts` is the **count of currently-available contracts (pricing options)** for the room — `sum(is_available(c) …)` in `parse_room()` — repeated on each of that room's contract rows, at column E on both tabs. The APIs expose **no stock/units count**, only a per-contract `available` flag, so this count of options is the closest real number; it is **not** physical rooms available. Changes to it are logged on the **Contracts** change tab, not the rooms tab — see §6.)

> ⚠️ **Do not use the room-level `acf.available` field.** It is an **unreliable source-side artifact**: the basepms→WordPress sync only populates it correctly on some days. On 2026-06-12 it was 99.6% accurate (1,219 `True` rooms had available contracts, 369 `False` rooms had none) — but on 06-09/10/11 and 06-15 it returned a uniform `False` for *every* room despite contracts being available, and on 06-08 a uniform `True`. The old `quantity_available` column fell through to this field, which is why it was meaningless. The reliable signal is the **per-contract** `available` flag — derive room availability as `available_contracts > 0`, never from `acf.available`.

> Sense of scale (run of 2026-06-05): ~1,540 room rows and ~4,480 contract rows across
> the 6 brands. `image_urls` is a single `|`-separated string **in the CSV** (the
> `Latest Room Data` Sheet tab explodes it to one row per URL — see §6). `price` is the price
> **per person per week in pounds** (`etl.pence_to_pounds` converts the API's minor-unit
> value; a legacy `units_artifact` guard survives in both change logs to absorb the old
> ~2026-06-08→10 pence/pounds switch, though it rarely fires now).

### The `reports/` folder (weekday change reports)

Each weekday run produces `reports/<YYYY-MM-DD>/` containing:
- **`rooms.csv` / `contracts.csv`** — a dated snapshot copy of that day's data.
- **`changes.md`** — a human-readable changelog vs the **previous run's** `*_latest.csv`:
  a summary plus sections for price changes (`£old → £new`), contracts added/removed,
  rooms added/removed, and availability changes.

This is produced by `make_report.py` (see §4), not `etl.py`. Row identity for the diff:
rooms are keyed on `(brand_name, property, room_type)`; contracts on the stable unique
`instalment_id` alone (contracts are rendered by their descriptive `name`, and the
brand-vanish guard reads `brand_name` from the row) — change those keys in `make_report.py`
if you change what counts as "the same" row.

---

## 6. How it runs automatically (GitHub Actions)

Workflow file: `.github/workflows/run_etl.yml`, named **"Room Database ETL"**.

- **Trigger:** weekday cron at **6:28 PM UK time** (Mon–Fri), plus manual
  `workflow_dispatch` from the Actions tab. No weekend runs. GitHub cron is UTC with no
  DST, so it's two entries scoped by month: `28 17 * 4-10 1-5` (BST) and
  `28 18 * 11,12,1,2,3 1-5` (GMT); expect ~1hr drift for a few days around each DST switch.
- **Steps:** checkout → **stash previous `*_latest.csv`** (to `/tmp/prev`, for the diff) →
  set up Python 3.11 → `pip install -r requirements.txt` → `python etl.py` (with
  `PSL_API_TOKEN` injected from repo secrets) → copy newest CSVs to `*_latest.csv` →
  **`python make_report.py …`** (writes `reports/<date>/`) → **`python publish_to_sheets.py`**
  (pushes the latest data to Google Sheets) → commit & push `output/` and `reports/` back
  to the repo (as `github-actions[bot]`).
- **Google Sheets publish:** `publish_to_sheets.py` (service account) writes four tabs:
  - `Latest Room Data` / `Latest Contracts Data` — the full current CSVs, **cleared &
    rewritten each run**, with a `Run Time` column appended as the last column. On
    `Latest Room Data` the `image_urls` column is **exploded to one row per URL** (all
    other columns repeated, `|` separators dropped) via `explode_on()` — display-only;
    `rooms_latest.csv` itself stays `|`-joined so the change-log diff is unaffected.
  - `Room Data Changes Last 30 Days` / `Contracts Data Changes Last 30 Days` — a **rolling
    30-day change log kept *inside the Sheet*** (not committed to GitHub). Each run diffs the
    previous run's `*_latest.csv` (stashed to `/tmp/prev`) vs the new one, appends the day's
    change rows, and prunes rows older than 30 days. Columns: Run Date · Brand · Property ·
    City · Room Type · (contracts also: Academic Year · Duration) · Change Field · Change
    Type · Old Content · New Content. Watched fields: rooms `description`,
    `thumbnail_url`, `image_urls` (long values clipped to the ~49K cell
    limit); contracts `price`, `available`, `start_date`, `end_date`. Contract identity
    is brand + property + city + room_type + academic_year + `contract_length`; multiple
    contracts under one identity (e.g. Sept + Jan intakes) are paired by the stable
    `instalment_id` first, then `name`, then start date,
    then order (`_match_contracts`). Unmatched new contracts log Change Type
    **"New Contract"**, vanished ones **"Sold Out"**; rooms use Added/Removed.
  - **Room-tab suppression:** the Room change tab does **not** log `available_contracts`,
    `(added)`, or `(removed)` (set `ROOM_CHANGE_FIELD_EXCLUDE`, filtered in
    `update_change_tab` against **both** new and existing rows, so they're purged from
    history too). Room **available-contracts count** changes are instead logged on the
    **Contracts** change tab — one row per room (Change Field "Available Contracts", blank
    Academic Year / Duration) via `room_available_contracts_changes()`, sourced from the
    rooms indexes so it doesn't fan out per contract. It skips comparisons where the old
    value is blank (schema-migration guard, like `units_artifact`) so renaming/adding the
    field doesn't log a spurious `"" → N` for every room. Room appearance/disappearance is
    already covered on the Contracts tab by New Contract / Sold Out. The Contracts tab also filters
    `CONTRACT_CHANGE_FIELD_EXCLUDE = {(added), (removed)}` — legacy Change Field values from
    an older diff that the current code no longer emits, kept out so stale rows don't linger.
  - **Brand-vanish guard:** if a whole brand is absent from the new run (probable API
    timeout), its rows are NOT logged as "removed" (same guard in `make_report.py`).
  - **Reset switch:** trigger the workflow with the `reset_change_tabs` input = true (env
    `RESET_CHANGE_TABS`) to wipe both change tabs back to headers — used to clear out bad
    entries.
  - Needs secrets `GCP_SA_KEY` (service-account JSON) + `SHEET_ID`. No secrets → no-op.
  - 🔐 **The service-account key is a secret.** It lives only as the `GCP_SA_KEY` GitHub
    secret. Any local copy of the JSON key must **never** be committed — `.gitignore`
    blocks `rooms-data-*.json`, `*service-account*.json`, `gcp-*.json`, `*.iam.gs*.txt`.
    The Sheet ID is `1ZFftk46uMQTG0259qlOGwoUAKMoECdSj-LK59mOtatU` (`SHEET_ID` secret); the
    Sheet is shared with the service account's email as Editor.
- **What gets committed:** everything under `output/` (every dated run **and** the two
  `*_latest.csv`) plus everything under `reports/`. The job declares
  `permissions: contents: write` so it can push.
- History note: an earlier version only kept `*_latest.csv` and gitignored the timestamped
  files; it also tried to `git add` those ignored files and failed. Both are fixed —
  timestamped files are now tracked and kept.

If you ever need the token: GitHub repo → Settings → Secrets and variables → Actions →
secret named `PSL_API_TOKEN`.

---

## 7. Files in this repo (what's what)

| Path | What it is |
|---|---|
| `etl.py` | **The scraper.** Fetches all brands and writes the CSVs. |
| `make_report.py` | **The weekday report generator.** Diffs runs → `reports/<date>/`. |
| `publish_to_sheets.py` | **Pushes latest CSVs to Google Sheets** (service account). |
| `requirements.txt` | `requests` (etl.py) + `gspread`, `google-auth` (publish step). |
| `README.md` | Public-facing setup/usage doc (git init, secrets, how to run). |
| `.github/workflows/run_etl.yml` | The weekday GitHub Actions automation. |
| `.gitignore` | Python cruft + `.vs/` + **service-account key files** (`rooms-data-*.json` etc.) + `_bf/`. Does NOT ignore output CSVs (those are kept). |
| `output/` | Generated CSVs — every dated run **plus** `*_latest.csv`. |
| `reports/` | Per-weekday `<date>/` folders: dated snapshot CSVs + `changes.md`. |
| `rooms-data.pyproj` / `rooms-data.sln` | Visual Studio Python project/solution files (just for opening in VS). |
| `PROJECT_OVERVIEW.md` | **This file.** Your future-self memory. |

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

To build a change report locally (CI does this automatically each weekday), point it at a
previous and a new pair of CSVs:

```powershell
python make_report.py 2026-06-08 `
  output/rooms_<old>.csv output/contracts_<old>.csv `
  output/rooms_latest.csv output/contracts_latest.csv `
  reports
```

> On this Windows machine the interpreter is the **`py`** launcher (plain `python` isn't on
> the Git Bash PATH), so run `py etl.py` / `py make_report.py …`.

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
- **Clear bad rows from the Sheet change tabs** → run the workflow with the
  `reset_change_tabs` input ticked (wipes both change tabs to headers).
- **Rebuild the change log from scratch** → the per-day full snapshots live in `output/`
  (timestamped runs) and in git history (`*_latest.csv` at each `chore: update room data`
  commit). Diff consecutive days with the same logic as `publish_to_sheets.py`
  (`diff_rooms` / `diff_contracts`, stamping each change with the later day's date), then
  write the rows into the two change tabs. This was done once on 2026-06-09 to repair tabs
  after an API-timeout polluted them; the script was a throwaway in `_bf/` (gitignored).

---

## Maintenance checklist

Before you `git commit`, if you touched any of these, update the matching section above:

- [ ] Changed `BRANDS` → update **§3** and the brand table.
- [ ] Changed `ROOM_FIELDS` / `CONTRACT_FIELDS` or parser output → update **§5**.
- [ ] Changed the fetch/parse logic or constants → update **§4**.
- [ ] Changed `make_report.py` (diff keys, report sections) → update **§4** + **§5**.
- [ ] Changed the GitHub workflow (schedule, steps, secrets) → update **§6**.
- [ ] Added/removed files → update **§7**.
- [ ] Bump the **_Last updated_** date at the top.

_If you forget, the next "what is this project again?" moment will be painful. Keep it current._
