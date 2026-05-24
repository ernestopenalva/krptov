import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
TOKEN_SCANNER_LATEST_FILE = DATA_DIR / "token_scanner_latest.json"
SOCIAL_INFERENCE_LATEST_FILE = DATA_DIR / "social_inference_latest.json"
SOCIAL_ALERTS_FILE = DATA_DIR / "social_alerts.json"
SOCIAL_POSTS_DIR = DATA_DIR / "social_posts"

DEFAULT_LIMIT = 10
DEFAULT_CONFIG = {
    "enabled": None,
    "scoring_mode": None,
    "disable_post_metric_alerts": None,
    "monitoring_window_hours": None,
    "max_posts_per_token": 8,
    "cycle_interval_seconds": 180,
    "max_new_tokens_per_day": None,
    "followers_alert_threshold": None,
    "excluded_author_usernames": [],
    "alert_rules": {},
    "badges": {},
    "automation": {},
}


def now_local():
    return datetime.now().replace(microsecond=0)


def today_stamp():
    return now_local().strftime("%Y-%m-%d")


def relative(path):
    try:
        return path.relative_to(PROJECT_ROOT)
    except ValueError:
        return path


def parse_iso(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value).replace("Z", "").replace(".000", ""))
    except ValueError:
        return None


def parse_scalar(value):
    value = str(value).strip()

    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.lower() in ("none", "null"):
        return None

    try:
        return int(value)
    except ValueError:
        pass

    try:
        return float(value)
    except ValueError:
        return value.strip('"').strip("'")


def load_json(path, default=None, warnings=None, criticals=None, critical=False):
    if default is None:
        default = {}

    if not path.exists():
        message = f"Arquivo ausente: {relative(path)}"
        if critical and criticals is not None:
            criticals.append(message)
        elif warnings is not None:
            warnings.append(message)
        return default

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        message = f"JSON invalido em {relative(path)}: {exc}"
        if critical and criticals is not None:
            criticals.append(message)
        elif warnings is not None:
            warnings.append(message)
        return default


def load_jsonl(path, warnings=None):
    rows = []

    if not path.exists():
        if warnings is not None:
            warnings.append(f"Arquivo ausente: {relative(path)}")
        return rows

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()

            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                if warnings is not None:
                    warnings.append(f"Linha JSONL invalida ignorada em {relative(path)}:{line_number}")
                continue

            if isinstance(data, dict):
                rows.append(data)
            elif warnings is not None:
                warnings.append(f"Linha JSONL nao-objeto ignorada em {relative(path)}:{line_number}")

    return rows


def load_simple_social_config(warnings):
    config = DEFAULT_CONFIG.copy()
    config["alert_rules"] = {}
    config["badges"] = {}
    config["automation"] = {}

    if not CONFIG_FILE.exists():
        warnings.append(f"Config ausente: {relative(CONFIG_FILE)}")
        return config

    in_social = False
    nested_section = None
    current_list_key = None

    for raw_line in CONFIG_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        no_comment = raw_line.split("#", 1)[0].rstrip()

        if not no_comment.strip():
            continue

        stripped = no_comment.strip()
        indent = len(raw_line) - len(raw_line.lstrip(" "))

        if indent == 0:
            in_social = stripped == "social_inference:"
            nested_section = None
            current_list_key = None
            continue

        if not in_social:
            continue

        if current_list_key and indent >= 4 and stripped.startswith("- "):
            config[current_list_key].append(parse_scalar(stripped[2:].strip()))
            continue

        if indent == 2 and stripped in ("alert_rules:", "badges:", "automation:"):
            nested_section = stripped[:-1]
            current_list_key = None
            continue

        if indent == 2:
            nested_section = None
            current_list_key = None

        if ":" not in stripped:
            continue

        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()

        if value == "" and key == "excluded_author_usernames":
            config[key] = []
            current_list_key = key
            continue

        if value == "":
            continue

        if nested_section and indent >= 4:
            config[nested_section][key] = parse_scalar(value)
        elif indent == 2 and key in config:
            config[key] = parse_scalar(value)

    return config


def get_nested(data, keys, default=None):
    current = data

    for key in keys:
        if not isinstance(current, dict):
            return default

        current = current.get(key)

        if current is None:
            return default

    return current


def normalize_address(value):
    if not value:
        return None

    value = str(value).strip().lower()
    if value.startswith("0x") and len(value) == 42:
        return value

    return value


def split_watchlist_key(key):
    if not key:
        return None, None

    key = str(key)
    if ":" not in key:
        return None, normalize_address(key)

    chain_id, token_address = key.split(":", 1)
    return chain_id or None, normalize_address(token_address)


def entry_token_address(key, entry):
    if isinstance(entry, dict) and entry.get("token_address"):
        return normalize_address(entry.get("token_address"))

    _, token_address = split_watchlist_key(key)
    return token_address


def entry_chain_id(key, entry):
    if isinstance(entry, dict) and entry.get("chain_id"):
        return str(entry.get("chain_id"))

    chain_id, _ = split_watchlist_key(key)
    return chain_id or "unknown"


def entry_watchlist_key(key, entry):
    if isinstance(entry, dict) and entry.get("watchlist_key"):
        return str(entry.get("watchlist_key"))

    token_address = entry_token_address(key, entry)
    chain_id = entry_chain_id(key, entry)

    if token_address and chain_id and chain_id != "unknown":
        return f"{chain_id}:{token_address}"

    return str(key) if key else None


