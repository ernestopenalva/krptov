"""Archive stale watchlist entries that cannot be monitored without a pool address."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WATCHLIST_FILE = PROJECT_ROOT / "data" / "watchlist.json"
ARCHIVE_FILE = PROJECT_ROOT / "data" / "watchlist_archive.jsonl"


def parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chain", default="base")
    parser.add_argument("--older-than-hours", type=float, default=72)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    watchlist = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    selected = {}
    for key, entry in watchlist.items():
        if not isinstance(entry, dict):
            continue
        created = parse_iso(entry.get("created_at_utc"))
        if (
            str(entry.get("chain_id") or entry.get("chain") or "").lower() == args.chain.lower()
            and not entry.get("pair_address")
            and not entry.get("pool_address")
            and entry.get("status") == "novo"
            and entry.get("monitor_status") == "pendente"
            and created is not None
            and (now - created).total_seconds() >= args.older_than_hours * 3600
        ):
            selected[key] = entry

    print(f"candidatos={len(selected)}")
    for key, entry in selected.items():
        print(key, entry.get("created_at_utc"), entry.get("token_symbol"))

    if not args.apply or not selected:
        print("nenhuma alteracao aplicada" if not args.apply else "nenhum candidato")
        return

    backup = WATCHLIST_FILE.with_name(
        f"watchlist.json.backup-{now.strftime('%Y%m%dT%H%M%SZ')}"
    )
    shutil.copy2(WATCHLIST_FILE, backup)
    with ARCHIVE_FILE.open("a", encoding="utf-8") as handle:
        for key, entry in selected.items():
            handle.write(json.dumps({
                "archived_at_utc": now.isoformat(),
                "reason": "manual_archive_stale_missing_pool_address",
                "watchlist_key": key,
                "entry": entry,
            }, ensure_ascii=False) + "\n")

    remaining = {key: entry for key, entry in watchlist.items() if key not in selected}
    temporary = WATCHLIST_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(remaining, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(WATCHLIST_FILE)
    print(f"arquivados={len(selected)} backup={backup}")


if __name__ == "__main__":
    main()
