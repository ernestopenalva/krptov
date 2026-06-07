import argparse
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WATCHLIST_FILE = PROJECT_ROOT / "data" / "watchlist.json"


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_watchlist(path=WATCHLIST_FILE):
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if not isinstance(payload, dict):
        raise ValueError("data/watchlist.json precisa ser um dict.")

    return payload


def numeric_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def short_address(value):
    if not value:
        return "indisponivel"
    value = str(value)
    if len(value) <= 14:
        return value
    return f"{value[:6]}...{value[-4:]}"


def format_score(value):
    number = numeric_or_none(value)
    if number is None:
        return "-"
    return f"{number:.2f}"


def format_age(minutes):
    number = numeric_or_none(minutes)
    if number is None:
        return "-"
    if number < 60:
        return f"{number:.0f}m"
    return f"{number / 60:.1f}h"


def normalize_entry(key, entry):
    if not isinstance(entry, dict):
        return None

    return {
        "watchlist_key": entry.get("watchlist_key") or key,
        "chain": entry.get("chain") or entry.get("chain_id") or "unknown",
        "source": entry.get("source") or "unknown",
        "quote_token": entry.get("quote_token") or "-",
        "status": entry.get("status") or "-",
        "social_eligibility": entry.get("social_eligibility") or "missing",
        "market_score": numeric_or_none(entry.get("market_score")),
        "oldest_pair_age_minutes": numeric_or_none(entry.get("oldest_pair_age_minutes")),
        "times_seen": int(entry.get("times_seen") or 0),
        "last_seen_at_utc": entry.get("last_seen_at_utc") or entry.get("created_at_utc") or "",
        "token_address": entry.get("token_address") or key.split(":", 1)[-1],
    }


def social_ready(entry):
    return (
        entry["status"] in {"novo", "ativo"}
        and entry["social_eligibility"] == "eligible"
        and entry["market_score"] is not None
    )


def filter_entries(entries, args):
    filtered = []

    for entry in entries:
        if args.chain and entry["chain"] != args.chain:
            continue
        if args.source and entry["source"] != args.source:
            continue
        if args.eligible_only and not social_ready(entry):
            continue
        filtered.append(entry)

    return filtered


def ranking_sort_key(entry):
    score = entry["market_score"]
    score_value = score if score is not None else -1
    eligible_rank = 1 if social_ready(entry) else 0
    return (eligible_rank, score_value, entry["last_seen_at_utc"])


def ranked_entries(watchlist, args):
    entries = [
        normalize_entry(key, entry)
        for key, entry in watchlist.items()
    ]
    entries = [entry for entry in entries if entry]
    entries = filter_entries(entries, args)
    entries.sort(key=ranking_sort_key, reverse=True)
    return entries


def movement_marker(key, position, previous_positions):
    if not previous_positions:
        return "new"

    previous = previous_positions.get(key)
    if previous is None:
        return "new"
    if previous == position:
        return "="
    if previous > position:
        return f"up {previous - position}"
    return f"down {position - previous}"


def table_rows(entries, previous_positions, top):
    rows = []

    for index, entry in enumerate(entries[:top], start=1):
        rows.append(
            {
                "pos": str(index),
                "move": movement_marker(entry["watchlist_key"], index, previous_positions),
                "score": format_score(entry["market_score"]),
                "chain": entry["chain"],
                "source": entry["source"],
                "quote": entry["quote_token"],
                "elig": entry["social_eligibility"],
                "age": format_age(entry["oldest_pair_age_minutes"]),
                "seen": str(entry["times_seen"]),
                "token": short_address(entry["token_address"]),
            }
        )

    return rows


def print_table(rows):
    columns = [
        ("pos", "#", 4),
        ("move", "Mov", 8),
        ("score", "Score", 7),
        ("chain", "Chain", 9),
        ("source", "Source", 18),
        ("quote", "Quote", 7),
        ("elig", "Elig", 18),
        ("age", "AgeDS", 7),
        ("seen", "Seen", 5),
        ("token", "Token", 15),
    ]
    header = " ".join(title.ljust(width) for _, title, width in columns)
    print(header)
    print("-" * len(header))

    for row in rows:
        print(" ".join(str(row[key])[:width].ljust(width) for key, _, width in columns))


def print_summary(watchlist, entries, args, previous_positions):
    all_entries = [
        normalize_entry(key, entry)
        for key, entry in watchlist.items()
    ]
    all_entries = [entry for entry in all_entries if entry]
    social_candidates = [entry for entry in all_entries if social_ready(entry)]

    print("=== KRPTO-V | Watchlist Ranking ===")
    print(f"Atualizado: {utc_now_iso()}")
    print(f"WL total: {len(all_entries)}")
    print(f"Visiveis no filtro: {len(entries)}")
    print(f"Candidatos social: {len(social_candidates)}")
    print(f"Por chain: {dict(Counter(entry['chain'] for entry in all_entries))}")
    print(f"Social eligibility: {dict(Counter(entry['social_eligibility'] for entry in all_entries))}")
    if args.chain:
        print(f"Filtro chain: {args.chain}")
    if args.source:
        print(f"Filtro source: {args.source}")
    if args.eligible_only:
        print("Filtro: apenas candidatos elegiveis para social")
    print()
    print_table(table_rows(entries, previous_positions, args.top))


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def run_once(args, previous_positions=None):
    watchlist = load_watchlist(args.watchlist)
    entries = ranked_entries(watchlist, args)
    previous_positions = previous_positions or {}
    print_summary(watchlist, entries, args, previous_positions)
    return {
        entry["watchlist_key"]: index
        for index, entry in enumerate(entries, start=1)
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Mostra o ranking atual da Watchlist do KRPTO-V.",
    )
    parser.add_argument("--watchlist", type=Path, default=WATCHLIST_FILE)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--chain", choices=["ethereum", "base"])
    parser.add_argument("--source")
    parser.add_argument("--eligible-only", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=15)
    return parser.parse_args()


def main():
    args = parse_args()
    previous_positions = {}

    while True:
        if args.watch:
            clear_screen()
        previous_positions = run_once(args, previous_positions)
        if not args.watch:
            break
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    main()
