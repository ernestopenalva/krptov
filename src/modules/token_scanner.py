import argparse
import json
import os
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


DEXSCREENER_LATEST_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chain_id}/{token_address}"
TOKEN_SCANNER_VERSION = "krptov-token-scanner-v1-ethereum-watchlist-2026-05-21"

STATUS_NOVO = "novo"
STATUS_ATIVO = "ativo"
STATUS_DESCARTE = "descarte"

STATUS_REASON_DESCARTE_FEED = "descarte_feed"
STATUS_REASON_SOCIAL_TIMEOUT = "social_timeout"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
LATEST_SNAPSHOT_FILE = DATA_DIR / "token_scanner_latest.json"
LATEST_RAW_PROFILES_FILE = DATA_DIR / "token_scanner_latest_profiles_raw.json"

DEFAULT_CONFIG = {
    "chain_id": "ethereum",
    "watchlist_max_tokens": 10,
    "watchlist_infinite": True,
    "feed_disappearance_minutes": 60,
    "discard_retention_hours": 168,
    "cycle_interval_seconds": 60,
}


def now():
    return datetime.now().replace(microsecond=0)


def to_iso(value):
    return value.isoformat()


def parse_iso(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        return None


def parse_config_value(value):
    value = value.strip()

    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False

    try:
        return int(value)
    except ValueError:
        return value


def load_simple_yaml_token_scanner(config_file):
    config = {}
    in_token_scanner = False

    for raw_line in config_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()

        if not line:
            continue

        if line == "token_scanner:":
            in_token_scanner = True
            continue

        if not raw_line.startswith((" ", "\t")):
            in_token_scanner = False

        if not in_token_scanner or ":" not in line:
            continue

        key, value = line.strip().split(":", 1)
        config[key.strip()] = parse_config_value(value)

    return config


def load_config(config_file=CONFIG_FILE):
    config = DEFAULT_CONFIG.copy()

    if not config_file.exists():
        return config

    loaded = load_simple_yaml_token_scanner(config_file)
    config.update({key: value for key, value in loaded.items() if value != ""})
    return config


def ensure_directories():
    DATA_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)


def load_watchlist(path=WATCHLIST_FILE):
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("data/watchlist.json precisa ser um dict indexado por token_address.")

    return data


def save_json(path, payload):
    temp_path = path.with_name(f".{path.name}.tmp")

    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    os.replace(temp_path, path)


def append_jsonl(path, payload):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def normalize_ethereum_address(address):
    if not isinstance(address, str):
        return None

    address = address.strip()

    if len(address) != 42:
        return None
    if not address.startswith("0x"):
        return None

    hex_part = address[2:]
    if not all(char in "0123456789abcdefABCDEF" for char in hex_part):
        return None

    return address.lower()


# ============================================================
# Etapa 1 - Descoberta
# ============================================================


def fetch_latest_token_profiles():
    import requests

    response = requests.get(DEXSCREENER_LATEST_PROFILES_URL, timeout=20)
    response.raise_for_status()
    return response.json()


def filter_latest_ethereum_profiles(tokens, chain_id):
    seen = set()
    filtered = []

    for token in tokens:
        if token.get("chainId") != chain_id:
            continue

        token_address = normalize_ethereum_address(token.get("tokenAddress"))
        if not token_address:
            print(f"[IGNORADO] Endereco Ethereum invalido: {str(token.get('tokenAddress'))[:80]}")
            continue

        if token_address in seen:
            continue

        token["tokenAddress"] = token_address
        seen.add(token_address)
        filtered.append(token)

    return filtered


# ============================================================
# Etapa 2 - Enriquecimento Dexscreener
# ============================================================


def fetch_token_pairs(chain_id, token_address):
    import requests

    url = DEXSCREENER_TOKEN_PAIRS_URL.format(
        chain_id=chain_id,
        token_address=token_address,
    )
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.json()


def get_nested_number(data, path, default=0):
    current = data

    for key in path:
        if not isinstance(current, dict):
            return default

        current = current.get(key)

        if current is None:
            return default

    try:
        return float(current)
    except (TypeError, ValueError):
        return default


def select_pair_with_highest_liquidity(pairs):
    if not pairs:
        return None

    return max(
        pairs,
        key=lambda pair: get_nested_number(pair, ["liquidity", "usd"]),
    )


def build_scanner_metrics(selected_pair):
    if not selected_pair:
        return {
            "liquidity_usd": 0,
            "volume_h1": 0,
            "buys_h1": 0,
            "sells_h1": 0,
            "total_txns_h1": 0,
            "mcap": None,
            "fdv": None,
            "price_change_m5": 0,
            "price_change_h1": 0,
        }

    buys_h1 = int(get_nested_number(selected_pair, ["txns", "h1", "buys"]))
    sells_h1 = int(get_nested_number(selected_pair, ["txns", "h1", "sells"]))

    return {
        "liquidity_usd": get_nested_number(selected_pair, ["liquidity", "usd"]),
        "volume_h1": get_nested_number(selected_pair, ["volume", "h1"]),
        "buys_h1": buys_h1,
        "sells_h1": sells_h1,
        "total_txns_h1": buys_h1 + sells_h1,
        "mcap": selected_pair.get("marketCap"),
        "fdv": selected_pair.get("fdv"),
        "price_change_m5": get_nested_number(selected_pair, ["priceChange", "m5"]),
        "price_change_h1": get_nested_number(selected_pair, ["priceChange", "h1"]),
    }


