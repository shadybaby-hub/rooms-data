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
ROOM_KEY     = ("brand_name", "property", "room_type")
CONTRACT_KEY = ("brand_name", "property", "room_type", "contract_title")

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

    rooms_added     = sorted(k for k in new_rooms     if k not in old_rooms)
    rooms_removed   = sorted(k for k in old_rooms     if k not in new_rooms)
    contracts_added = sorted(k for k in new_contracts if k not in old_contracts)
    contracts_removed = sorted(k for k in old_contracts if k not in new_contracts)

    price_changes, avail_changes = [], []
    for k in sorted(new_contracts):
        if k in old_contracts:
            o, n = old_contracts[k], new_contracts[k]
            if o.get("price_pw", "") != n.get("price_pw", ""):
                price_changes.append(
                    (k, o.get("price_pw", ""), n.get("price_pw", ""),
                     n.get("currency_symbol", ""))
                )
            if o.get("available", "") != n.get("available", ""):
                avail_changes.append((k, o.get("available", ""), n.get("available", "")))

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
            f"- Availability changes: **{len(avail_changes)}**",
            "",
        ]
        section(lines, "Price changes", price_changes,
                lambda t: f"{label(t[0])}: {t[3]}{t[1]} → {t[3]}{t[2]}")
        section(lines, "Contracts added", contracts_added, label)
        section(lines, "Contracts removed", contracts_removed, label)
        section(lines, "Rooms added", rooms_added, label)
        section(lines, "Rooms removed", rooms_removed, label)
        section(lines, "Availability changes", avail_changes,
                lambda t: f"{label(t[0])}: {t[1]} → {t[2]}")

    with open(os.path.join(out_dir, "changes.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Report written to {out_dir}")


if __name__ == "__main__":
    main()