def find_watchlist_entry(watchlist, token_address, chain_id=None, watchlist_key=None):
    token_address = normalize_address(token_address)
    chain_id = str(chain_id) if chain_id else None

    if watchlist_key:
        entry = watchlist.get(watchlist_key)
        if isinstance(entry, dict):
            return watchlist_key, entry

    if token_address and chain_id:
        exact_key = f"{chain_id}:{token_address}"
        entry = watchlist.get(exact_key)
        if isinstance(entry, dict):
            return exact_key, entry

    for key, entry in watchlist.items():
        if not isinstance(entry, dict):
            continue

        if token_address and entry_token_address(key, entry) != token_address:
            continue

        if chain_id and entry_chain_id(key, entry) != chain_id:
            continue

        return key, entry

    if token_address:
        entry = watchlist.get(token_address)
        if isinstance(entry, dict):
            return token_address, entry

    return None, None


def format_number(value, decimals=0):
    if value is None:
        return "indisponivel"

    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)

    if decimals == 0:
        return f"{int(round(number)):,}".replace(",", ".")

    return f"{number:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_money(value):
    if value is None:
        return "indisponivel"

    return f"US$ {format_number(value, 2)}"


def format_percent(value):
    if value is None:
        return "indisponivel"

    return f"{value:.1f}%".replace(".", ",")


def format_alert_reasons(reasons):
    if not reasons:
        return "nenhum"

    if not isinstance(reasons, list):
        reasons = [reasons]

    formatted = []
    legacy_markers = ("post_score", "bio_pattern", "blue", "author_badge")

    for reason in reasons:
        reason_text = str(reason)
        if any(marker in reason_text for marker in legacy_markers):
            reason_text = f"{reason_text} [legado/telemetria; nao e criterio atual]"
        formatted.append(reason_text)

    return ", ".join(formatted)