def build_new_watchlist_entry(token_profile, selected_pair, now_text):
    token_address = token_profile.get("tokenAddress")

    return {
        "token_address": normalize_ethereum_address(token_address),
        "chain_id": token_profile.get("chainId"),
        "status": STATUS_NOVO,
        "status_reason": None,
        "first_seen_at": now_text,
        "last_seen_at": now_text,
        "last_seen_on_dexscreener_at": now_text,
        "times_seen": 1,
        "token_profile": token_profile,
        "selected_pair": selected_pair,
        "scanner_metrics": build_scanner_metrics(selected_pair),
    }


# ============================================================
# Etapa 3 - Watchlist e lifecycle
# ============================================================


def update_watchlist_entry(entry, token_profile, selected_pair, now_text):
    entry["last_seen_at"] = now_text
    entry["last_seen_on_dexscreener_at"] = now_text
    entry["times_seen"] = int(entry.get("times_seen", 0)) + 1
    entry["token_profile"] = token_profile
    entry["selected_pair"] = selected_pair
    entry["scanner_metrics"] = build_scanner_metrics(selected_pair)


def discard_stale_new_tokens(watchlist, seen_addresses, now, feed_disappearance_minutes):
    discarded = []
    max_age = timedelta(minutes=feed_disappearance_minutes)

    for token_address, entry in watchlist.items():
        if entry.get("status") != STATUS_NOVO:
            continue
        if token_address in seen_addresses:
            continue

        last_seen = parse_iso(entry.get("last_seen_on_dexscreener_at"))
        if not last_seen:
            continue

        if now - last_seen > max_age:
            entry["status"] = STATUS_DESCARTE
            entry["status_reason"] = STATUS_REASON_DESCARTE_FEED
            entry["discarded_at"] = to_iso(now)
            discarded.append(token_address)

    return discarded


def remove_expired_discards(watchlist, now, discard_retention_hours):
    removed = []
    retention = timedelta(hours=discard_retention_hours)

    for token_address, entry in list(watchlist.items()):
        if entry.get("status") != STATUS_DESCARTE:
            continue

        discarded_at = parse_iso(entry.get("discarded_at"))
        if not discarded_at:
            continue

        if now - discarded_at > retention:
            del watchlist[token_address]
            removed.append(token_address)

    return removed


def trim_watchlist_if_needed(watchlist, config):
    if config["watchlist_infinite"]:
        return []

    max_tokens = int(config["watchlist_max_tokens"])
    if len(watchlist) <= max_tokens:
        return []

    removable = [
        (token_address, parse_iso(entry.get("last_seen_at")) or datetime.min)
        for token_address, entry in watchlist.items()
        if entry.get("status") == STATUS_NOVO
    ]
    removable.sort(key=lambda item: item[1])

    removed = []
    while len(watchlist) > max_tokens and removable:
        token_address, _ = removable.pop(0)
        del watchlist[token_address]
        removed.append(token_address)

    return removed


def preserve_social_fields(outgoing_watchlist, current_watchlist):
    social_keys = (
        "social_",
        "telegram_alert_",
        "last_alert_",
    )
    exact_keys = {
        "best_social_score",
        "best_alert_rank",
    }

    for token_address, outgoing_entry in outgoing_watchlist.items():
        current_entry = current_watchlist.get(token_address)
        if not isinstance(current_entry, dict):
            continue

        for key, value in current_entry.items():
            if key.startswith(social_keys) or key in exact_keys:
                outgoing_entry[key] = value

        if current_entry.get("status_reason") == STATUS_REASON_SOCIAL_TIMEOUT:
            outgoing_entry["status"] = STATUS_DESCARTE
            outgoing_entry["status_reason"] = STATUS_REASON_SOCIAL_TIMEOUT
        elif current_entry.get("status") == STATUS_ATIVO and outgoing_entry.get("status") == STATUS_NOVO:
            outgoing_entry["status"] = STATUS_ATIVO


def write_log_lines(lines, now):
    log_file = LOGS_DIR / f"token_scanner_{now.strftime('%Y-%m-%d')}.txt"

    with log_file.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
        f.write("\n")


def build_snapshot(now_text, config, counters, watchlist, seen_addresses, discarded, removed):
    return {
        "generated_at": now_text,
        "scanner_version": TOKEN_SCANNER_VERSION,
        "source": DEXSCREENER_LATEST_PROFILES_URL,
        "config": config,
        "counters": counters,
        "chains_found": counters["chains_found"],
        "seen_addresses": sorted(seen_addresses),
        "discarded_structurally": discarded,
        "removed_physically": removed,
        "watchlist_total": len(watchlist),
        "watchlist": watchlist,
    }


