# Student Room Database — ETL

Fetches room types and contracts from 6 student accommodation brand APIs and exports two combined, sorted CSVs updated automatically every **weekday (Mon–Fri)** via GitHub Actions. Each weekday run also writes a dated change report (snapshot + changelog) into `reports/`.

## Brands covered

| Brand | API |
|---|---|
| Prestige Student Living | api.prestigestudentliving.com |
| Urban Student Life | api.urbanstudentlife.com |
| Evo Student | api.evostudent.com |
| Universal Student Living | api.universalstudentliving.com |
| Homes for Students | api.wearehomesforstudents.com |
| Essential Student Living | api.essentialstudentliving.com |

## Output files

| File | Description |
|---|---|
| `output/rooms_latest.csv` | One row per room type — sorted A–Z by property |
| `output/contracts_latest.csv` | One row per contract / pricing option — sorted A–Z by property |
| `output/rooms_<timestamp>.csv` etc. | Every dated run is kept (full history of raw runs) |
| `reports/<date>/rooms.csv`, `contracts.csv` | Dated snapshot copy of each weekday's data |
| `reports/<date>/changes.md` | Changelog vs the previous run: price changes, contracts/rooms added/removed |

### rooms_latest.csv columns

`brand_name` · `property` · `city` · `room_type` · `quantity_available` · `description` · `thumbnail_url` · `image_urls`

### contracts_latest.csv columns

`brand_name` · `property` · `city` · `room_type` · `contract_title` · `academic_year` · `price_pw` · `currency_symbol` · `available` · `start_date` · `end_date` · `contract_length_weeks` · `base_hub_url`

---

## Setup

### 1. Create the GitHub repository

```bash
git init
git remote add origin https://github.com/<your-org>/student-room-db.git
git add .
git commit -m "init"
git push -u origin main
```

### 2. Add the API token secret (if required)

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `PSL_API_TOKEN` | `Bearer <your_token>` (leave blank if APIs are public) |

### 3. Enable GitHub Actions

Actions are enabled by default. The workflow runs:
- **Every weekday (Mon–Fri) at 6:28 PM UK time** automatically (17:28 UTC in BST, 18:28 UTC in GMT)
- **On demand** via the Actions tab → `Room Database ETL` → `Run workflow`

> The push-back step needs write access: **Settings → Actions → General → Workflow permissions → "Read and write permissions"**.

### 4. Run locally

```bash
pip install -r requirements.txt
python etl.py
```

Outputs land in `output/rooms_<timestamp>.csv` and `output/contracts_<timestamp>.csv`.

---

## How it works

```
GitHub Actions (weekday cron, Mon–Fri 6:28pm UK)
        │
        ▼
   etl.py runs
        │
        ├── Prestige Student Living  /rooms + /basepms-rooms
        ├── Urban Student Life       /basepms-rooms
        ├── Evo Student              /basepms-rooms
        ├── Universal Student Living /basepms-rooms
        ├── Homes for Students       /basepms-rooms
        └── Essential Student Living /basepms-rooms
                 │
                 ▼
        Parse & flatten all rooms + contracts
        Sort A–Z by property
                 │
                 ▼
        output/rooms_<timestamp>.csv  (every run kept)
        output/rooms_latest.csv       (newest, stable name)
        output/contracts_<timestamp>.csv + contracts_latest.csv
                 │
                 ▼
        make_report.py  → reports/<date>/
              ├── rooms.csv / contracts.csv  (dated snapshot)
              └── changes.md  (diff vs previous run)
                 │
                 ▼
        publish_to_sheets.py  → Google Sheet (service account)
              ├── Latest Room Data / Latest Contracts Data  (current data, rewritten)
              └── Room/Contracts Data Changes Last 30 Days   (rolling change log)
                 │
                 ▼
        git commit & push (output/ + reports/) to repo
```

## Adding a new brand

Add an entry to the `BRANDS` list in `etl.py`:

```python
{
    "brand":     "New Brand Name",
    "base_url":  "https://api.newbrand.com/wp-json/wp/v2",
    "endpoints": ["basepms-rooms"],
},
```

Commit and push — the next scheduled run will include it automatically.
