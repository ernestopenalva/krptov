import argparse
import json
import os
import shutil
import time
from collections import Counter
from datetime import datetime, time as datetime_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WATCHLIST_FILE = PROJECT_ROOT / "data" / "watchlist.json"
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"
SOCIAL_LATEST_FILE = PROJECT_ROOT / "data" / "social_inference_latest.json"
BRASILIA_TZ = ZoneInfo("America/Sao_Paulo")


def brt_now():
    return datetime.now(BRASILIA_TZ)


def parse_iso_datetime(value):
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BRASILIA_TZ)
    return parsed.astimezone(BRASILIA_TZ)


def load_watchlist(path=WATCHLIST_FILE):
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if not isinstance(payload, dict):
        raise ValueError("data/watchlist.json precisa ser um dict.")

    return payload


def load_json_file(path, default=None):
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return default


def load_social_settings(
    path=CONFIG_FILE,
    default_checks=3,
    default_min_age=0,
    default_min_quote_liquidity=1,
    default_max_new_tokens=10,
    default_max_active_tokens=40,
):
    if not path.exists():
        return (
            default_checks,
            default_min_age,
            default_min_quote_liquidity,
            default_max_new_tokens,
            default_max_active_tokens,
        )

    in_social = False
    max_checks = default_checks
    min_age = default_min_age
    min_quote_liquidity = default_min_quote_liquidity
    max_new_tokens = default_max_new_tokens
    max_active_tokens = default_max_active_tokens
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
        if in_social and stripped.startswith("min_quote_liquidity_usd:"):
            _, value = stripped.split(":", 1)
            try:
                min_quote_liquidity = float(value.strip().strip('"').strip("'"))
            except ValueError:
                min_quote_liquidity = default_min_quote_liquidity
        if in_social and stripped.startswith("max_new_tokens_per_cycle:"):
            _, value = stripped.split(":", 1)
            try:
                max_new_tokens = int(value.strip().strip('"').strip("'"))
            except ValueError:
                max_new_tokens = default_max_new_tokens
        if in_social and stripped.startswith("max_active_tokens_per_cycle:"):
            _, value = stripped.split(":", 1)
            try:
                max_active_tokens = int(value.strip().strip('"').strip("'"))
            except ValueError:
                max_active_tokens = default_max_active_tokens

    return max_checks, min_age, min_quote_liquidity, max_new_tokens, max_active_tokens


def load_wake_window_start(path=CONFIG_FILE, default_start="10:00"):
    if not path.exists():
        return default_start

    in_social = False
    in_wake_window = False
    start = default_start
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if not raw_line.startswith(" ") and stripped.endswith(":"):
            in_social = stripped[:-1] == "social_inference"
            in_wake_window = False
            continue
        if in_social and raw_line.startswith("  ") and not raw_line.startswith("    ") and stripped.endswith(":"):
            in_wake_window = stripped[:-1] == "wake_window"
            continue
        if in_social and in_wake_window and stripped.startswith("start:"):
            _, value = stripped.split(":", 1)
            start = value.strip().strip('"').strip("'")

    return start


def load_social_victor_policy(path=CONFIG_FILE):
    policy = {
        "cycle_interval_seconds": 120,
        "monitoring_window_hours": 2,
        "max_new_tokens_per_cycle": 1,
        "max_active_tokens_per_cycle": 0,
    }
    if not path.exists():
        return policy

    in_social = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if not raw_line.startswith(" ") and stripped.endswith(":"):
            in_social = stripped[:-1] == "social_inference"
            continue
        if not in_social or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        if key not in policy:
            continue
        try:
            policy[key] = int(value.strip().strip('"').strip("'"))
        except ValueError:
            continue
    return policy


def parse_hhmm(value, default="10:00"):
    try:
        hour_text, minute_text = str(value or default).split(":", 1)
        return datetime_time(hour=int(hour_text), minute=int(minute_text))
    except (TypeError, ValueError):
        hour_text, minute_text = default.split(":", 1)
        return datetime_time(hour=int(hour_text), minute=int(minute_text))


def social_usage_file(usage_date):
    if not usage_date:
        return None
    return PROJECT_ROOT / "data" / f"social_inference_usage_{usage_date}.json"