def format_age(timestamp):
    parsed = parse_iso(timestamp)

    if not parsed:
        return "indisponivel"

    seconds = max(0, int((now_local() - parsed).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes}min"

    return f"{minutes}min"


def symbol_name(token):
    symbol = get_nested(token, ["selected_pair", "baseToken", "symbol"])
    name = get_nested(token, ["selected_pair", "baseToken", "name"])

    if symbol and name:
        return f"{symbol} / {name}"
    if symbol:
        return symbol
    if name:
        return name

    return get_nested(token, ["token_profile", "description"], "indisponivel")


def token_chain(token):
    if not isinstance(token, dict):
        return None

    return (
        token.get("chain_id")
        or get_nested(token, ["selected_pair", "chainId"])
        or get_nested(token, ["token_profile", "chainId"])
    )


def token_created_at(token):
    return (
        token.get("token_created_at")
        or get_nested(token, ["selected_pair", "pairCreatedAt"])
        or token.get("first_seen_at")
    )


def alert_chain_id(alert):
    return alert.get("chain_id") or split_watchlist_key(alert.get("watchlist_key"))[0] or "unknown"


def parse_posts_filename(path):
    stem = path.stem
    if "_" not in stem:
        return None, normalize_address(stem), True

    chain_id, token_address = stem.split("_", 1)
    return chain_id or None, normalize_address(token_address), False


def social_value(token, key, default=None):
    social = token.get("social")

    if isinstance(social, dict) and key in social:
        return social.get(key, default)

    return token.get(key, default)


def token_metrics(token):
    metrics = token.get("scanner_metrics")

    if isinstance(metrics, dict):
        return metrics

    return {}


def audit_watchlist(watchlist, config, warnings, criticals):
    status_counts = Counter()
    reason_counts = Counter()
    active_tokens = []
    max_posts = int(config.get("max_posts_per_token") or 8)

    summary = {
        "total": len(watchlist),
        "status_counts": status_counts,
        "status_reason_counts": reason_counts,
        "chain_counts": Counter(),
        "social_monitoring_started": 0,
        "social_monitoring_expires": 0,
        "social_last_checked": 0,
        "social_tracked_tweet_ids": 0,
        "social_tracked_posts_count": 0,
        "telegram_alert_sent": 0,
        "best_alert_rank": 0,
        "active_tokens": active_tokens,
    }

    entries_missing_chain = 0
    entries_missing_address = 0
    entries_missing_key = 0
    status_by_chain = defaultdict(Counter)

    for key, token in watchlist.items():
        if not isinstance(token, dict):
            warnings.append(f"Token invalido na watchlist: {key}")
            continue

        status = token.get("status", "outros") or "outros"
        reason = token.get("status_reason")
        address = entry_token_address(key, token)
        chain_id = entry_chain_id(key, token)
        wl_key = entry_watchlist_key(key, token)

        if not token.get("chain_id"):
            entries_missing_chain += 1
            warnings.append(f"Watchlist entry sem chain_id: {wl_key or key}")
        if not token.get("token_address"):
            entries_missing_address += 1
            warnings.append(f"Watchlist entry sem token_address: {wl_key or key}")
        if not token.get("watchlist_key"):
            entries_missing_key += 1
            warnings.append(f"Watchlist entry sem watchlist_key: {wl_key or key}")

        status_counts[status] += 1
        reason_counts[str(reason)] += 1
        summary["chain_counts"][chain_id] += 1
        status_by_chain[chain_id][status] += 1

        started = social_value(token, "social_monitoring_started_at")
        expires = social_value(token, "social_monitoring_expires_at")
        checked = social_value(token, "social_last_checked_at")
        tracked_ids = social_value(token, "social_tracked_tweet_ids") or []
        tracked_count = social_value(token, "social_tracked_posts_count")
        telegram_sent = social_value(token, "telegram_alert_sent")
        best_rank = social_value(token, "best_alert_rank")

        if started:
            summary["social_monitoring_started"] += 1
        if expires:
            summary["social_monitoring_expires"] += 1
        if checked:
            summary["social_last_checked"] += 1
        if tracked_ids:
            summary["social_tracked_tweet_ids"] += 1
        if tracked_count is not None:
            summary["social_tracked_posts_count"] += 1
        if telegram_sent is True:
            summary["telegram_alert_sent"] += 1
        if best_rank is not None:
            summary["best_alert_rank"] += 1

        try:
            tracked_count_num = int(tracked_count or 0)
        except (TypeError, ValueError):
            tracked_count_num = 0

        if status == "ativo":
            active_tokens.append({
                "watchlist_key": wl_key,
                "address": address,
                "chain_id": chain_id,
                "symbol_name": symbol_name(token),
                "first_seen_at": token.get("first_seen_at"),
                "last_seen_at": token.get("last_seen_at"),
                "token_created_at": token.get("token_created_at"),
                "token_created_at_source": token.get("token_created_at_source"),
                "social_started": started,
                "social_expires": expires,
                "social_last_checked": checked,
                "tracked_posts": tracked_count,
                "best_alert_rank": best_rank,
                "post_metric_telemetry": social_value(token, "best_social_score"),
                "last_alert_reason": social_value(token, "last_alert_reason"),
            })

        if status == "ativo" and (not started or not expires):
            warnings.append(f"Ativo sem janela social: {wl_key or address}")

        expire_at = parse_iso(expires)
        if status == "ativo" and expire_at and now_local() >= expire_at:
            criticals.append(f"Ativo expirado ainda nao descartado: {wl_key or address}")

        if status == "descarte" and reason == "social_timeout" and not social_value(token, "social_monitoring_completed_at"):
            warnings.append(f"Descarte/social_timeout sem completed_at: {wl_key or address}")

        if status == "ativo" and not checked:
            warnings.append(f"Ativo sem social_last_checked_at: {wl_key or address}")

        if tracked_count_num > max_posts:
            criticals.append(f"social_tracked_posts_count acima do limite ({tracked_count_num}>{max_posts}): {wl_key or address}")

        if isinstance(tracked_ids, list) and len(tracked_ids) > max_posts:
            criticals.append(f"social_tracked_tweet_ids acima do limite ({len(tracked_ids)}>{max_posts}): {wl_key or address}")

        if best_rank is not None and not social_value(token, "last_alert_at"):
            warnings.append(f"Token com best_alert_rank mas sem last_alert_at: {wl_key or address}")

    summary["status_by_chain"] = {chain: dict(counter) for chain, counter in status_by_chain.items()}
    summary["entries_missing_chain_id"] = entries_missing_chain
    summary["entries_missing_token_address"] = entries_missing_address
    summary["entries_missing_watchlist_key"] = entries_missing_key

    return summary


def scanner_latest_summary(warnings):
    data = load_json(TOKEN_SCANNER_LATEST_FILE, default={}, warnings=warnings)
    counters = data.get("counters", {}) if isinstance(data, dict) else {}
    chains = data.get("chains_found") or counters.get("chains_found") or {}

    return {
        "generated_at": data.get("generated_at"),
        "tokens_returned": counters.get("tokens_returned"),
        "ethereum_found": counters.get("ethereum_found"),
        "base_found": counters.get("base_found"),
        "target_chains_found": counters.get("target_chains_found"),
        "target_chains_breakdown": counters.get("target_chains_breakdown"),
        "new_added": counters.get("new_added"),
        "updated": counters.get("updated"),
        "ignored_discarded": counters.get("ignored_discarded"),
        "ignored_external_status": counters.get("ignored_external_status"),
        "enrichment_errors": counters.get("enrichment_errors"),
        "watchlist_total": data.get("watchlist_total"),
        "chains_found": chains if isinstance(chains, dict) else {},
    }


def social_latest_summary(warnings, criticals):
    data = load_json(SOCIAL_INFERENCE_LATEST_FILE, default={}, warnings=warnings)
    errors = data.get("errors") if isinstance(data, dict) else []

    if errors:
        criticals.append(f"social_inference_latest contem errors: {len(errors)}")

    return {
        "timestamp": data.get("timestamp"),
        "tokens_checked": data.get("tokens_checked"),
        "alerts_generated": data.get("alerts_generated"),
        "tokens_expired": data.get("tokens_expired"),
        "tokens_blocked_by_daily_limit": data.get("tokens_blocked_by_daily_limit"),
        "errors": errors or [],
    }


def usage_summary(date, config, warnings):
    path = DATA_DIR / f"social_inference_usage_{date}.json"
    data = load_json(path, default={}, warnings=warnings)
    max_daily = data.get("max_new_tokens_per_day") or config.get("max_new_tokens_per_day")
    started = data.get("new_tokens_started", 0)
    tokens_started = data.get("tokens_started") or data.get("tokens_started_recent") or []
    started_by_chain = Counter()

    if isinstance(tokens_started, list):
        for item in tokens_started:
            if isinstance(item, dict):
                chain_id = item.get("chain_id") or split_watchlist_key(item.get("watchlist_key"))[0] or "unknown"
            else:
                chain_id = split_watchlist_key(str(item))[0] or "unknown"
            started_by_chain[chain_id] += 1

    try:
        percent = float(started) / float(max_daily) * 100 if max_daily else None
    except (TypeError, ValueError, ZeroDivisionError):
        percent = None

    return {
        "path": path,
        "new_tokens_started": started,
        "max_new_tokens_per_day": max_daily,
        "percent_used": percent,
        "tokens_started": tokens_started if isinstance(tokens_started, list) else [],
        "started_by_chain": dict(started_by_chain),
    }


def to_float(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def social_history_summary(date, limit, max_posts, warnings):
    path = DATA_DIR / f"social_inference_{date}.jsonl"
    rows = load_jsonl(path, warnings=warnings)
    tokens = set()
    rank_counts = Counter()
    chain_counts = Counter()
    posts_found = 0
    users_found = 0
    alerts_generated = 0
    expired = 0

    for row in rows:
        token = row.get("token_address")
        chain_id = row.get("chain_id") or split_watchlist_key(row.get("watchlist_key"))[0] or "unknown"
        chain_counts[chain_id] += 1
        if token:
            normalized_token = normalize_address(token)
            tokens.add(f"{chain_id}:{normalized_token}")
            tokens.add(normalized_token)

        posts = row.get("posts_found") or row.get("posts_count") or row.get("result_count") or 0
        users = row.get("users_found") or row.get("users_count") or 0

        try:
            posts_num = int(posts)
        except (TypeError, ValueError):
            posts_num = 0

        try:
            users_num = int(users)
        except (TypeError, ValueError):
            users_num = 0

        posts_found += posts_num
        users_found += users_num

        if posts_num > max_posts:
            warnings.append(
                f"Historico social registra posts_found>{max_posts} para {token}; "
                "bruto pode ter 10, mas analise deve filtrar."
            )

        if row.get("alert_generated") is True or row.get("alerts_generated"):
            alerts_generated += int(row.get("alerts_generated") or 1)

        if row.get("status_after") == "descarte":
            expired += 1

        rank = row.get("alert_rank")
        if rank is not None:
            rank_counts[str(rank)] += 1

    return {
        "path": path,
        "rows_total": len(rows),
        "unique_tokens_checked": len(tokens),
        "posts_found_total": posts_found,
        "users_found_total": users_found,
        "alerts_generated_total": alerts_generated,
        "tokens_expired": expired,
        "alert_rank_distribution": dict(rank_counts),
        "chain_counts": dict(chain_counts),
        "latest_events": rows[-limit:],
        "tokens": tokens,
    }


def load_alerts(date, warnings):
    daily_path = DATA_DIR / f"social_alerts_{date}.jsonl"
    accumulated = load_json(SOCIAL_ALERTS_FILE, default=[], warnings=warnings)

    if isinstance(accumulated, dict):
        accumulated = [accumulated]
    elif not isinstance(accumulated, list):
        accumulated = []

    daily = load_jsonl(daily_path, warnings=warnings)
    return accumulated, daily, daily_path


def alerts_summary(date, limit, watchlist, warnings, criticals):
    accumulated, daily, daily_path = load_alerts(date, warnings)
    rank_counts = Counter()
    chain_counts = Counter()
    token_counts = Counter()
    signature_counts = Counter()
    max_rank_by_token = defaultdict(float)

    for alert in accumulated:
        token = str(alert.get("token_address", "")).lower()
        chain_id = alert_chain_id(alert)
        rank = alert.get("alert_rank")
        signature = alert.get("alert_signature")

        if rank is not None:
            rank_counts[str(rank)] += 1
            try:
                max_rank_by_token[token] = max(max_rank_by_token[token], float(rank))
            except (TypeError, ValueError):
                pass

        if token:
            token_counts[f"{chain_id}:{token}"] += 1
        chain_counts[chain_id] += 1

        if not alert.get("chain_id"):
            warnings.append(f"Alerta sem chain_id: {token}")

        if token and signature:
            signature_counts[(token, str(rank), signature)] += 1

    for (token, rank, signature), count in signature_counts.items():
        if count > 1:
            warnings.append(f"Alerta repetido para mesmo token/rank/signature ({count}x): {token} rank={rank} {signature}")

    for token, max_rank in max_rank_by_token.items():
        _, wl_token = find_watchlist_entry(watchlist, token)
        if not isinstance(wl_token, dict):
            continue

        best_rank = social_value(wl_token, "best_alert_rank")
        try:
            best_rank_num = float(best_rank)
        except (TypeError, ValueError):
            best_rank_num = None

        if best_rank_num is None or best_rank_num < max_rank:
            criticals.append(f"best_alert_rank inconsistente na WL para {token}: WL={best_rank}, alertas={max_rank:g}")

    latest = sorted(accumulated, key=lambda alert: str(alert.get("timestamp", "")))[-limit:]

    return {
        "daily_path": daily_path,
        "total_accumulated": len(accumulated),
        "total_day": len(daily),
        "rank_counts": dict(rank_counts),
        "chain_counts": dict(chain_counts),
        "top_tokens": token_counts.most_common(limit),
        "latest_alerts": latest,
    }


def posts_summary(date, watchlist, max_posts, warnings, criticals):
    day_dir = SOCIAL_POSTS_DIR / date
    files = sorted(day_dir.glob("*.json")) if day_dir.exists() else []
    details = []
    tokens_with_files = set()

    if not day_dir.exists():
        warnings.append(f"Pasta de posts brutos ausente: {relative(day_dir)}")

    for path in files:
        payload = load_json(path, default={}, warnings=warnings)
        response = payload.get("response", {}) if isinstance(payload, dict) else {}
        data = response.get("data", []) if isinstance(response, dict) else []
        users = get_nested(response, ["includes", "users"], [])
        meta_result_count = get_nested(response, ["meta", "result_count"])
        filename_chain, filename_token, legacy_name = parse_posts_filename(path)
        token = normalize_address(payload.get("token_address") or filename_token)
        chain_id = payload.get("chain_id") or filename_chain
        watchlist_key = payload.get("watchlist_key")
        wl_key, wl_token = find_watchlist_entry(watchlist, token, chain_id, watchlist_key)

        if wl_token:
            chain_id = chain_id or entry_chain_id(wl_key, wl_token)
            watchlist_key = watchlist_key or entry_watchlist_key(wl_key, wl_token)

        chain_id = chain_id or "unknown"
        token_file_key = f"{chain_id}:{token}" if token else str(path)
        tokens_with_files.add(token_file_key)
        if token:
            tokens_with_files.add(token)
        tracked_posts = social_value(wl_token or {}, "social_tracked_posts_count")

        data_count = len(data) if isinstance(data, list) else 0
        users_count = len(users) if isinstance(users, list) else 0

        if legacy_name:
            warnings.append(f"Possivel arquivo antigo sem prefixo chain: {relative(path)}")
        if chain_id == "unknown":
            warnings.append(f"Post bruto sem chain_id: {relative(path)}")

        if data_count > max_posts:
            warnings.append(
                f"Bruto com {data_count} posts para {token_file_key}; API pode retornar minimo 10, "
                f"analise deve filtrar para {max_posts}."
            )

        try:
            tracked_num = int(tracked_posts or 0)
        except (TypeError, ValueError):
            tracked_num = 0

        if tracked_num > max_posts:
            criticals.append(f"tracked_posts>{max_posts} na WL para {watchlist_key or token_file_key}: {tracked_num}")

        details.append({
            "token": token,
            "chain_id": chain_id,
            "watchlist_key": watchlist_key,
            "timestamp": payload.get("timestamp"),
            "path": path,
            "data_count": data_count,
            "users_count": users_count,
            "meta_result_count": meta_result_count,
            "exists_in_watchlist": isinstance(wl_token, dict),
            "tracked_posts": tracked_posts,
        })

    for token, wl_token in watchlist.items():
        if not isinstance(wl_token, dict):
            continue

        wl_key = entry_watchlist_key(token, wl_token)
        wl_address = entry_token_address(token, wl_token)
        if wl_token.get("status") == "ativo" and wl_key not in tokens_with_files and wl_address not in tokens_with_files:
            warnings.append(f"Token ativo sem arquivo bruto hoje: {wl_key}")

    return {
        "day_dir": day_dir,
        "file_count": len(files),
        "tokens_with_files": len(tokens_with_files),
        "details": details,
    }


def errors_summary(date, limit, warnings):
    path = DATA_DIR / f"social_inference_error_{date}.json"
    payload = load_json(path, default=[], warnings=None)

    if not path.exists():
        return {"path": path, "errors": [], "status_counts": {}, "latest_errors": []}

    if isinstance(payload, dict):
        errors = payload.get("errors")
        if not isinstance(errors, list):
            errors = [payload]
    elif isinstance(payload, list):
        errors = payload
    else:
        errors = []
        warnings.append(f"Formato desconhecido em {relative(path)}")

    status_counts = Counter()

    for error in errors:
        if not isinstance(error, dict):
            continue
        status = error.get("status_code") or error.get("status") or "sem_status"
        status_counts[str(status)] += 1

    return {
        "path": path,
        "errors": errors,
        "status_counts": dict(status_counts),
        "latest_errors": errors[-limit:],
    }


def extract_log_cycles(path, limit, warnings):
    if not path.exists():
        warnings.append(f"Log ausente: {relative(path)}")
        return {"path": path, "count": 0, "first": None, "last": None, "latest": []}

    text = path.read_text(encoding="utf-8", errors="replace")
    matches = re.findall(r"Ciclo:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", text)
    return {
        "path": path,
        "count": len(matches),
        "first": matches[0] if matches else None,
        "last": matches[-1] if matches else None,
        "latest": matches[-limit:],
    }


def logs_summary(date, limit, config, warnings):
    scanner = extract_log_cycles(LOGS_DIR / f"token_scanner_{date}.txt", limit, warnings)
    social = extract_log_cycles(LOGS_DIR / f"social_inference_{date}.txt", limit, warnings)
    expected = config.get("cycle_interval_seconds")
    last_delta = None

    if len(social["latest"]) >= 2:
        prev = parse_iso(social["latest"][-2])
        last = parse_iso(social["latest"][-1])
        if prev and last:
            last_delta = (last - prev).total_seconds()

    return {
        "scanner": scanner,
        "social": social,
        "social_last_delta_seconds": last_delta,
        "social_expected_interval_seconds": expected,
    }


def validate_recency(name, timestamp, expected_seconds, warnings, oks):
    parsed = parse_iso(timestamp)

    if not parsed:
        warnings.append(f"{name} sem timestamp valido")
        return

    age_seconds = (now_local() - parsed).total_seconds()
    if expected_seconds and age_seconds > expected_seconds * 3:
        warnings.append(f"{name} antigo: {timestamp} ({format_age(timestamp)})")
    else:
        oks.append(f"{name} existe e parece recente: {timestamp}")


def print_section(title):
    print()
    print(title)
    print("-" * len(title))


def print_key_value(label, value):
    print(f"{label}: {value}")


def print_config(config):
    print_section("1. Config")
    print_key_value("enabled", config.get("enabled"))
    print_key_value("scoring_mode", config.get("scoring_mode"))
    print_key_value("disable_post_metric_alerts", config.get("disable_post_metric_alerts"))
    print_key_value("monitoring_window_hours", config.get("monitoring_window_hours"))
    print_key_value("max_posts_per_token", config.get("max_posts_per_token"))
    print_key_value("cycle_interval_seconds", config.get("cycle_interval_seconds"))
    print_key_value("max_new_tokens_per_day", config.get("max_new_tokens_per_day"))
    print_key_value("followers_alert_threshold", config.get("followers_alert_threshold"))
    print("Blacklist/excluded authors:")
    excluded = config.get("excluded_author_usernames") or []
    if excluded:
        for username in excluded:
            print(f"- {username}")
    else:
        print("- nenhum")
    print("Alert rules legadas:")
    for key, value in (config.get("alert_rules") or {}).items():
        print(f"- {key}: {value}")
    print("Badges/origem:")
    for key, value in (config.get("badges") or {}).items():
        print(f"- {key}: {value}")
    print("Automation/operator:")
    for key, value in (config.get("automation") or {}).items():
        print(f"- {key}: {value}")


def print_watchlist(summary, limit):
    print_section("2. Watchlist")
    print_key_value("Total de tokens", summary["total"])
    print(f"Status: {dict(summary['status_counts'])}")
    print(f"Status reason: {dict(summary['status_reason_counts'])}")
    print(f"Chains: {dict(summary['chain_counts'])}")
    print(f"Status por chain: {summary['status_by_chain']}")
    print_key_value("Entries sem chain_id", summary["entries_missing_chain_id"])
    print_key_value("Entries sem token_address", summary["entries_missing_token_address"])
    print_key_value("Entries sem watchlist_key", summary["entries_missing_watchlist_key"])
    print_key_value("Com social_monitoring_started_at", summary["social_monitoring_started"])
    print_key_value("Com social_monitoring_expires_at", summary["social_monitoring_expires"])
    print_key_value("Com social_last_checked_at", summary["social_last_checked"])
    print_key_value("Com social_tracked_tweet_ids", summary["social_tracked_tweet_ids"])
    print_key_value("Com social_tracked_posts_count", summary["social_tracked_posts_count"])
    print_key_value("Com telegram_alert_sent true", summary["telegram_alert_sent"])
    print_key_value("Com best_alert_rank", summary["best_alert_rank"])
    print("Tokens ativos:")
    for token in summary["active_tokens"][:limit]:
        print(
            f"- {token['chain_id']} | {token['watchlist_key']} | {token['address']} | {token['symbol_name']} | "
            f"created={token['token_created_at']} ({token['token_created_at_source']}) | "
            f"first={token['first_seen_at']} | last={token['last_seen_at']} | "
            f"social={token['social_started']} -> {token['social_expires']} | "
            f"checked={token['social_last_checked']} | tracked={token['tracked_posts']} | "
            f"origin_rank={token['best_alert_rank']} | post_metric_telemetry={token['post_metric_telemetry']} | "
            f"reason={token['last_alert_reason']}"
        )


def print_scanner_latest(summary):
    print_section("4. Scanner latest")
    for key in (
        "generated_at",
        "tokens_returned",
        "ethereum_found",
        "base_found",
        "target_chains_found",
        "new_added",
        "updated",
        "ignored_discarded",
        "ignored_external_status",
        "enrichment_errors",
        "watchlist_total",
    ):
        print_key_value(key, summary.get(key))
    print("Chains top:")
    for chain, count in Counter(summary.get("chains_found") or {}).most_common(10):
        print(f"- {chain}: {count}")
    print(f"Target chains breakdown: {summary.get('target_chains_breakdown')}")


def print_social_latest(summary):
    print_section("5. Social latest")
    for key in (
        "timestamp",
        "tokens_checked",
        "alerts_generated",
        "tokens_expired",
        "tokens_blocked_by_daily_limit",
    ):
        print_key_value(key, summary.get(key))
    errors = summary.get("errors") or []
    print_key_value("errors", len(errors))
    for error in errors:
        print(f"- {error}")


def print_usage(summary):
    print_section("6. Social usage diaria")
    print_key_value("Arquivo", relative(summary["path"]))
    print_key_value("new_tokens_started", summary["new_tokens_started"])
    print_key_value("max_new_tokens_per_day", summary["max_new_tokens_per_day"])
    print_key_value("percentual usado", format_percent(summary["percent_used"]))
    print(f"Started por chain: {summary['started_by_chain']}")
    print("Tokens started recentes:")
    for token in summary["tokens_started"][-10:]:
        if isinstance(token, dict):
            print(f"- chain={token.get('chain_id')} | key={token.get('watchlist_key')} | token={token.get('token_address')}")
        else:
            print(f"- {token}")


def print_history(summary, limit):
    print_section("7. Historico social JSONL")
    print_key_value("Arquivo", relative(summary["path"]))
    print_key_value("Linhas totais", summary["rows_total"])
    print_key_value("Tokens unicos verificados", summary["unique_tokens_checked"])
    print_key_value("posts_found total", summary["posts_found_total"])
    print_key_value("users_found total", summary["users_found_total"])
    print_key_value("alert_generated total", summary["alerts_generated_total"])
    print_key_value("tokens expirados", summary["tokens_expired"])
    print(f"Distribuicao origin alert_rank: {summary['alert_rank_distribution']}")
    print(f"Eventos por chain: {summary['chain_counts']}")
    print(f"Ultimos {limit} eventos:")
    for event in summary["latest_events"]:
        chain_id = event.get("chain_id") or split_watchlist_key(event.get("watchlist_key"))[0] or "unknown"
        print(
            f"- {event.get('timestamp')} | {chain_id} | {event.get('watchlist_key')} | {event.get('token_address')} | "
            f"posts={event.get('posts_found', event.get('posts_count'))} | "
            f"alert={event.get('alert_generated', event.get('alerts_generated'))} | "
            f"origin_rank={event.get('alert_rank')} | status_after={event.get('status_after')}"
        )


def print_alerts(summary, watchlist):
    print_section("8. Alertas")
    print_key_value("Total acumulado", summary["total_accumulated"])
    print_key_value("Total do dia", summary["total_day"])
    print(f"Alertas por rank de origem/reputacao: {summary['rank_counts']}")
    print(f"Alertas por chain: {summary['chain_counts']}")
    print("Top tokens por quantidade de alertas:")
    for token, count in summary["top_tokens"]:
        print(f"- {token}: {count}")
    print("Ultimos alertas:")
    for alert in summary["latest_alerts"]:
        token = normalize_address(alert.get("token_address"))
        chain_id = alert_chain_id(alert)
        wl_key, wl_token = find_watchlist_entry(watchlist, token, chain_id, alert.get("watchlist_key"))
        if not isinstance(wl_token, dict):
            wl_token = {}
        print(
            f"- {alert.get('timestamp')} | {chain_id} | {alert.get('watchlist_key') or wl_key} | {token} | origin_rank={alert.get('alert_rank')} | "
            f"reasons={format_alert_reasons(alert.get('alert_reasons'))} | "
            f"post_metric_telemetry={alert.get('best_post_score')} | "
            f"followers={format_number(alert.get('best_author_followers'))} | "
            f"status={wl_token.get('status', 'indisponivel')} | {symbol_name(wl_token)}"
        )


def print_posts(summary):
    print_section("9. Posts brutos salvos")
    print_key_value("Pasta", relative(summary["day_dir"]))
    print_key_value("Quantidade de arquivos", summary["file_count"])
    print_key_value("Tokens com arquivo bruto", summary["tokens_with_files"])
    for item in summary["details"]:
        print(
            f"- {item['chain_id']} | {item['watchlist_key']} | {item['token']} | ts={item['timestamp']} | posts={item['data_count']} | "
            f"users={item['users_count']} | result_count={item['meta_result_count']} | "
            f"WL={item['exists_in_watchlist']} | tracked={item['tracked_posts']} | "
            f"file={relative(item['path'])}"
        )


def print_errors(summary):
    print_section("10. Erros")
    print_key_value("Arquivo", relative(summary["path"]))
    print_key_value("Quantidade de erros", len(summary["errors"]))
    print(f"status_code por frequencia: {summary['status_counts']}")
    print("Ultimos erros:")
    for error in summary["latest_errors"]:
        if isinstance(error, dict):
            print(
                f"- token={error.get('token_address')} | status={error.get('status_code', error.get('status'))} | "
                f"payload={str(error.get('payload', error))[:240]}"
            )
        else:
            print(f"- {error}")


def print_logs(summary):
    print_section("11. Logs textuais")
    scanner = summary["scanner"]
    social = summary["social"]
    print(f"Scanner: ciclos={scanner['count']} | primeiro={scanner['first']} | ultimo={scanner['last']}")
    print(f"Social: ciclos={social['count']} | primeiro={social['first']} | ultimo={social['last']}")
    print(f"Social intervalo ultimo: {summary['social_last_delta_seconds']}s | esperado={summary['social_expected_interval_seconds']}s")
    print("Ultimos ciclos scanner:")
    for cycle in scanner["latest"]:
        print(f"- {cycle}")
    print("Ultimos ciclos social:")
    for cycle in social["latest"]:
        print(f"- {cycle}")


def print_diagnosis(oks, warnings, criticals):
    print_section("Diagnostico final")
    print("OK:")
    if oks:
        for item in oks:
            print(f"- {item}")
    else:
        print("- nenhum")

    print("Warnings:")
    if warnings:
        for item in warnings:
            print(f"- {item}")
    else:
        print("- nenhum")

    print("Criticos:")
    if criticals:
        for item in criticals:
            print(f"- {item}")
    else:
        print("- nenhum")


def build_audit(args):
    warnings = []
    criticals = []
    oks = []
    date = args.date
    config = load_simple_social_config(warnings)
    max_posts = int(config.get("max_posts_per_token") or 8)
    watchlist = load_json(WATCHLIST_FILE, default={}, warnings=warnings, criticals=criticals, critical=True)

    if config.get("scoring_mode") == "origin_reputation":
        oks.append("Config social em modo origin_reputation.")
    else:
        warnings.append(f"Config social_inference.scoring_mode inesperado: {config.get('scoring_mode')}")

    if config.get("disable_post_metric_alerts") is True:
        oks.append("Alertas por metrica de post desativados.")
    else:
        warnings.append("disable_post_metric_alerts nao esta true; conferir semantica do novo modelo.")

    badges = config.get("badges") or {}
    if badges.get("ignore_blue_as_alert") is True:
        oks.append("Selo azul isolado esta ignorado como criterio de alerta.")
    else:
        warnings.append("ignore_blue_as_alert nao esta true; selo azul pode estar ambiguo.")

    if not isinstance(watchlist, dict):
        criticals.append("watchlist ausente ou invalida")
        watchlist = {}
    else:
        oks.append("Watchlist carregada.")

    watchlist_info = audit_watchlist(watchlist, config, warnings, criticals)
    scanner_info = scanner_latest_summary(warnings)
    social_info = social_latest_summary(warnings, criticals)
    usage_info = usage_summary(date, config, warnings)
    history_info = social_history_summary(date, args.limit, max_posts, warnings)
    alerts_info = alerts_summary(date, args.limit, watchlist, warnings, criticals)
    posts_info = posts_summary(date, watchlist, max_posts, warnings, criticals)
    errors_info = errors_summary(date, args.limit, warnings)
    logs_info = logs_summary(date, args.limit, config, warnings)

    if scanner_info.get("generated_at"):
        validate_recency("Scanner latest", scanner_info.get("generated_at"), 300, warnings, oks)
    if social_info.get("timestamp"):
        validate_recency("Social latest", social_info.get("timestamp"), config.get("cycle_interval_seconds"), warnings, oks)

    if watchlist_info["active_tokens"]:
        oks.append("WL tem tokens ativos.")
    if watchlist_info["chain_counts"]:
        oks.append("Watchlist multi-chain carregada.")
    if watchlist_info["entries_missing_chain_id"] == 0:
        oks.append("Todas entries da WL tem chain_id.")
    if watchlist_info["entries_missing_watchlist_key"] == 0:
        oks.append("Todas entries da WL tem watchlist_key.")
    if watchlist_info["social_tracked_posts_count"]:
        oks.append("WL tem tokens com posts rastreados.")
    if not errors_info["errors"]:
        oks.append("Sem arquivo de erros HTTP recentes.")

    daily_started = to_float(usage_info["new_tokens_started"])
    daily_limit = to_float(usage_info["max_new_tokens_per_day"])
    if daily_limit and daily_started >= daily_limit:
        warnings.append("Uso diario atingiu max_new_tokens_per_day.")

    expected = logs_info["social_expected_interval_seconds"]
    last_delta = logs_info["social_last_delta_seconds"]
    if expected and last_delta and last_delta > expected * 2:
        warnings.append(f"Social pode estar atrasado: ultimo intervalo {last_delta}s, esperado {expected}s.")

    for token in watchlist_info["active_tokens"]:
        if (
            token["watchlist_key"] not in history_info["tokens"]
            and token["address"] not in history_info["tokens"]
            and social_info.get("tokens_checked", 0)
        ):
            warnings.append(f"Token ativo nao apareceu no historico social do dia: {token['watchlist_key']}")

    return {
        "date": date,
        "root": str(PROJECT_ROOT),
        "config": config,
        "watchlist": watchlist_info,
        "scanner_latest": scanner_info,
        "social_latest": social_info,
        "usage": usage_info,
        "history": history_info,
        "alerts": alerts_info,
        "posts": posts_info,
        "errors": errors_info,
        "logs": logs_info,
        "oks": oks,
        "warnings": warnings,
        "criticals": criticals,
        "_watchlist_raw": watchlist,
    }


def make_json_safe(value):
    if isinstance(value, Path):
        return str(relative(value))
    if isinstance(value, Counter):
        return dict(value)
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {str(key): make_json_safe(item) for key, item in value.items() if key != "_watchlist_raw"}
    if isinstance(value, list):
        return [make_json_safe(item) for item in value]
    return value


def print_human_report(audit, limit):
    print("=== KRPTO-V | Auditoria Operacional ===")
    print(f"Data: {audit['date']}")
    print(f"Root: {audit['root']}")
    print_config(audit["config"])
    print_watchlist(audit["watchlist"], limit)
    print_section("3. Lifecycle")
    print("Validacoes executadas. Ver Warnings/Criticos no diagnostico final.")
    print_scanner_latest(audit["scanner_latest"])
    print_social_latest(audit["social_latest"])
    print_usage(audit["usage"])
    print_history(audit["history"], limit)
    print_alerts(audit["alerts"], audit["_watchlist_raw"])
    print_posts(audit["posts"])
    print_errors(audit["errors"])
    print_logs(audit["logs"])
    print_diagnosis(audit["oks"], audit["warnings"], audit["criticals"])


def parse_args():
    parser = argparse.ArgumentParser(description="Audita scanner + social inference do KRPTO-V usando apenas arquivos locais.")
    parser.add_argument("--date", default=today_stamp(), help="Data analisada: YYYY-MM-DD. Default: hoje.")
    parser.add_argument("--json", action="store_true", help="Tambem imprime JSON consolidado.")
    parser.add_argument("--strict", action="store_true", help="Sai com codigo 1 se houver criticos.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Quantidade de eventos recentes a mostrar.")
    return parser.parse_args()


def main():
    args = parse_args()
    audit = build_audit(args)
    print_human_report(audit, args.limit)

    if args.json:
        print()
        print("JSON consolidado")
        print("----------------")
        print(json.dumps(make_json_safe(audit), ensure_ascii=False, indent=2))

    if args.strict and audit["criticals"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