def print_summary(snapshot):
    counters = snapshot["counters"]
    lines = [
        "=== KRPTO-V | Token Scanner ===",
        f"Versao: {snapshot['scanner_version']}",
        f"Ciclo: {snapshot['generated_at']}",
        f"Tokens retornados: {counters['tokens_returned']}",
        f"Ethereum encontrados: {counters['ethereum_found']}",
        "Chains encontradas: "
        + ", ".join(
            f"{chain}={count}"
            for chain, count in counters["chains_found"].items()
        ),
        f"Novos adicionados: {counters['new_added']}",
        f"Atualizados: {counters['updated']}",
        f"Ignorados por descarte: {counters['ignored_discarded']}",
        f"Ignorados por status externo: {counters['ignored_external_status']}",
        f"Descartados estruturalmente: {len(snapshot['discarded_structurally'])}",
        f"Removidos fisicamente: {len(snapshot['removed_physically'])}",
        f"Total da WL: {snapshot['watchlist_total']}",
    ]

    for line in lines:
        print(line)

    return lines


def run_cycle(config_file=CONFIG_FILE):
    ensure_directories()

    config = load_config(Path(config_file))
    current_time = now()
    now_text = to_iso(current_time)
    date_stamp = current_time.strftime("%Y-%m-%d")

    watchlist = load_watchlist()
    tokens = fetch_latest_token_profiles()
    save_json(
        LATEST_RAW_PROFILES_FILE,
        {
            "generated_at": now_text,
            "source": DEXSCREENER_LATEST_PROFILES_URL,
            "total_tokens": len(tokens),
            "tokens": tokens,
        },
    )

    chain_id = config["chain_id"]
    chains_found = dict(Counter(token.get("chainId", "unknown") for token in tokens))
    ethereum_tokens = filter_latest_ethereum_profiles(tokens, chain_id)

    counters = {
        "tokens_returned": len(tokens),
        "ethereum_found": len(ethereum_tokens),
        "chains_found": chains_found,
        "new_added": 0,
        "updated": 0,
        "ignored_discarded": 0,
        "ignored_external_status": 0,
        "enrichment_errors": 0,
        "trimmed_by_size": 0,
    }
    seen_addresses = set()

    for token in ethereum_tokens:
        token_address = normalize_ethereum_address(token.get("tokenAddress"))
        if not token_address:
            continue

        seen_addresses.add(token_address)
        existing = watchlist.get(token_address)

        if existing and existing.get("status") == STATUS_DESCARTE:
            counters["ignored_discarded"] += 1
            continue

        if existing and existing.get("status") not in [STATUS_NOVO, STATUS_ATIVO]:
            counters["ignored_external_status"] += 1
            continue

        try:
            pairs = fetch_token_pairs(chain_id, token_address)
            selected_pair = select_pair_with_highest_liquidity(pairs)
        except Exception as exc:
            counters["enrichment_errors"] += 1
            selected_pair = None
            token["scanner_error"] = str(exc)

        if not existing:
            watchlist[token_address] = build_new_watchlist_entry(
                token_profile=token,
                selected_pair=selected_pair,
                now_text=now_text,
            )
            counters["new_added"] += 1
            continue

        update_watchlist_entry(
            entry=existing,
            token_profile=token,
            selected_pair=selected_pair,
            now_text=now_text,
        )
        counters["updated"] += 1

    discarded = discard_stale_new_tokens(
        watchlist=watchlist,
        seen_addresses=seen_addresses,
        now=current_time,
        feed_disappearance_minutes=int(config["feed_disappearance_minutes"]),
    )
    removed = remove_expired_discards(
        watchlist=watchlist,
        now=current_time,
        discard_retention_hours=int(config["discard_retention_hours"]),
    )
    trimmed = trim_watchlist_if_needed(watchlist, config)
    counters["trimmed_by_size"] = len(trimmed)
    removed.extend(trimmed)

    snapshot = build_snapshot(
        now_text=now_text,
        config=config,
        counters=counters,
        watchlist=watchlist,
        seen_addresses=seen_addresses,
        discarded=discarded,
        removed=removed,
    )

    preserve_social_fields(watchlist, load_watchlist())
    save_json(WATCHLIST_FILE, watchlist)
    save_json(LATEST_SNAPSHOT_FILE, snapshot)
    append_jsonl(DATA_DIR / f"token_scanner_{date_stamp}.jsonl", snapshot)

    summary_lines = print_summary(snapshot)
    write_log_lines(summary_lines, current_time)

    return snapshot


def run_token_scanner():
    return run_cycle()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Executa um ciclo stateless do token scanner do KRPTO-V."
    )
    parser.add_argument(
        "--config",
        default=CONFIG_FILE,
        type=Path,
        help="Caminho do config.yaml.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    run_cycle(config_file=args.config)


if __name__ == "__main__":
    run_token_scanner()
