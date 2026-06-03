import argparse
import json
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_ALERTS_FILE = DATA_DIR / "social_alerts.json"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
SOCIAL_POSTS_DIR = DATA_DIR / "social_posts"
DEFAULT_LIMIT = 20


def resolve_path(value):
    path = Path(value)

    if path.is_absolute():
        return path

    return PROJECT_ROOT / path


def relative_path(path):
    try:
        return path.relative_to(PROJECT_ROOT)
    except ValueError:
        return path


def parse_iso(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError:
        return None


def sort_key(alert):
    parsed = parse_iso(alert.get("timestamp"))

    if parsed:
        return parsed

    return datetime.min


def read_json_alerts(path):
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"JSON malformado em {path}: {exc}") from exc

    if isinstance(data, list):
        return data, []

    if isinstance(data, dict):
        return [data], []

    return [], [f"Arquivo JSON nao contem lista nem objeto: {path}"]


def read_jsonl_alerts(path):
    alerts = []
    warnings = []

    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()

            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(f"Linha JSONL malformada ignorada: {line_number}")
                continue

            if isinstance(data, dict):
                alerts.append(data)
            else:
                warnings.append(f"Linha JSONL ignorada por nao ser objeto: {line_number}")

    return alerts, warnings


def load_alerts(path):
    if not path.exists():
        raise SystemExit(f"Arquivo de alertas nao encontrado: {path}")

    suffix = path.suffix.lower()

    if suffix == ".jsonl":
        return read_jsonl_alerts(path)

    return read_json_alerts(path)


def load_watchlist():
    if not WATCHLIST_FILE.exists():
        return {}

    try:
        with WATCHLIST_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        return {}

    if not isinstance(data, dict):
        return {}

    return data


def get_token_from_watchlist(watchlist, token_address):
    _, entry = find_watchlist_entry(watchlist, token_address)
    return entry


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
    if isinstance(entry, dict) and entry.get("chain"):
        return str(entry.get("chain"))

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


def find_watchlist_entry(watchlist, token_address, chain_id=None):
    token_address = normalize_address(token_address)
    chain_id = str(chain_id) if chain_id else None

    if not token_address:
        return None, None

    if chain_id:
        exact_key = f"{chain_id}:{token_address}"
        entry = watchlist.get(exact_key)
        if isinstance(entry, dict):
            return exact_key, entry

    for key, entry in watchlist.items():
        if not isinstance(entry, dict):
            continue

        if entry_token_address(key, entry) != token_address:
            continue

        if chain_id and entry_chain_id(key, entry) != chain_id:
            continue

        return key, entry

    entry = watchlist.get(token_address)
    if isinstance(entry, dict):
        return token_address, entry

    return None, None


def get_nested(data, keys, default=None):
    current = data

    for key in keys:
        if not isinstance(current, dict):
            return default

        current = current.get(key)

        if current is None:
            return default

    return current


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


def format_bool(value):
    if value is True:
        return "sim"
    if value is False:
        return "nao"
    return "indisponivel"


