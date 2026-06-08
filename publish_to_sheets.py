"""
publish_to_sheets.py — Push the latest CSVs into a Google Sheet
===============================================================
Writes the full latest data into a single Google Sheet on two named tabs:

  • "Latest Room Data"      ← output/rooms_latest.csv
  • "Latest Contracts Data" ← output/contracts_latest.csv

(Step 2 will add "Room Data Changes Last 30 Days" / "Contracts Data Changes
Last 30 Days" tabs here too.)

Auth is a Google **service account** with the Sheets API. The workflow provides
two environment variables:

  GCP_SA_KEY  — the full service-account JSON key (the file's contents)
  SHEET_ID    — the target spreadsheet ID (the long string in the Sheet URL,
                between /d/ and /edit)

If either is missing the script prints a notice and exits 0, so the workflow
step is a no-op until you've added the secrets (no failed runs in the meantime).

Run locally:
    GCP_SA_KEY="$(cat key.json)" SHEET_ID="<id>" python publish_to_sheets.py
"""

import csv
import json
import os
import sys

# Each entry: (tab title, source CSV)
TABS = [
    ("Latest Room Data",      "output/rooms_latest.csv"),
    ("Latest Contracts Data", "output/contracts_latest.csv"),
]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
# Google Sheets hard limit is 50,000 chars per cell — truncate defensively.
MAX_CELL = 49000


def load_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    return [[(c if len(c) <= MAX_CELL else c[:MAX_CELL] + "…") for c in row]
            for row in rows]


def write_tab(spreadsheet, title, rows):
    import gspread

    n_rows = max(len(rows), 1)
    n_cols = max((len(r) for r in rows), default=1)
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=n_rows + 10, cols=n_cols + 2)
    ws.clear()
    ws.resize(rows=n_rows, cols=n_cols)
    # RAW so values land exactly as in the CSV (no auto number/date coercion).
    ws.update(values=rows, range_name="A1", value_input_option="RAW")
    print(f"  wrote '{title}': {len(rows)} rows × {n_cols} cols")


def main():
    sa_key = os.getenv("GCP_SA_KEY", "").strip()
    sheet_id = os.getenv("SHEET_ID", "").strip()
    if not sa_key or not sheet_id:
        print("GCP_SA_KEY / SHEET_ID not set — skipping Google Sheets publish.")
        return  # exit 0: no-op until secrets are configured

    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(json.loads(sa_key), scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    print(f"Publishing to spreadsheet: {sh.title}")

    for title, path in TABS:
        if not os.path.exists(path):
            print(f"  !! {path} missing — skipping '{title}'")
            continue
        write_tab(sh, title, load_rows(path))

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # surface a clear error in the Actions log
        print(f"ERROR publishing to Google Sheets: {e}", file=sys.stderr)
        raise
