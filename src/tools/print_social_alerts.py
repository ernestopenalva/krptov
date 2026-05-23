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
    if not token_address:
        return None

    return watchlist.get(token_address.lower()) or watchlist.get(token_address)


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


def format_list(values):
    if not values:
        return "nenhum"

    if isinstance(values, list):
        return ", ".join(str(value) for value in values)

    return str(values)


def short_address(value):
    if not value:
        return "indisponivel"

    value = str(value)

    if len(value) <= 14:
        return value

    return f"{value[:6]}...{value[-4:]}"


def token_symbol(watch_token):
    return get_nested(watch_token, ["selected_pair", "baseToken", "symbol"])


def token_name(watch_token):
    return (
        get_nested(watch_token, ["selected_pair", "baseToken", "name"])
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


def posts_file_for_alert(alert):
    token_address = str(alert.get("token_address", "")).lower()
    timestamp = alert.get("timestamp") or alert.get("social_monitoring_started_at")
    parsed = parse_iso(timestamp)

    if parsed:
        date_part = parsed.strftime("%Y-%m-%d")
    else:
        date_part = "YYYY-MM-DD"

    return SOCIAL_POSTS_DIR / date_part / f"{token_address}.json"


def source_from_args(args):
    if args.file:
        return resolve_path(args.file)

    if args.date:
        return DATA_DIR / f"social_alerts_{args.date}.jsonl"

    return DEFAULT_ALERTS_FILE


def apply_filters(alerts, args, watchlist):
    filtered = alerts

    if args.token:
        wanted = args.token.lower()
        filtered = [
            alert for alert in filtered
            if str(alert.get("token_address", "")).lower() == wanted
        ]

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
        filtered = [
            alert for alert in filtered
            if (
                get_token_from_watchlist(watchlist, str(alert.get("token_address", "")).lower())
                or {}
            ).get("status") == "ativo"
        ]

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
    print("Motivos:")

    if not reasons:
        print("- nenhum")
        return

    for reason in reasons:
        print(f"- {reason}")


def print_watchlist_details(alert, watch_token):
    metrics = scanner_metrics(watch_token)
    social = social_fields(watch_token)
    posts_file = posts_file_for_alert(alert)

    print()
    print("Watchlist:")
    print(f"Status atual: {watch_token.get('status', 'indisponivel')}")
    print(f"Status reason: {watch_token.get('status_reason', 'indisponivel')}")
    print(f"Token: {token_name(watch_token) or 'indisponivel'} / {token_symbol(watch_token) or 'indisponivel'}")
    print(f"Liquidity: {format_money(metrics.get('liquidity_usd'))}")
    print(f"Volume h1: {format_money(metrics.get('volume_h1'))}")
    print(f"MCap: {format_money(metrics.get('mcap'))}")
    print(f"FDV: {format_money(metrics.get('fdv'))}")
    print(f"Price change h1: {format_number(metrics.get('price_change_h1'), 2)}%")
    print(f"Social last checked at: {social.get('social_last_checked_at', 'indisponivel')}")
    print(f"Best social score: {social.get('best_social_score', 'indisponivel')}")
    print(f"Best alert rank: {social.get('best_alert_rank', 'indisponivel')}")
    print(f"Tracked posts: {social.get('social_tracked_posts_count', 'indisponivel')}")
    print(f"Posts file: {relative_path(posts_file)}")


def print_alert(index, alert, watch_token, show_watchlist):
    reasons = alert.get("alert_reasons") or []
    bio_patterns = alert.get("bio_patterns_found") or []

    print("-" * 80)
    print(f"[{index}] {alert.get('timestamp', 'indisponivel')}")
    print(f"Token: {alert.get('token_address', 'indisponivel')}")
    print(f"Status: {alert.get('status_before', 'indisponivel')} -> {alert.get('status_after', 'indisponivel')}")
    print(f"Rank: {alert.get('alert_rank', 'indisponivel')}")
    print_reasons(reasons)
    print()
    print(f"Best post score: {alert.get('best_post_score', 'indisponivel')}")
    print(f"Best author followers: {format_number(alert.get('best_author_followers'))}")
    print(f"Affiliation: {format_bool(alert.get('affiliation_found'))}")
    print(f"Bio patterns: {format_list(bio_patterns)}")
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
    status = watch_token.get("status") if watch_token else alert.get("status_after")
    reasons = format_list(alert.get("alert_reasons") or [])
    timestamp = alert.get("timestamp", "indisponivel")
    rank = alert.get("alert_rank", "indisponivel")
    token_address = short_address(alert.get("token_address"))

    print(
        f"{timestamp} | rank={rank} | {symbol or 'indisponivel'} | "
        f"{token_address} | {reasons} | status={status or 'indisponivel'}"
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
    parser.add_argument("--min-rank", type=float, help="Filtra alertas com alert_rank >= N.")
    parser.add_argument("--active-only", action="store_true", help="Mostra apenas tokens ainda ativos na watchlist.")
    parser.add_argument("--compact", action="store_true", help="Saida curta, uma linha por alerta.")
    parser.add_argument("--show-watchlist", action="store_true", help="Enriquece com dados atuais da watchlist.")
    return parser.parse_args()


def main():
    args = parse_args()
    source = source_from_args(args)
    alerts, warnings = load_alerts(source)
    watchlist = load_watchlist() if args.active_only or args.show_watchlist or args.compact else {}
    visible_alerts = apply_filters(alerts, args, watchlist)

    print_header(source, len(alerts), len(visible_alerts), warnings)

    if not visible_alerts:
        print("Nenhum alerta encontrado para os filtros informados.")
        return

    for index, alert in enumerate(visible_alerts, start=1):
        token_address = str(alert.get("token_address", "")).lower()
        watch_token = get_token_from_watchlist(watchlist, token_address)

        if args.compact:
            print_compact_alert(alert, watch_token)
        else:
            print_alert(index, alert, watch_token, args.show_watchlist)

    if not args.compact:
        print_investigation_commands(visible_alerts)


if __name__ == "__main__":
    main()
