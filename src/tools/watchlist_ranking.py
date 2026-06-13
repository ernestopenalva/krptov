import argparse
import json
import os
import shutil
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WATCHLIST_FILE = PROJECT_ROOT / "data" / "watchlist.json"
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"


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


def load_social_settings(path=CONFIG_FILE, default_checks=3, default_min_age=30):
    if not path.exists():
        return default_checks, default_min_age

    in_social = False
    max_checks = default_checks
    min_age = default_min_age
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if not raw_line.startswith(" ") and stripped.endswith(":"):
            in_social = stripped[:-1] == "social_inference"
            continue
        if in_social and stripped.startswith("max_social_checks_per_token:"):
            _, value = stripped.split(":", 1)
            try:
                max_checks = int(value.strip().strip('"').strip("'"))
            except ValueError:
                max_checks = default_checks
        if in_social and stripped.startswith("min_social_age_minutes:"):
            _, value = stripped.split(":", 1)
            try:
                min_age = int(value.strip().strip('"').strip("'"))
            except ValueError:
                min_age = default_min_age

    return max_checks, min_age


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
    if number >= 1440:
        return f"{number / 1440:.1f}d"
    if number < 60:
        return f"{number:.0f}m"
    return f"{number / 60:.1f}h"


def format_money(value):
    number = numeric_or_none(value)
    if number is None:
        return "-"
    if number >= 1_000_000:
        return f"${number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"${number / 1_000:.1f}K"
    return f"${number:.0f}"


def format_compact_number(value):
    number = numeric_or_none(value)
    if number is None:
        return "-"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return f"{number:.0f}"


def display_name(entry):
    symbol = entry.get("token_symbol")
    quote = entry.get("quote_token")
    if symbol and quote and quote != "-":
        return f"{symbol}/{quote}"
    if symbol:
        return str(symbol)
    name = entry.get("token_name")
    if name:
        return str(name)
    return short_address(entry["token_address"])


def short_sanity(value):
    mapping = {
        "ok": "ok",
        "misleading_liquidity": "mis",
        "-": "-",
    }
    return mapping.get(value, str(value or "-"))


def compact_chain(value):
    return {"ethereum": "eth", "base": "base"}.get(value, str(value or "-"))


def compact_source(value):
    mapping = {
        "uniswap_v2": "uni_v2",
        "uniswap_v3": "uni_v3",
        "uniswap_v4": "uni_v4",
        "sushiswap_v2": "sushi",
        "aerodrome": "aero",
        "aerodrome_slipstream": "aero_s",
    }
    return mapping.get(value, str(value or "-"))


def compact_eligibility(value):
    mapping = {
        "eligible": "elig",
        "pending": "pend",
        "blocked_old_market": "old_m",
        "missing": "miss",
    }
    return mapping.get(value, str(value or "-"))


def compact_status(value):
    mapping = {
        "novo": "novo",
        "ativo": "ativ",
        "descarte": "desc",
        "pendente": "pend",
        "concluido": "conc",
        "-": "-",
    }
    return mapping.get(value, str(value or "-")[:4])


def compact_age_source(value):
    mapping = {
        "oldest_pair": "old",
        "selected_pair": "sel",
    }
    return mapping.get(value, str(value or "-"))


def compact_completed_reason(value):
    mapping = {
        "alert_sent": "alert",
        "max_social_checks": "maxchk",
        "social_timeout": "time",
        "social_eligibility_blocked_old_market": "old_m",
        None: "-",
        "": "-",
    }
    return mapping.get(value, str(value or "-")[:6])


def format_social_checks(value, max_checks):
    number = numeric_or_none(value)
    if number is None:
        return "-"
    if max_checks and max_checks > 0:
        return f"{int(number)}/{max_checks}"
    return str(int(number))


