"""
publish_to_sheets.py — Push latest data + rolling change log to Google Sheets
=============================================================================
Writes four tabs into one Google Sheet, via a service account.

LATEST TABS (cleared & rewritten every run — show only the current data):
  • "Latest Room Data"      ← output/rooms_latest.csv      (+ a "Run Time" last column)
  • "Latest Contracts Data" ← output/contracts_latest.csv  (+ a "Run Time" last column)

CHANGE TABS (rolling 30-day log, kept INSIDE the Sheet — not committed to GitHub):
  • "Room Data Changes Last 30 Days"
  • "Contracts Data Changes Last 30 Days"
Each run diffs the previous run's *_latest.csv (stashed to /tmp/prev by the
workflow) against the new *_latest.csv, appends the day's change rows, and drops
rows whose Run Date is older than 30 days. The Sheet is the only store for this
log, so history persists across runs without any repo file.

ENV (provided by the workflow):
  GCP_SA_KEY      — service-account JSON (file contents)
  SHEET_ID        — target spreadsheet ID
  PREV_ROOMS      — previous rooms_latest.csv     (default /tmp/prev/rooms_latest.csv)
  PREV_CONTRACTS  — previous contracts_latest.csv (default /tmp/prev/contracts_latest.csv)

If GCP_SA_KEY / SHEET_ID are missing the script is a no-op (exit 0).

Run locally:
    GCP_SA_KEY="$(cat key.json)" SHEET_ID="<id>" \
    PREV_ROOMS=output/rooms_<old>.csv PREV_CONTRACTS=output/contracts_<old>.csv \
    python publish_to_sheets.py
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta

# ── Latest tabs ────────────────────────────────────────────────────────────────
# (title, csv path, field to explode one-row-per-value — None = leave as-is).
# "Latest Room Data" explodes image_urls so each URL gets its own row.
LATEST_TABS = [
    ("Latest Room Data",      "output/rooms_latest.csv",     "image_urls"),
    ("Latest Contracts Data", "output/contracts_latest.csv", None),
]
RUN_TIME_HEADER = "Run Time"

# ── Change tabs ─────────────────────────────────────────────────────────────────
ROOM_CHANGE_TAB     = "Room Data Changes Last 30 Days"
CONTRACT_CHANGE_TAB = "Contracts Data Changes Last 30 Days"
ROOM_CHANGE_HEADER = [
    "Run Date", "Brand", "Property", "City", "Room Type",
    "Change Field", "Change Type", "Old Content", "New Content",
]
CONTRACT_CHANGE_HEADER = [
    "Run Date", "Brand", "Property", "City", "Room Type",
    "Academic Year", "Duration",
    "Change Field", "Change Type", "Old Content", "New Content",
]

# Row identity (what makes "the same" room/contract across runs) + watched fields.
ROOM_KEY     = ("brand_name", "property", "room_type")
# Contracts are grouped (not uniquely keyed) — the same room/year/duration can
# offer several contracts (e.g. Sept + Jan intakes), matched up in diff_contracts.
CONTRACT_KEY = ("brand_name", "property", "city", "room_type", "academic_year",
                "contract_length_weeks")
ROOM_WATCH     = [
    ("quantity_available", "Quantity Available"),
    ("description",        "Description"),
    ("thumbnail_url",      "Thumbnail URL"),
    ("image_urls",         "Image URLs"),
]
CONTRACT_WATCH = [
    ("price_pw",   "Price (pw)"),
    ("available",  "Available"),
    ("start_date", "Start Date"),
    ("end_date",   "End Date"),
]

# Change Field values suppressed from the Room Data Changes tab. Quantity moves
# to the Contracts change tab (see room_quantity_changes); room add/remove is
# already represented there as New Contract / Sold Out.
ROOM_CHANGE_FIELD_EXCLUDE = {"Quantity Available", "(removed)", "(added)"}

WINDOW_DAYS = 30
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
MAX_CELL = 49000  # Sheets hard limit is 50,000 chars/cell

_NOW = datetime.now(timezone.utc)
RUN_TIME_STR = _NOW.strftime("%Y-%m-%d %H:%M UTC")
RUN_DATE_STR = _NOW.strftime("%Y-%m-%d")


# ── CSV helpers ─────────────────────────────────────────────────────────────────

def _clip(v):
    return v if len(v) <= MAX_CELL else v[:MAX_CELL] + "…"


def load_grid(path):
    """Raw CSV as list-of-lists, cells clipped."""
    with open(path, newline="", encoding="utf-8") as f:
        return [[_clip(c) for c in row] for row in csv.reader(f)]


def load_index(path, key_fields):
    """CSV as {key_tuple: row_dict}, or None if the file is absent."""
    if not path or not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8") as f:
        return {tuple(r.get(k, "") for k in key_fields): r for r in csv.DictReader(f)}


def load_groups(path, key_fields):
    """CSV as {key_tuple: [row_dicts]}, or None if the file is absent."""
    if not path or not os.path.exists(path):
        return None
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.setdefault(tuple(r.get(k, "") for k in key_fields), []).append(r)
    return out


def explode_on(grid, col):
    """Expand a `|`-joined cell into one row per value, repeating every other
    column. Rows with 0 or 1 values pass through unchanged. The header row
    (row 0) is never exploded. Display-only — the source CSV is untouched."""
    if not grid:
        return grid
    out = [grid[0]]
    for row in grid[1:]:
        cell  = row[col] if col < len(row) else ""
        parts = [p for p in cell.split("|") if p] if cell else []
        if len(parts) <= 1:
            out.append(row)
            continue
        for p in parts:
            new = list(row)
            new[col] = p
            out.append(new)
    return out


# ── Diffing ─────────────────────────────────────────────────────────────────────

def diff_rooms(old, new):
    rows = []
    new_brands = {k[0] for k in new}  # brands present this run
    for k in sorted(set(old) | set(new)):
        o, n = old.get(k), new.get(k)
        brand, prop, rtype = k
        if o and not n:
            if brand not in new_brands:
                continue  # whole brand missing — likely a fetch failure, not a removal
            rows.append([RUN_DATE_STR, brand, prop, o.get("city", ""), rtype,
                         "(removed)", "Removed", o.get("quantity_available", ""), ""])
        elif n and not o:
            rows.append([RUN_DATE_STR, brand, prop, n.get("city", ""), rtype,
                         "(added)", "Added", "", n.get("quantity_available", "")])
        else:
            for field, label in ROOM_WATCH:
                ov, nv = o.get(field, ""), n.get(field, "")
                if ov != nv:
                    rows.append([RUN_DATE_STR, brand, prop, n.get("city", ""), rtype,
                                 label, "Changed", _clip(ov), _clip(nv)])
    return rows


def _units_artifact(ov, nv):
    """True when two prices differ only by the pence→pounds unit switch
    (price_pw was ×100 before etl.py started converting on 2026-06-10).
    A genuine 100x price change is implausible, so this is safe to keep."""
    try:
        a, b = float(ov), float(nv)
    except (TypeError, ValueError):
        return False
    return a == b * 100 or b == a * 100


def _match_contracts(olds, news):
    """Pair up old/new contracts within one identity group: by contract_title
    first, then start_date, then whatever is left in stable order. Returns
    (pairs, unmatched_old, unmatched_new)."""
    pairs, olds, news = [], list(olds), list(news)
    for keyf in (lambda r: r.get("contract_title", ""),
                 lambda r: r.get("start_date", "")):
        lookup = {}
        for o in olds:
            lookup.setdefault(keyf(o), []).append(o)
        remaining = []
        for n in news:
            candidates = lookup.get(keyf(n))
            if candidates:
                pairs.append((candidates.pop(0), n))
            else:
                remaining.append(n)
        olds = [o for lst in lookup.values() for o in lst]
        news = remaining
    order = lambda r: (r.get("start_date", ""), r.get("contract_title", ""))
    olds.sort(key=order)
    news.sort(key=order)
    while olds and news:
        pairs.append((olds.pop(0), news.pop(0)))
    return pairs, olds, news


def diff_contracts(old, new):
    rows = []
    new_brands = {k[0] for k in new}  # brands present this run
    for k in sorted(set(old) | set(new)):
        brand, prop, city, rtype, ay, dur = k
        pairs, gone, added = _match_contracts(old.get(k, []), new.get(k, []))
        if brand in new_brands:  # else whole brand missing — likely a fetch failure
            for o in gone:
                rows.append([RUN_DATE_STR, brand, prop, city, rtype, ay, dur,
                             "(sold out)", "Sold Out", o.get("price_pw", ""), ""])
        for n in added:
            rows.append([RUN_DATE_STR, brand, prop, city, rtype, ay, dur,
                         "(new contract)", "New Contract", "", n.get("price_pw", "")])
        for o, n in pairs:
            for field, label in CONTRACT_WATCH:
                ov, nv = o.get(field, ""), n.get(field, "")
                if ov != nv:
                    if field == "price_pw" and _units_artifact(ov, nv):
                        continue
                    rows.append([RUN_DATE_STR, brand, prop, city, rtype, ay, dur,
                                 label, "Changed", ov, nv])
    return rows


def room_quantity_changes(old_rooms, new_rooms):
    """Room-level quantity_available changes, formatted for the *contracts*
    change tab (blank Academic Year / Duration). One row per room — sourced from
    the rooms indexes, so it doesn't fan out per contract the way CONTRACT_WATCH
    would. Only rooms present in both runs are considered, so added/removed rooms
    are excluded (they show on the contracts tab as New Contract / Sold Out)."""
    rows = []
    new_brands = {k[0] for k in new_rooms}
    for k in sorted(set(old_rooms) & set(new_rooms)):
        brand, prop, rtype = k
        if brand not in new_brands:
            continue
        o, n = old_rooms[k], new_rooms[k]
        ov, nv = o.get("quantity_available", ""), n.get("quantity_available", "")
        if ov != nv:
            rows.append([RUN_DATE_STR, brand, prop, n.get("city", ""), rtype,
                         "", "", "Quantity Available", "Changed", ov, nv])
    return rows


# ── Sheet writers ───────────────────────────────────────────────────────────────

def _write(ws, values, ncols):
    import gspread  # noqa: F401  (ensures dep present)
    ws.clear()
    ws.resize(rows=max(len(values), 1), cols=max(ncols, 1))
    ws.update(values=values, range_name="A1", value_input_option="RAW")


def _get_or_create(sh, title, ncols):
    import gspread
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=100, cols=ncols)


def write_latest_tab(sh, title, path, explode_field=None):
    grid = load_grid(path)
    if explode_field and grid and explode_field in grid[0]:
        grid = explode_on(grid, grid[0].index(explode_field))
    if grid:  # append the Run Time column (header + one value per data row)
        grid[0] = grid[0] + [RUN_TIME_HEADER]
        for i in range(1, len(grid)):
            grid[i] = grid[i] + [RUN_TIME_STR]
    ws = _get_or_create(sh, title, len(grid[0]) if grid else 1)
    _write(ws, grid, len(grid[0]) if grid else 1)
    print(f"  '{title}': {max(len(grid) - 1, 0)} rows + Run Time column")


def update_change_tab(sh, title, header, new_rows, reset=False, exclude_fields=()):
    ws = _get_or_create(sh, title, len(header))
    if reset:  # wipe the rolling log back to just the header row
        _write(ws, [header], len(header))
        print(f"  '{title}': reset to header only")
        return
    existing = ws.get_all_values()
    body = existing[1:] if (existing and existing[0] == header) else \
           ([] if not existing else existing)
    cutoff = (_NOW.date() - timedelta(days=WINDOW_DAYS))
    # Change Field column to filter on (F on rooms, H on contracts). Applies to
    # both existing rows and new ones, so excluded fields are also purged.
    cf_idx = header.index("Change Field") if exclude_fields else -1

    kept = []
    for row in body + new_rows:
        try:
            d = datetime.strptime(row[0], "%Y-%m-%d").date()
        except (ValueError, IndexError):
            continue  # drop malformed / stray header rows
        if d < cutoff:
            continue
        if exclude_fields and len(row) > cf_idx and row[cf_idx] in exclude_fields:
            continue
        kept.append(row)
    kept.sort(key=lambda r: r[0], reverse=True)  # newest first

    _write(ws, [header] + kept, len(header))
    print(f"  '{title}': +{len(new_rows)} new, {len(kept)} kept (last {WINDOW_DAYS}d)")


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    sa_key = os.getenv("GCP_SA_KEY", "").strip()
    sheet_id = os.getenv("SHEET_ID", "").strip()
    if not sa_key or not sheet_id:
        print("GCP_SA_KEY / SHEET_ID not set — skipping Google Sheets publish.")
        return

    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(json.loads(sa_key), scopes=SCOPES)
    sh = gspread.authorize(creds).open_by_key(sheet_id)
    print(f"Publishing to spreadsheet: {sh.title}")

    # 1. Latest tabs (only current data + Run Time column)
    for title, path, explode_field in LATEST_TABS:
        if os.path.exists(path):
            write_latest_tab(sh, title, path, explode_field)
        else:
            print(f"  !! {path} missing — skipping '{title}'")

    # 2. Change tabs (rolling 30-day log stored in the Sheet)
    reset = os.getenv("RESET_CHANGE_TABS", "").strip().lower() in ("1", "true", "yes")
    if reset:
        print("  RESET_CHANGE_TABS set — clearing both change tabs to headers only.")
        update_change_tab(sh, ROOM_CHANGE_TAB, ROOM_CHANGE_HEADER, [], reset=True)
        update_change_tab(sh, CONTRACT_CHANGE_TAB, CONTRACT_CHANGE_HEADER, [], reset=True)
        print("Done.")
        return

    prev_rooms = os.getenv("PREV_ROOMS", "/tmp/prev/rooms_latest.csv")
    prev_contracts = os.getenv("PREV_CONTRACTS", "/tmp/prev/contracts_latest.csv")

    old_rooms = load_index(prev_rooms, ROOM_KEY)
    new_rooms = load_index("output/rooms_latest.csv", ROOM_KEY) or {}
    old_contracts = load_groups(prev_contracts, CONTRACT_KEY)
    new_contracts = load_groups("output/contracts_latest.csv", CONTRACT_KEY) or {}

    room_changes = diff_rooms(old_rooms, new_rooms) if old_rooms is not None else []
    contract_changes = diff_contracts(old_contracts, new_contracts) if old_contracts is not None else []
    # Room quantity changes are logged on the *contracts* tab (one row per room),
    # and suppressed from the room tab below.
    if old_rooms is not None:
        contract_changes += room_quantity_changes(old_rooms, new_rooms)
    if old_rooms is None or old_contracts is None:
        print("  (no previous run found — change tabs pruned only, no new rows)")

    update_change_tab(sh, ROOM_CHANGE_TAB, ROOM_CHANGE_HEADER, room_changes,
                      exclude_fields=ROOM_CHANGE_FIELD_EXCLUDE)
    update_change_tab(sh, CONTRACT_CHANGE_TAB, CONTRACT_CHANGE_HEADER, contract_changes)

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR publishing to Google Sheets: {e}", file=sys.stderr)
        raise
