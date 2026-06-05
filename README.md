# Student Room Database — ETL

Fetches room types and contracts from 6 student accommodation brand APIs and exports two combined, sorted CSVs updated automatically every day via GitHub Actions.

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
- **Daily at 06:00 UTC** automatically
- **On demand** via the Actions tab → `Room Database ETL` → `Run workflow`

### 4. Run locally

```bash
pip install -r requirements.txt
python etl.py
```

Outputs land in `output/rooms_<timestamp>.csv` and `output/contracts_<timestamp>.csv`.

---

## How it works

```
GitHub Actions (daily cron 06:00 UTC)
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
        output/rooms_latest.csv
        output/contracts_latest.csv
                 │
                 ▼
        git commit & push to repo
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