def normalize_entry(key, entry):
    if not isinstance(entry, dict):
        return None

    return {
        "watchlist_key": entry.get("watchlist_key") or key,
        "chain": entry.get("chain") or entry.get("chain_id") or "unknown",
        "source": entry.get("source") or "unknown",
        "quote_token": entry.get("quote_token") or "-",
        "status": entry.get("status") or "-",
        "social_status": entry.get("social_status") or "-",
        "social_checks_count": numeric_or_none(entry.get("social_checks_count")),
        "social_completed_reason": entry.get("social_completed_reason") or "-",
        "social_last_posts_returned": numeric_or_none(entry.get("social_last_posts_returned")),
        "telegram_alert_sent": entry.get("telegram_alert_sent") is True,
        "social_eligibility": entry.get("social_eligibility") or "missing",
        "market_score": numeric_or_none(entry.get("market_score")),
        "liquidity_usd": numeric_or_none(entry.get("liquidity_usd")),
        "quote_liquidity_usd": numeric_or_none(entry.get("quote_liquidity_usd")),
        "volume_h24": numeric_or_none(entry.get("volume_h24")),
        "txns_h24": numeric_or_none(entry.get("txns_h24")),
        "market_sanity_status": entry.get("market_sanity_status") or "-",
        "oldest_pair_age_minutes": numeric_or_none(entry.get("oldest_pair_age_minutes")),
        "minimum_token_age_inferred_minutes": numeric_or_none(entry.get("minimum_token_age_inferred_minutes")),
        "minimum_token_age_inferred_source": entry.get("minimum_token_age_inferred_source") or "-",
        "times_seen": int(entry.get("times_seen") or 0),
        "last_seen_at_utc": entry.get("last_seen_at_utc") or entry.get("created_at_utc") or "",
        "token_address": entry.get("token_address") or key.split(":", 1)[-1],
        "token_name": entry.get("token_name"),
        "token_symbol": entry.get("token_symbol"),
    }


def social_ready(entry, min_social_age_minutes=30):
    if entry.get("social_status") == "concluido":
        return False

    monitoring_active = (
        entry.get("social_status") == "ativo"
        or (entry["status"] == "ativo" and entry.get("social_status") in {"-", "pendente"})
    )
    age_ready = (
        monitoring_active
        or min_social_age_minutes <= 0
        or (
            entry["minimum_token_age_inferred_minutes"] is not None
            and entry["minimum_token_age_inferred_minutes"] >= min_social_age_minutes
        )
    )
    return (
        entry["status"] in {"novo", "ativo"}
        and (entry["social_eligibility"] == "eligible" or monitoring_active)
        and entry["market_score"] is not None
        and age_ready
    )


def filter_entries(entries, args, min_social_age_minutes=30):
    filtered = []

    for entry in entries:
        if args.chain and entry["chain"] != args.chain:
            continue
        if args.source and entry["source"] != args.source:
            continue
        if args.active_only and entry["status"] != "ativo" and entry["social_status"] != "ativo":
            continue
        if args.eligible_only and not social_ready(entry, min_social_age_minutes=min_social_age_minutes):
            continue
        filtered.append(entry)

    return filtered


def ranking_sort_key(entry, min_social_age_minutes=30):
    score = entry["market_score"]
    score_value = score if score is not None else -1
    eligible_rank = 1 if social_ready(entry, min_social_age_minutes=min_social_age_minutes) else 0
    return (eligible_rank, score_value, entry["last_seen_at_utc"])


def ranked_entries(watchlist, args):
    entries = [
        normalize_entry(key, entry)
        for key, entry in watchlist.items()
    ]
    entries = [entry for entry in entries if entry]
    _, min_social_age_minutes = load_social_settings()
    entries = filter_entries(entries, args, min_social_age_minutes=min_social_age_minutes)
    entries.sort(key=lambda entry: ranking_sort_key(entry, min_social_age_minutes=min_social_age_minutes), reverse=True)
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


def table_rows(entries, previous_positions, top, max_social_checks=5):
    rows = []

    for index, entry in enumerate(entries[:top], start=1):
        rows.append(
            {
                "pos": str(index),
                "score": format_score(entry["market_score"]),
                "chain": compact_chain(entry["chain"]),
                "source": compact_source(entry["source"]),
                "quote": entry["quote_token"],
                "status": compact_status(entry["status"]),
                "social_status": compact_status(entry["social_status"]),
                "checks": format_social_checks(entry["social_checks_count"], max_social_checks),
                "done": compact_completed_reason(entry["social_completed_reason"]),
                "elig": compact_eligibility(entry["social_eligibility"]),
                "minimum_age": format_age(entry["minimum_token_age_inferred_minutes"]),
                "liq": format_money(entry["liquidity_usd"]),
                "quote_liq": format_money(entry["quote_liquidity_usd"]),
                "vol": format_money(entry["volume_h24"]),
                "txns": format_compact_number(entry["txns_h24"]),
                "sanity": short_sanity(entry["market_sanity_status"]),
                "ca": entry["token_address"],
                "name": display_name(entry),
            }
        )

    return rows


