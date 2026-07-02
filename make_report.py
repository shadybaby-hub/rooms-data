"""
make_report.py — Weekday change report generator
=================================================
Compares the previously published CSVs against the freshly generated ones and
writes, into reports/<date>/:

  • rooms.csv / contracts.csv  — a dated snapshot copy of the new data
  • changes.md                 — a human-readable changelog vs the previous run

It is run by the GitHub Actions workflow after etl.py, once per weekday. The
"previous" files are the *_latest.csv committed by the prior run (the workflow
stashes them before they get overwritten). Any previous file may be missing
(e.g. the very first run) — that case is handled and produces a snapshot-only
report.

USAGE
-----
    python make_report.py <date> <prev_rooms> <prev_contracts> \
                          <new_rooms> <new_contracts> <reports_root>

All paths are plain files; <date> is the folder name (e.g. 2026-06-08).
"""

import csv
import os
import shutil
import sys
from datetime import datetime, timezone

# Row identity keys — what makes "the same" room / contract across runs.
# Contracts are now keyed on the API's stable, unique `instalment_id` (the new
# system exposes one per contracts_cache entry); brand/property/name are pulled
# from the row for display and the fetch-failure guard.
ROOM_KEY     = ("brand_name", "property", "room_type")
CONTRACT_KEY = ("instalment_id",)

# Cap per-section bullets so a huge churn day doesn't produce a giant file.
MAX_ROWS = 300


def load(path):
    if not path or not os.path.exists(path):
        return None  # None == "no previous data" (distinct from empty file)
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def index(rows, key_fields):
    out = {}
    for r in rows or []:
        out[tuple(r.get(k, "") for k in key_fields)] = r
    return out


def label(key):
    return " | ".join(p for p in key if p)


def contract_label(row):
    """Human-readable identity for a contract row. Contracts are keyed internally
    on instalment_id (not meaningful on its own), so reports render the API's
    descriptive `name`, prefixed with the brand."""
    name  = row.get("name", "")
    brand = row.get("brand_name", "")
    if name:
        return f"{brand} — {name}" if brand else name
    return " | ".join(p for p in (
        brand, row.get("property", ""), row.get("room_type", ""),
        row.get("instalment_id", "")) if p)


def units_artifact(ov, nv):
    """True when two prices differ only by the pence→pounds unit switch
    (price_pw was ×100 before etl.py started converting on 2026-06-10)."""
    try:
        a, b = float(ov), float(nv)
    except (TypeError, ValueError):
        return False
    return a == b * 100 or b == a * 100


def section(lines, title, items, render):
    lines.append(f"## {title} ({len(items)})")
    lines.append("")
    if not items:
        lines.append("_None_")
    else:
        for it in items[:MAX_ROWS]:
            lines.append(f"- {render(it)}")
        if len(items) > MAX_ROWS:
            lines.append(f"- … and {len(items) - MAX_ROWS} more")
    lines.append("")