def first_list_value(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def format_affiliation(prefix, source):
    found = source.get(f"{prefix}_affiliation_found")
    if not found:
        return "nao"

    raw = source.get(f"{prefix}_affiliation_raw")
    name = source.get(f"{prefix}_affiliation_name")
    username = source.get(f"{prefix}_affiliation_username")
    affiliation_id = source.get(f"{prefix}_affiliation_id")
    affiliation_type = source.get(f"{prefix}_affiliation_type")
    url = source.get(f"{prefix}_affiliation_url")
    badge_url = source.get(f"{prefix}_affiliation_badge_url")

    if isinstance(raw, dict):
        name = name or raw.get("description") or raw.get("name") or raw.get("label")
        username = username or raw.get("username") or raw.get("screen_name") or raw.get("handle")
        affiliation_id = affiliation_id or first_list_value(raw.get("user_id")) or raw.get("id")
        affiliation_type = affiliation_type or raw.get("type") or raw.get("verified_type")
        url = url or raw.get("url")
        badge_url = badge_url or raw.get("badge_url")

    parts = []

    if name:
        parts.append(str(name))
    if username:
        parts.append(f"@{username}")
    if url:
        parts.append(str(url))
    if affiliation_id:
        parts.append(f"id={affiliation_id}")
    if affiliation_type:
        parts.append(f"type={affiliation_type}")
    if badge_url:
        parts.append(f"badge={badge_url}")
    if raw and not parts:
        parts.append("raw=" + json.dumps(raw, ensure_ascii=False))

    if not parts:
        return "sim"

    return "sim (" + ", ".join(parts) + ")"


def print_author_summary(title, summary):
    print(f"{title}:")
    if not isinstance(summary, dict):
        print("- indisponivel")
        return

    username = summary.get("username") or "indisponivel"
    print(f"- Author: @{username}")
    print(f"- Followers: {format_number(summary.get('followers'))}")
    print(f"- Verified: {format_bool(summary.get('verified'))}")
    print(f"- Verified type: {summary.get('verified_type', 'indisponivel')}")
    print(
        "- Affiliation: "
        + format_affiliation("", {
            "_affiliation_found": summary.get("affiliation_found"),
            "_affiliation_name": summary.get("affiliation_name"),
            "_affiliation_username": summary.get("affiliation_username"),
            "_affiliation_id": summary.get("affiliation_id"),
            "_affiliation_type": summary.get("affiliation_type"),
            "_affiliation_raw": summary.get("affiliation_raw"),
        })
    )


def print_trigger_posts(posts):
    print("Trigger posts:")
    if not posts:
        print("- indisponivel")
        return

    for post in posts[:3]:
        print(f"- URL: {post.get('url') or 'indisponivel'}")
        print(f"  Author: @{post.get('author_username', 'indisponivel')}")
        print(f"  Created: {post.get('created_at', 'indisponivel')}")
        metrics = post.get("public_metrics") or {}
        print(
            "  Metrics: "
            f"likes={metrics.get('like_count', 0)} "
            f"replies={metrics.get('reply_count', 0)} "
            f"retweets={metrics.get('retweet_count', 0)} "
            f"quotes={metrics.get('quote_count', 0)} "
            f"impressions={metrics.get('impression_count', 0)}"
        )
        text = (post.get("text") or "").strip()
        if text:
            print("  Text:")
            for line in text.splitlines()[:6]:
                print(f"    {line}")


def format_list(values):
    if not values:
        return "nenhum"

    if isinstance(values, list):
        return ", ".join(str(value) for value in values)

    return str(values)


def format_alert_reason(reason):
    reason = str(reason)
    legacy_markers = ("post_score", "bio_pattern", "blue", "author_badge")

    if any(marker in reason for marker in legacy_markers):
        return f"{reason} [legado/telemetria; nao e criterio atual]"

    return reason


def short_address(value):
    if not value:
        return "indisponivel"

    value = str(value)

    if len(value) <= 14:
        return value

    return f"{value[:6]}...{value[-4:]}"


def token_symbol(watch_token):
    return (
        watch_token.get("token_symbol")
        or get_nested(watch_token, ["selected_pair", "baseToken", "symbol"])
    )


def token_chain(watch_token, key=None):
    chain_from_key, _ = split_watchlist_key(key)

    return (
        watch_token.get("chain_id")
        or watch_token.get("chain")
        or get_nested(watch_token, ["selected_pair", "chainId"])
        or get_nested(watch_token, ["token_profile", "chainId"])
        or chain_from_key
    )


def token_name(watch_token):
    return (
        watch_token.get("token_name")
        or get_nested(watch_token, ["selected_pair", "baseToken", "name"])
        or get_nested(watch_token, ["token_profile", "description"])
    )


def scanner_metrics(watch_token):
    metrics = watch_token.get("scanner_metrics")

    if isinstance(metrics, dict):
        return metrics

    return {}


def social_fields(watch_token):
    fields = watch_token.get("social")

    if isinstance(fields, dict):
        return fields

    return watch_token


def alert_chain_id(alert, watch_key=None, watch_token=None):
    return (
        alert.get("chain_id")
        or (token_chain(watch_token, watch_key) if watch_token else None)
        or split_watchlist_key(alert.get("watchlist_key"))[0]
        or "unknown"
    )


def alert_watchlist_key(alert, watch_key=None, watch_token=None):
    if alert.get("watchlist_key"):
        return alert.get("watchlist_key")

    if watch_token:
        return entry_watchlist_key(watch_key, watch_token)

    token_address = normalize_address(alert.get("token_address"))
    chain_id = alert_chain_id(alert)

    if token_address and chain_id and chain_id != "unknown":
        return f"{chain_id}:{token_address}"

    return token_address


def posts_file_for_alert(alert, watch_key=None, watch_token=None):
    token_address = normalize_address(alert.get("token_address")) or ""
    chain_id = alert_chain_id(alert, watch_key, watch_token)
    timestamp = alert.get("timestamp") or alert.get("social_monitoring_started_at")
    parsed = parse_iso(timestamp)

    if parsed:
        date_part = parsed.strftime("%Y-%m-%d")
    else:
        date_part = "YYYY-MM-DD"

    day_dir = SOCIAL_POSTS_DIR / date_part
    if chain_id and chain_id != "unknown":
        prefixed = day_dir / f"{chain_id}_{token_address}.json"
        if prefixed.exists():
            return prefixed
        legacy = day_dir / f"{token_address}.json"
        return prefixed if not legacy.exists() else legacy

    return day_dir / f"{token_address}.json"


def source_from_args(args):
    if args.file:
        return resolve_path(args.file)

    if args.date:
        return DATA_DIR / f"social_alerts_{args.date}.jsonl"

    return DEFAULT_ALERTS_FILE


def apply_filters(alerts, args, watchlist):
    filtered = alerts

    if args.token:
        wanted = normalize_address(args.token)
        filtered = [
            alert for alert in filtered
            if normalize_address(alert.get("token_address")) == wanted
        ]

    if args.chain:
        wanted_chain = args.chain
        chain_filtered = []
        for alert in filtered:
            _, watch_token = find_watchlist_entry(
                watchlist,
                alert.get("token_address"),
                alert.get("chain_id") or wanted_chain,
            )
            if alert_chain_id(alert, None, watch_token) == wanted_chain:
                chain_filtered.append(alert)
        filtered = chain_filtered

    if args.min_rank is not None:
        rank_filtered = []

        for alert in filtered:
            try:
                rank = float(alert.get("alert_rank") or 0)
            except (TypeError, ValueError):
                rank = 0

            if rank >= args.min_rank:
                rank_filtered.append(alert)

        filtered = rank_filtered

    if args.active_only:
        active_filtered = []
        for alert in filtered:
            _, entry = find_watchlist_entry(
                watchlist,
                alert.get("token_address"),
                alert.get("chain_id"),
            )
            if (entry or {}).get("status") == "ativo":
                active_filtered.append(alert)
        filtered = active_filtered

    filtered = sorted(filtered, key=sort_key)

    if args.latest:
        return filtered[-1:]

    if args.limit is not None and args.limit >= 0:
        return filtered[-args.limit:]

    return filtered


def print_header(source, total_loaded, total_showing, warnings):
    print("=== KRPTO-V | Social Alerts ===")
    print(f"Fonte: {relative_path(source)}")
    print(f"Alertas carregados: {total_loaded}")
    print(f"Mostrando: {total_showing}")

    for warning in warnings:
        print(f"Aviso: {warning}")

    print()


def print_reasons(reasons):
    print("Motivos de origem/reputacao:")

    if not reasons:
        print("- nenhum")
        return

    for reason in reasons:
        print(f"- {format_alert_reason(reason)}")


def print_watchlist_details(alert, watch_token):
    metrics = scanner_metrics(watch_token)
    social = social_fields(watch_token)
    posts_file = posts_file_for_alert(alert, None, watch_token)

    print()
    print("Watchlist:")
    print(f"Status atual: {watch_token.get('status', 'indisponivel')}")
    print(f"Social status: {watch_token.get('social_status', 'indisponivel')}")
    print(f"Monitor status: {watch_token.get('monitor_status', 'indisponivel')}")
    print(f"Status reason: {watch_token.get('status_reason', 'indisponivel')}")
    print(f"Chain: {token_chain(watch_token) or 'unknown'}")
    print(f"Watchlist key: {entry_watchlist_key(None, watch_token) or 'indisponivel'}")
    print(f"Token created at: {watch_token.get('created_at_utc') or watch_token.get('token_created_at', 'indisponivel')}")
    print(f"Source: {watch_token.get('source') or watch_token.get('token_created_at_source', 'indisponivel')}")
    print(f"Pool: {watch_token.get('pool_address', 'indisponivel')}")
    print(f"Quote token: {watch_token.get('quote_token', 'indisponivel')}")
    print(f"Token: {token_name(watch_token) or 'indisponivel'} / {token_symbol(watch_token) or 'indisponivel'}")
    print(f"Liquidity: {format_money(metrics.get('liquidity_usd'))}")
    print(f"Volume h1: {format_money(metrics.get('volume_h1'))}")
    print(f"MCap: {format_money(metrics.get('mcap'))}")
    print(f"FDV: {format_money(metrics.get('fdv'))}")
    print(f"Price change h1: {format_number(metrics.get('price_change_h1'), 2)}%")
    print(f"Social last checked at: {social.get('social_last_checked_at', 'indisponivel')}")
    print(f"Post metric telemetry legado: {social.get('best_social_score', 'indisponivel')}")
    print(f"Best origin alert rank: {social.get('best_alert_rank', 'indisponivel')}")
    print(f"Tracked posts: {social.get('social_tracked_posts_count', 'indisponivel')}")
    print(f"Posts file: {relative_path(posts_file)}")


def print_alert(index, alert, watch_token, show_watchlist):
    reasons = alert.get("alert_reasons") or []
    watch_key = None
    if watch_token:
        watch_key = alert.get("watchlist_key") or entry_watchlist_key(None, watch_token)
    chain_id = alert_chain_id(alert, watch_key, watch_token)
    wl_key = alert_watchlist_key(alert, watch_key, watch_token)

    print("-" * 80)
    print(f"[{index}] {alert.get('timestamp', 'indisponivel')}")
    print(f"Chain: {chain_id}")
    print(f"Watchlist key: {wl_key or 'indisponivel'}")
    print(f"Token: {alert.get('token_address', 'indisponivel')}")
    if watch_token:
        print(f"Token created at: {watch_token.get('created_at_utc') or watch_token.get('token_created_at', 'indisponivel')}")
        print(f"Source: {watch_token.get('source') or watch_token.get('token_created_at_source', 'indisponivel')}")
    print(f"Status: {alert.get('status_before', 'indisponivel')} -> {alert.get('status_after', 'indisponivel')}")
    print(f"Rank origem/reputacao: {alert.get('alert_rank', 'indisponivel')}")
    print_reasons(reasons)
    print()
    print(f"Best author followers: {format_number(alert.get('best_author_followers'))}")
    print(f"Origin type: {alert.get('origin_type', 'indisponivel')}")
    print(f"Author: @{alert.get('author_username', 'indisponivel')}")
    print(f"Author followers: {format_number(alert.get('author_followers'))}")
    print(f"Author verified: {format_bool(alert.get('author_verified'))}")
    print(f"Author verified type: {alert.get('author_verified_type', 'indisponivel')}")
    print(f"Author affiliation: {format_affiliation('author', alert)}")
    print(f"Automated operator: {alert.get('automated_operator_username', 'nenhum')}")
    print(f"Operator followers: {format_number(alert.get('operator_followers'))}")
    print(f"Operator verified type: {alert.get('operator_verified_type', 'indisponivel')}")
    print(f"Operator affiliation: {format_affiliation('operator', alert)}")
    print()
    print_author_summary("Selected origin summary", alert.get("selected_origin_summary"))
    print_author_summary("Best followers author", alert.get("best_followers_author_summary"))
    print_author_summary("Best affiliation author", alert.get("best_affiliation_author_summary"))
    print()
    print_trigger_posts(alert.get("trigger_posts") or [])
    print(f"Alert posts snapshot: {alert.get('raw_alert_posts_file', 'indisponivel')}")
    print(
        "Janela social: "
        f"{alert.get('social_monitoring_started_at', 'indisponivel')} -> "
        f"{alert.get('social_monitoring_expires_at', 'indisponivel')}"
    )
    print(f"Telegram flag: {str(alert.get('telegram_alert_sent', 'indisponivel')).lower()}")

    if show_watchlist and watch_token:
        print_watchlist_details(alert, watch_token)

    print()


def print_compact_alert(alert, watch_token):
    symbol = token_symbol(watch_token) if watch_token else None
    chain = alert_chain_id(alert, None, watch_token)
    status = watch_token.get("status") if watch_token else alert.get("status_after")
    reasons = format_list([format_alert_reason(reason) for reason in (alert.get("alert_reasons") or [])])
    timestamp = alert.get("timestamp", "indisponivel")
    rank = alert.get("alert_rank", "indisponivel")
    token_address = short_address(alert.get("token_address"))
    origin = alert.get("origin_type", "indisponivel")

    print(
        f"{timestamp} | rank={rank} | {chain or 'chain?'} | {symbol or 'indisponivel'} | "
        f"{token_address} | {origin} | {reasons} | status={status or 'indisponivel'}"
    )


def print_investigation_commands(alerts):
    if not alerts:
        return

    print("Como investigar")
    print("---------------")

    for alert in alerts:
        posts_file = relative_path(posts_file_for_alert(alert))
        print(f"python src/tools/print_x_posts.py {posts_file} --tracked-only")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Imprime alertas locais do social inference do KRPTO-V.",
    )
    parser.add_argument("--file", help="Arquivo especifico .json ou .jsonl.")
    parser.add_argument("--date", help="Data do historico diario: YYYY-MM-DD.")
    parser.add_argument("--latest", action="store_true", help="Mostra apenas o alerta mais recente.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Mostra os ultimos N alertas.")
    parser.add_argument("--token", help="Filtra por token address.")
    parser.add_argument("--chain", help="Filtra por chain, como ethereum ou base.")
    parser.add_argument("--min-rank", type=float, help="Filtra alertas com alert_rank >= N.")
    parser.add_argument("--active-only", action="store_true", help="Mostra apenas tokens ainda ativos na watchlist.")
    parser.add_argument("--compact", action="store_true", help="Saida curta, uma linha por alerta.")
    parser.add_argument("--show-watchlist", action="store_true", help="Enriquece com dados atuais da watchlist.")
    return parser.parse_args()


def main():
    args = parse_args()
    source = source_from_args(args)
    alerts, warnings = load_alerts(source)
    watchlist = load_watchlist() if args.active_only or args.show_watchlist or args.compact or args.chain else {}
    visible_alerts = apply_filters(alerts, args, watchlist)

    print_header(source, len(alerts), len(visible_alerts), warnings)

    if not visible_alerts:
        print("Nenhum alerta encontrado para os filtros informados.")
        return

    for index, alert in enumerate(visible_alerts, start=1):
        token_address = normalize_address(alert.get("token_address"))
        _, watch_token = find_watchlist_entry(watchlist, token_address, alert.get("chain_id"))

        if args.compact:
            print_compact_alert(alert, watch_token)
        else:
            print_alert(index, alert, watch_token, args.show_watchlist)

    if not args.compact:
        print_investigation_commands(visible_alerts)


if __name__ == "__main__":
    main()