def terminal_width():
    return shutil.get_terminal_size((120, 20)).columns


def table_columns(width=None):
    width = width or terminal_width()
    if width >= 145:
        return [
            ("pos", "#", 3),
            ("score", "Score", 6),
            ("chain", "Chn", 3),
            ("source", "Src", 6),
            ("quote", "Qte", 5),
            ("status", "WL", 4),
            ("social_status", "Soc", 4),
            ("checks", "Chk", 4),
            ("done", "Done", 6),
            ("elig", "Elig", 5),
            ("minimum_age", "MinAg", 5),
            ("liq", "LiqDS", 8),
            ("quote_liq", "QLiq", 8),
            ("vol", "Vol", 8),
            ("txns", "Tx24h", 6),
            ("sanity", "San", 3),
            ("ca", "CA", 42),
            ("name", "Nome", 18),
        ]

    return [
        ("pos", "#", 3),
        ("score", "Score", 5),
        ("chain", "Chn", 3),
        ("source", "Src", 5),
        ("quote", "Qte", 4),
        ("status", "WL", 4),
        ("social_status", "Soc", 4),
        ("checks", "Chk", 4),
        ("elig", "Elig", 5),
        ("minimum_age", "MinA", 4),
        ("liq", "LiqDS", 6),
        ("quote_liq", "QLiq", 6),
        ("vol", "Vol", 6),
        ("txns", "Tx24h", 5),
        ("sanity", "San", 3),
        ("name", "Nome", 8),
    ]


def print_table(rows, width=None):
    columns = table_columns(width)
    header = " ".join(title.ljust(width) for _, title, width in columns)
    print(header)
    print("-" * len(header))

    for row in rows:
        print(" ".join(str(row[key])[:width].ljust(width) for key, _, width in columns))


def print_summary(watchlist, entries, args, previous_positions):
    max_social_checks, min_social_age_minutes = load_social_settings()
    all_entries = [
        normalize_entry(key, entry)
        for key, entry in watchlist.items()
    ]
    all_entries = [entry for entry in all_entries if entry]
    social_candidates = [
        entry for entry in all_entries
        if social_ready(entry, min_social_age_minutes=min_social_age_minutes)
    ]
    social_active = [entry for entry in all_entries if entry["social_status"] == "ativo"]
    social_done = [entry for entry in all_entries if entry["social_status"] == "concluido"]

    print("=== KRPTO-V | Watchlist Ranking ===")
    print(f"Atualizado: {utc_now_iso()}")
    print(f"WL total: {len(all_entries)}")
    print(f"Visiveis no filtro: {len(entries)}")
    print(f"Candidatos social: {len(social_candidates)}")
    print(f"Idade minima social: {min_social_age_minutes}m")
    print(f"Por chain: {dict(Counter(entry['chain'] for entry in all_entries))}")
    print(f"Status WL: {dict(Counter(entry['status'] for entry in all_entries))}")
    print(f"Status social: {dict(Counter(entry['social_status'] for entry in all_entries))}")
    print(f"Social em observacao: {len(social_active)} | concluidos: {len(social_done)}")
    print(f"Conclusao social: {dict(Counter(entry['social_completed_reason'] for entry in social_done))}")
    print(f"Social eligibility: {dict(Counter(entry['social_eligibility'] for entry in all_entries))}")
    print(f"Market sanity: {dict(Counter(entry['market_sanity_status'] for entry in all_entries))}")
    if args.chain:
        print(f"Filtro chain: {args.chain}")
    if args.source:
        print(f"Filtro source: {args.source}")
    if args.eligible_only:
        print("Filtro: apenas candidatos elegiveis para social")
    if args.active_only:
        print("Filtro: apenas ativos")
    print()
    print_table(table_rows(entries, previous_positions, args.top, max_social_checks=max_social_checks))


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
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--chain", choices=["ethereum", "base"])
    parser.add_argument("--source")
    parser.add_argument("--eligible-only", action="store_true")
    parser.add_argument("--active-only", action="store_true")
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