def main():
    if len(sys.argv) != 7:
        sys.exit(
            "usage: make_report.py <date> <prev_rooms> <prev_contracts> "
            "<new_rooms> <new_contracts> <reports_root>"
        )
    (date_str, prev_rooms_p, prev_contracts_p,
     new_rooms_p, new_contracts_p, reports_root) = sys.argv[1:7]

    out_dir = os.path.join(reports_root, date_str)
    os.makedirs(out_dir, exist_ok=True)

    # 1. Dated snapshot copies of the new data
    shutil.copyfile(new_rooms_p,     os.path.join(out_dir, "rooms.csv"))
    shutil.copyfile(new_contracts_p, os.path.join(out_dir, "contracts.csv"))

    # 2. Load old + new
    prev_rooms_rows     = load(prev_rooms_p)
    prev_contracts_rows = load(prev_contracts_p)
    first_run = prev_rooms_rows is None and prev_contracts_rows is None

    old_rooms     = index(prev_rooms_rows,     ROOM_KEY)
    new_rooms     = index(load(new_rooms_p),   ROOM_KEY)
    old_contracts = index(prev_contracts_rows, CONTRACT_KEY)
    new_contracts = index(load(new_contracts_p), CONTRACT_KEY)

    # Brands present in the new run. If a whole brand is gone, that's almost
    # certainly a fetch failure (API timeout) rather than real removals, so we
    # don't report those rows as "removed".
    new_room_brands     = {k[0] for k in new_rooms}
    new_contract_brands = {r.get("brand_name", "") for r in new_contracts.values()}

    rooms_added     = sorted(k for k in new_rooms     if k not in old_rooms)
    rooms_removed   = sorted(k for k in old_rooms
                            if k not in new_rooms and k[0] in new_room_brands)
    contracts_added = sorted(k for k in new_contracts if k not in old_contracts)
    contracts_removed = sorted(
        k for k in old_contracts
        if k not in new_contracts
        and old_contracts[k].get("brand_name", "") in new_contract_brands
    )

    price_changes, avail_changes, bookable_changes = [], [], []
    for k in sorted(new_contracts):
        if k in old_contracts:
            o, n = old_contracts[k], new_contracts[k]
            op, np_ = o.get("price", ""), n.get("price", "")
            if op != np_ and not units_artifact(op, np_):
                price_changes.append((contract_label(n), op, np_))
            if o.get("available", "") != n.get("available", ""):
                avail_changes.append((contract_label(n),
                                      o.get("available", ""), n.get("available", "")))
            # `bookable` = matched in the `rooms` endpoint (actually bookable).
            # Skip when the old value is blank (schema-migration guard — the field
            # didn't exist in the previous run, so "" → x isn't a real change).
            ob, nb = o.get("bookable", ""), n.get("bookable", "")
            if ob != nb and ob != "":
                bookable_changes.append((contract_label(n), ob, nb))

    # Room-level field changes (only rooms present in both runs). The blank-old
    # guard again suppresses first-run schema-migration churn.
    room_avail_changes, amenity_changes, enquire_changes = [], [], []
    for k in sorted(new_rooms):
        if k in old_rooms:
            o, n = old_rooms[k], new_rooms[k]
            for field, bucket in (("quantity_available", room_avail_changes),
                                  ("amenities",          amenity_changes),
                                  ("enquire_status",     enquire_changes)):
                ov, nv = o.get(field, ""), n.get(field, "")
                if ov != nv and ov != "":
                    bucket.append((label(k), ov, nv))

    # 3. Write changelog
    lines = [
        f"# Change report — {date_str}",
        "",
        f"_Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}_",
        "",
    ]

    if first_run:
        lines += [
            "First run — no previous data to compare against. Snapshot only.",
            "",
            f"- Rooms in snapshot: **{len(new_rooms)}**",
            f"- Contracts in snapshot: **{len(new_contracts)}**",
            "",
        ]
    else:
        lines += [
            "## Summary",
            "",
            f"- Rooms: **+{len(rooms_added)}** added, **−{len(rooms_removed)}** removed",
            f"- Contracts: **+{len(contracts_added)}** added, "
            f"**−{len(contracts_removed)}** removed",
            f"- Price changes: **{len(price_changes)}**",
            f"- Room availability changes: **{len(room_avail_changes)}**",
            f"- Bookable changes: **{len(bookable_changes)}**",
            f"- Contract available-flag changes: **{len(avail_changes)}**",
            f"- Amenity changes: **{len(amenity_changes)}**",
            f"- Enquire-status changes: **{len(enquire_changes)}**",
            "",
        ]
        section(lines, "Price changes", price_changes,
                lambda t: f"{t[0]}: £{t[1]} → £{t[2]}")
        section(lines, "Room availability changes", room_avail_changes,
                lambda t: f"{t[0]}: {t[1]} → {t[2]}")
        section(lines, "Bookable changes", bookable_changes,
                lambda t: f"{t[0]}: {t[1]} → {t[2]}")
        section(lines, "Contracts added", contracts_added,
                lambda k: contract_label(new_contracts[k]))
        section(lines, "Contracts removed", contracts_removed,
                lambda k: contract_label(old_contracts[k]))
        section(lines, "Rooms added", rooms_added, label)
        section(lines, "Rooms removed", rooms_removed, label)
        section(lines, "Contract available-flag changes", avail_changes,
                lambda t: f"{t[0]}: {t[1]} → {t[2]}")
        section(lines, "Amenity changes", amenity_changes,
                lambda t: f"{t[0]}: {t[1]} → {t[2]}")
        section(lines, "Enquire-status changes", enquire_changes,
                lambda t: f"{t[0]}: {t[1]} → {t[2]}")

    with open(os.path.join(out_dir, "changes.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Report written to {out_dir}")


if __name__ == "__main__":
    main()