def social_history_file(usage_date):
    if not usage_date:
        return None
    return PROJECT_ROOT / "data" / f"social_inference_{usage_date}.jsonl"


def next_bucket_time(now_brt, bucket_minutes, usage_date=None):
    if bucket_minutes <= 0:
        return None

    try:
        social_date = datetime.strptime(usage_date, "%Y-%m-%d").date() if usage_date else now_brt.date()
    except (TypeError, ValueError):
        social_date = now_brt.date()

    start_time = parse_hhmm(load_wake_window_start())
    window_start = datetime.combine(social_date, start_time, tzinfo=BRASILIA_TZ)
    if now_brt < window_start:
        return window_start

    elapsed_minutes = max(0, int((now_brt - window_start).total_seconds() // 60))
    next_index = (elapsed_minutes // bucket_minutes) + 1
    return window_start + timedelta(minutes=next_index * bucket_minutes)


def load_post_budget_summary(now_brt=None):
    now_brt = now_brt or brt_now()
    latest = load_json_file(SOCIAL_LATEST_FILE, default={}) or {}
    usage_date = latest.get("usage_date")
    usage = load_json_file(social_usage_file(usage_date), default={}) if usage_date else {}
    usage = usage or {}
    budget = latest.get("post_budget") or {}

    posts_returned = int(usage.get("posts_returned") or latest.get("posts_returned_today") or budget.get("posts_returned") or 0)
    posts_returned_raw = int(usage.get("posts_returned_raw") or latest.get("posts_returned_raw_today") or 0)
    daily_budget = int(budget.get("daily_post_budget") or 0)
    bucket_target = int(budget.get("bucket_post_target") or 0)
    posts_allowed = int(budget.get("posts_allowed_so_far") or 0)
    bucket_minutes = int(budget.get("bucket_minutes") or 0)

    if not daily_budget and not bucket_target:
        return None

    previous_allowed = max(0, posts_allowed - bucket_target) if bucket_target else 0
    bucket_used = max(0, posts_returned - previous_allowed)
    if bucket_target:
        bucket_used = min(bucket_target, bucket_used)

    next_bucket = next_bucket_time(now_brt, bucket_minutes, usage_date=usage_date)

    return {
        "usage_date": usage_date or "-",
        "posts_returned": posts_returned,
        "posts_returned_raw": posts_returned_raw,
        "daily_budget": daily_budget,
        "posts_remaining_daily": max(0, daily_budget - posts_returned) if daily_budget > 0 else None,
        "bucket_used": bucket_used,
        "bucket_target": bucket_target,
        "next_bucket": next_bucket.strftime("%H:%M") if next_bucket else "-",
    }


def load_last_social_cycle_summary(usage_date, new_limit=10, active_limit=40):
    path = social_history_file(usage_date)
    if not path or not path.exists():
        return None

    last_timestamp = None
    records = []
    try:
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                timestamp = record.get("timestamp")
                if not timestamp:
                    continue
                if timestamp != last_timestamp:
                    last_timestamp = timestamp
                    records = [record]
                else:
                    records.append(record)
    except OSError:
        return None

    if not records:
        return None

    new_count = sum(1 for record in records if record.get("status_before") == "novo")
    active_count = sum(1 for record in records if record.get("status_before") == "ativo")
    total_count = len(records)
    return {
        "timestamp": last_timestamp,
        "new_count": new_count,
        "active_count": active_count,
        "total_count": total_count,
        "new_limit": new_limit,
        "active_limit": active_limit,
        "total_limit": new_limit + active_limit,
    }


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


def compact_chain(value):
    return {"ethereum": "eth", "base": "base", "bsc": "bsc"}.get(value, str(value or "-"))


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
        "social_monitoring_completed_at": entry.get("social_monitoring_completed_at"),
        "social_monitoring_started_at": entry.get("social_monitoring_started_at"),
        "social_monitoring_expires_at": entry.get("social_monitoring_expires_at"),
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


def social_ready(entry, min_social_age_minutes=0, min_quote_liquidity_usd=1):
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
    quote_liquidity_ready = (
        min_quote_liquidity_usd <= 0
        or (
            entry["quote_liquidity_usd"] is not None
            and entry["quote_liquidity_usd"] >= min_quote_liquidity_usd
        )
    )
    return (
        entry["status"] in {"novo", "ativo"}
        and (entry["social_eligibility"] == "eligible" or monitoring_active)
        and (entry["market_score"] is not None or monitoring_active)
        and age_ready
        and quote_liquidity_ready
    )


def filter_entries(entries, args, min_social_age_minutes=0, min_quote_liquidity_usd=1):
    filtered = []

    for entry in entries:
        if args.chain and entry["chain"] != args.chain:
            continue
        if args.source and entry["source"] != args.source:
            continue
        if args.active_only and entry["status"] != "ativo" and entry["social_status"] != "ativo":
            continue
        if args.eligible_only and not social_ready(
            entry,
            min_social_age_minutes=min_social_age_minutes,
            min_quote_liquidity_usd=min_quote_liquidity_usd,
        ):
            continue
        filtered.append(entry)

    return filtered


def ranking_sort_key(entry, min_social_age_minutes=0, min_quote_liquidity_usd=1):
    score = entry["market_score"]
    score_value = score if score is not None else -1
    ready = social_ready(
        entry,
        min_social_age_minutes=min_social_age_minutes,
        min_quote_liquidity_usd=min_quote_liquidity_usd,
    )
    monitoring_active = entry.get("social_status") == "ativo"
    social_victor_rank = 2 if monitoring_active and ready else 1 if ready else 0
    return (social_victor_rank, score_value, entry["last_seen_at_utc"])


def ranked_entries(watchlist, args):
    entries = [
        normalize_entry(key, entry)
        for key, entry in watchlist.items()
    ]
    entries = [entry for entry in entries if entry]
    _, min_social_age_minutes, min_quote_liquidity_usd, _, _ = load_social_settings()
    entries = filter_entries(
        entries,
        args,
        min_social_age_minutes=min_social_age_minutes,
        min_quote_liquidity_usd=min_quote_liquidity_usd,
    )
    entries.sort(
        key=lambda entry: ranking_sort_key(
            entry,
            min_social_age_minutes=min_social_age_minutes,
            min_quote_liquidity_usd=min_quote_liquidity_usd,
        ),
        reverse=True,
    )
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


def format_monitoring_window(entry, current_time=None):
    if entry.get("social_status") == "concluido":
        return "fim"
    if entry.get("social_status") != "ativo":
        return "fila"
    expires_at = parse_iso_datetime(entry.get("social_monitoring_expires_at"))
    if not expires_at:
        return "?"
    seconds = max(0, int((expires_at - (current_time or brt_now())).total_seconds()))
    if seconds <= 0:
        return "exp"
    minutes = (seconds + 59) // 60
    if minutes >= 60:
        return f"{minutes / 60:.1f}h"
    return f"{minutes}m"


def table_rows(entries, previous_positions, top, max_social_checks=0, current_time=None):
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
                "window": format_monitoring_window(entry, current_time=current_time),
                "elig": compact_eligibility(entry["social_eligibility"]),
                "minimum_age": format_age(entry["minimum_token_age_inferred_minutes"]),
                "liq": format_money(entry["liquidity_usd"]),
                "quote_liq": format_money(entry["quote_liquidity_usd"]),
                "vol": format_money(entry["volume_h24"]),
                "txns": format_compact_number(entry["txns_h24"]),
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
            ("window", "Jan", 5),
            ("elig", "Elig", 5),
            ("minimum_age", "MinAg", 5),
            ("liq", "LiqDS", 8),
            ("quote_liq", "QLiq", 8),
            ("vol", "Vol", 8),
            ("txns", "Tx24h", 6),
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
        ("window", "Jan", 4),
        ("elig", "Elig", 5),
        ("minimum_age", "MinA", 4),
        ("liq", "LiqDS", 6),
        ("quote_liq", "QLiq", 6),
        ("vol", "Vol", 6),
        ("txns", "Tx24h", 5),
        ("name", "Nome", 8),
    ]


def print_table(rows, width=None):
    columns = table_columns(width)
    header = " ".join(title.ljust(width) for _, title, width in columns)
    print(header)
    print("-" * len(header))

    for row in rows:
        print(" ".join(str(row[key])[:width].ljust(width) for key, _, width in columns))


def social_completion_summary(entries, current_date):
    total = Counter(entry["social_completed_reason"] for entry in entries)
    today = Counter()
    for entry in entries:
        completed_at = parse_iso_datetime(entry.get("social_monitoring_completed_at"))
        if completed_at and completed_at.date() == current_date:
            today[entry["social_completed_reason"]] += 1
    return total, today


def format_social_completion_summary(total, today):
    if not total:
        return "nenhuma"
    labels = {
        "-": "sem motivo",
        "social_timeout": "timeout",
        "alert_sent": "alert",
        "max_social_checks": "maxchk legado",
        "low_quote_liquidity": "lowliq",
    }
    parts = []
    for reason, count in total.items():
        label = labels.get(reason, str(reason))
        parts.append(f"{label} {today.get(reason, 0)}/{count}")
    return " | ".join(parts)


def print_summary(watchlist, entries, args, previous_positions):
    (
        max_social_checks,
        min_social_age_minutes,
        min_quote_liquidity_usd,
        max_new_tokens,
        max_active_tokens,
    ) = load_social_settings()
    all_entries = [
        normalize_entry(key, entry)
        for key, entry in watchlist.items()
    ]
    all_entries = [entry for entry in all_entries if entry]
    social_candidates = [
        entry for entry in all_entries
        if social_ready(
            entry,
            min_social_age_minutes=min_social_age_minutes,
            min_quote_liquidity_usd=min_quote_liquidity_usd,
        )
    ]
    social_done = [entry for entry in all_entries if entry["social_status"] == "concluido"]
    current_brt = brt_now()
    policy = load_social_victor_policy()
    post_budget = load_post_budget_summary(now_brt=current_brt)
    usage_date = post_budget.get("usage_date") if post_budget else current_brt.strftime("%Y-%m-%d")
    history_date = current_brt.strftime("%Y-%m-%d")
    last_cycle = load_last_social_cycle_summary(
        history_date,
        new_limit=max_new_tokens,
        active_limit=max_active_tokens,
    )
    completion_total, completion_today = social_completion_summary(social_done, current_brt.date())

    print("=== KRPTO-V | Watchlist Ranking ===")
    print(f"Atualizado: {current_brt.isoformat(timespec='seconds')}")
    print(f"WL total: {len(all_entries)}")
    print(f"Visiveis no filtro: {len(entries)} | Candidatos social: {len(social_candidates)}")
    print(
        f"Politica Social Victor: ciclo {policy['cycle_interval_seconds']}s | "
        f"janela {policy['monitoring_window_hours']}h | idade minima desabilitada"
    )
    print(
        f"Selecao por ciclo: {policy['max_new_tokens_per_cycle']} novo melhor rankeado + "
        f"ativos {'sem teto' if policy['max_active_tokens_per_cycle'] <= 0 else policy['max_active_tokens_per_cycle']} | "
        f"QLiq minima: US$ {min_quote_liquidity_usd:g}"
    )
    if last_cycle:
        print(
            f"Ultimo ciclo: novos {last_cycle['new_count']}/{last_cycle['new_limit']} | "
            f"ativos {last_cycle['active_count']}/{'sem teto' if last_cycle['active_limit'] <= 0 else last_cycle['active_limit']} | "
            f"total {last_cycle['total_count']}"
        )
    if post_budget:
        daily_total = post_budget["daily_budget"] or "sem limite"
        remaining = post_budget["posts_remaining_daily"]
        print(
            f"Posts hoje: unicos {post_budget['posts_returned']}/{daily_total} | "
            f"brutos {post_budget['posts_returned_raw']} | "
            f"restante {'sem limite' if remaining is None else remaining}"
        )
    print(f"Por chain: {dict(Counter(entry['chain'] for entry in all_entries))}")
    print(f"Status WL: {dict(Counter(entry['status'] for entry in all_entries))}")
    print(f"Status social: {dict(Counter(entry['social_status'] for entry in all_entries))}")
    print(f"Conclusao social hoje/total: {format_social_completion_summary(completion_total, completion_today)}")
    print(f"Social eligibility: {dict(Counter(entry['social_eligibility'] for entry in all_entries))}")
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
    parser.add_argument("--chain", choices=["ethereum", "base", "bsc"])
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
