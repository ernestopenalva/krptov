import argparse
import json
import os
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path


DEXSCREENER_LATEST_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chain_id}/{token_address}"
TOKEN_SCANNER_VERSION = "krptov-token-scanner-v2-compact-watchlist-2026-06-01"

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
WATCHLIST_LOCK_FILE = DATA_DIR / "watchlist.lock"
LATEST_SNAPSHOT_FILE = DATA_DIR / "token_scanner_latest.json"
LATEST_RAW_PROFILES_FILE = DATA_DIR / "token_scanner_latest_profiles_raw.json"

DEFAULT_CONFIG = {
    "chain_ids": ["ethereum", "base"],
    "watchlist_max_tokens": 10,
    "watchlist_infinite": True,
    "feed_disappearance_minutes": 60,
    "discard_retention_hours": 168,
    "cycle_interval_seconds": 60,
}

SCANNER_OWNED_FIELDS = {
    "watchlist_key",
    "chain",
    "chain_id",
    "token_address",
    "token_symbol",
    "token_name",
    "pool_address",
    "quote_token",
    "quote_token_address",
    "source",
    "source_type",
    "dex_id",
    "discovered_at_utc",
    "created_at_utc",
    "created_block",
    "created_tx",
    "last_seen_at_utc",
    "times_seen",
    "status",
    "status_reason",
    "discarded_reason",
    "discarded_at",
    "scanner_validation_status",
    "scanner_validation_reason",
}


def now():
    return datetime.utcnow().replace(microsecond=0)


def to_iso(value):
    return value.isoformat() + "Z"


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
    list_key = None

    for raw_line in config_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()

        if not line:
            continue

        stripped = line.strip()

        if line == "token_scanner:":
            in_token_scanner = True
            list_key = None
            continue

        if not raw_line.startswith((" ", "\t")):
            in_token_scanner = False
            list_key = None

        if not in_token_scanner:
            continue

        if stripped.startswith("- ") and list_key:
            config.setdefault(list_key, []).append(parse_config_value(stripped[2:]))
            continue

        if ":" not in line:
            continue

        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()

        if value == "":
            config[key] = []
            list_key = key
            continue

        config[key] = parse_config_value(value)
        list_key = None

    return config


def load_config(config_file=CONFIG_FILE):
    config = DEFAULT_CONFIG.copy()

    if not config_file.exists():
        return config

    loaded = load_simple_yaml_token_scanner(config_file)
    config.update({key: value for key, value in loaded.items() if value != ""})

    if "chain_id" in loaded and "chain_ids" not in loaded:
        config["chain_ids"] = [loaded["chain_id"]]
    if isinstance(config.get("chain_ids"), str):
        config["chain_ids"] = [config["chain_ids"]]

    return config


def ensure_directories():
    DATA_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)


def load_watchlist(path=None):
    path = path or WATCHLIST_FILE

    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("data/watchlist.json precisa ser um dict indexado por token.")

    return migrate_watchlist_keys(data)


def save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")

    try:
        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def append_jsonl(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


@contextmanager
def watchlist_lock(timeout_seconds=120, poll_seconds=0.2):
    DATA_DIR.mkdir(exist_ok=True)
    started_at = time.time()
    lock_handle = None

    while True:
        try:
            lock_handle = os.open(
                WATCHLIST_LOCK_FILE,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.write(lock_handle, str(os.getpid()).encode("utf-8"))
            break
        except FileExistsError:
            if time.time() - started_at >= timeout_seconds:
                raise TimeoutError(f"Timeout aguardando lock da watchlist: {WATCHLIST_LOCK_FILE}")
            time.sleep(poll_seconds)

    try:
        yield
    finally:
        if lock_handle is not None:
            os.close(lock_handle)
        try:
            WATCHLIST_LOCK_FILE.unlink()
        except FileNotFoundError:
            pass


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


def make_watchlist_key(chain_id, token_address):
    normalized_address = normalize_ethereum_address(token_address)
    if not chain_id or not normalized_address:
        return None

    return f"{chain_id}:{normalized_address}"


def migrate_watchlist_keys(watchlist):
    migrated = {}

    for key, entry in watchlist.items():
        if not isinstance(entry, dict):
            migrated[key] = entry
            continue

        token_key = key
        if ":" not in key:
            token_key = make_watchlist_key(entry.get("chain_id"), entry.get("token_address") or key) or key

        migrated[token_key] = normalize_compact_watchlist_entry(entry, token_key)

    return migrated


def timestamp_ms_to_iso(value):
    if value in [None, ""]:
        return None

    try:
        return datetime.utcfromtimestamp(int(value) / 1000).replace(microsecond=0).isoformat() + "Z"
    except (TypeError, ValueError, OSError):
        return None


def get_token_created_at(selected_pair):
    if not isinstance(selected_pair, dict):
        return None, None

    pair_created_at_ms = selected_pair.get("pairCreatedAt")
    created_at = timestamp_ms_to_iso(pair_created_at_ms)
    if not created_at:
        return None, None

    return created_at, pair_created_at_ms


def get_pair_token_data(selected_pair, token_address):
    if not isinstance(selected_pair, dict):
        return {}, {}

    normalized_address = normalize_ethereum_address(token_address)
    base_token = selected_pair.get("baseToken") or {}
    quote_token = selected_pair.get("quoteToken") or {}
    base_address = normalize_ethereum_address(base_token.get("address"))
    quote_address = normalize_ethereum_address(quote_token.get("address"))

    if normalized_address and quote_address == normalized_address:
        return quote_token, base_token

    return base_token, quote_token


def get_compact_pair_fields(selected_pair, token_address):
    token_data, quote_data = get_pair_token_data(selected_pair, token_address)

    return {
        "token_symbol": token_data.get("symbol"),
        "token_name": token_data.get("name"),
        "pool_address": selected_pair.get("pairAddress") if isinstance(selected_pair, dict) else None,
        "quote_token": quote_data.get("symbol"),
        "quote_token_address": normalize_ethereum_address(quote_data.get("address")),
        "dex_id": selected_pair.get("dexId") if isinstance(selected_pair, dict) else None,
    }


def legacy_created_at(entry):
    return (
        entry.get("created_at_utc")
        or entry.get("token_created_at")
        or timestamp_ms_to_iso(entry.get("pair_created_at_ms"))
    )


def normalize_compact_watchlist_entry(entry, watchlist_key=None):
    if not isinstance(entry, dict):
        return entry

    chain_id = entry.get("chain_id") or entry.get("chain")
    token_address = normalize_ethereum_address(entry.get("token_address"))
    selected_pair = entry.get("selected_pair")

    if not token_address and watchlist_key and ":" in watchlist_key:
        _, key_address = watchlist_key.split(":", 1)
        token_address = normalize_ethereum_address(key_address)

    pair_fields = get_compact_pair_fields(selected_pair, token_address)
    compact_key = make_watchlist_key(chain_id, token_address) or watchlist_key
    discovered_at = entry.get("discovered_at_utc") or entry.get("first_seen_at")
    last_seen_at = (
        entry.get("last_seen_at_utc")
        or entry.get("last_seen_on_dexscreener_at")
        or entry.get("last_seen_at")
        or discovered_at
    )

    entry["watchlist_key"] = compact_key
    entry["chain"] = chain_id
    entry["chain_id"] = chain_id
    entry["token_address"] = token_address
    entry["token_symbol"] = entry.get("token_symbol") or pair_fields["token_symbol"]
    entry["token_name"] = entry.get("token_name") or pair_fields["token_name"]
    entry["pool_address"] = entry.get("pool_address") or pair_fields["pool_address"]
    entry["quote_token"] = entry.get("quote_token") or pair_fields["quote_token"]
    entry["quote_token_address"] = entry.get("quote_token_address") or pair_fields["quote_token_address"]
    entry["source"] = entry.get("source") or "dexscreener_profiles"
    entry["source_type"] = entry.get("source_type") or "token_profile"
    entry["discovered_at_utc"] = discovered_at
    entry["created_at_utc"] = legacy_created_at(entry)
    entry["created_block"] = entry.get("created_block")
    entry["created_tx"] = entry.get("created_tx")
    entry["last_seen_at_utc"] = last_seen_at
    entry["times_seen"] = int(entry.get("times_seen", 0))
    entry["status"] = entry.get("status") or STATUS_NOVO
    entry["social_status"] = entry.get("social_status") or "pendente"
    entry["monitor_status"] = entry.get("monitor_status") or "pendente"
    entry["status_reason"] = entry.get("status_reason")
    entry["discarded_reason"] = entry.get("discarded_reason")
    entry["telegram_alert_sent"] = bool(entry.get("telegram_alert_sent", False))
    entry["scanner_validation_status"] = entry.get("scanner_validation_status") or (
        "approved" if selected_pair else "pending"
    )
    entry["scanner_validation_reason"] = entry.get("scanner_validation_reason") or (
        "dexscreener_profile_with_selected_pair"
        if selected_pair
        else "dexscreener_profile_without_pair"
    )
    if pair_fields["dex_id"]:
        entry["dex_id"] = entry.get("dex_id") or pair_fields["dex_id"]

    for legacy_key in [
        "token_profile",
        "selected_pair",
        "scanner_metrics",
        "token_created_at",
        "token_created_at_source",
        "pair_created_at_ms",
        "first_seen_at",
        "last_seen_at",
        "last_seen_on_dexscreener_at",
    ]:
        entry.pop(legacy_key, None)

    return entry


# ============================================================
# Etapa 1 - Descoberta
# ============================================================


def fetch_latest_token_profiles():
    import requests

    response = requests.get(DEXSCREENER_LATEST_PROFILES_URL, timeout=20)
    response.raise_for_status()
    return response.json()


def filter_latest_evm_profiles(tokens, chain_ids):
    seen = set()
    filtered = []
    allowed_chains = set(chain_ids)

    for token in tokens:
        chain_id = token.get("chainId")

        if chain_id not in allowed_chains:
            continue

        token_address = normalize_ethereum_address(token.get("tokenAddress"))
        if not token_address:
            print(f"[IGNORADO] Endereco EVM invalido: {str(token.get('tokenAddress'))[:80]}")
            continue

        token_key = f"{chain_id}:{token_address}"
        if token_key in seen:
            continue

        token["tokenAddress"] = token_address
        seen.add(token_key)
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


def build_new_watchlist_entry(token_profile, selected_pair, now_text):
    token_address = token_profile.get("tokenAddress")
    chain_id = token_profile.get("chainId")
    token_created_at, _ = get_token_created_at(selected_pair)
    pair_fields = get_compact_pair_fields(selected_pair, token_address)

    entry = {
        "token_address": normalize_ethereum_address(token_address),
        "chain": chain_id,
        "chain_id": chain_id,
        "watchlist_key": make_watchlist_key(chain_id, token_address),
        "token_symbol": pair_fields["token_symbol"],
        "token_name": pair_fields["token_name"],
        "pool_address": pair_fields["pool_address"],
        "quote_token": pair_fields["quote_token"],
        "quote_token_address": pair_fields["quote_token_address"],
        "source": "dexscreener_profiles",
        "source_type": "token_profile",
        "discovered_at_utc": now_text,
        "created_at_utc": token_created_at,
        "created_block": None,
        "created_tx": None,
        "last_seen_at_utc": now_text,
        "times_seen": 1,
        "status": STATUS_NOVO,
        "social_status": "pendente",
        "monitor_status": "pendente",
        "status_reason": None,
        "discarded_reason": None,
        "telegram_alert_sent": False,
        "scanner_validation_status": "approved" if selected_pair else "pending",
        "scanner_validation_reason": (
            "dexscreener_profile_with_selected_pair"
            if selected_pair
            else "dexscreener_profile_without_pair"
        ),
    }
    if pair_fields["dex_id"]:
        entry["dex_id"] = pair_fields["dex_id"]
    return entry


# ============================================================
# Etapa 3 - Watchlist e lifecycle
# ============================================================


def update_watchlist_entry(entry, token_profile, selected_pair, now_text):
    token_address = token_profile.get("tokenAddress")
    chain_id = token_profile.get("chainId")
    token_created_at, _ = get_token_created_at(selected_pair)
    pair_fields = get_compact_pair_fields(selected_pair, token_address)

    entry["token_address"] = normalize_ethereum_address(token_address)
    entry["chain"] = chain_id
    entry["chain_id"] = chain_id
    entry["watchlist_key"] = make_watchlist_key(chain_id, token_address)
    entry["token_symbol"] = pair_fields["token_symbol"] or entry.get("token_symbol")
    entry["token_name"] = pair_fields["token_name"] or entry.get("token_name")
    entry["pool_address"] = entry.get("pool_address") or pair_fields["pool_address"]
    entry["quote_token"] = entry.get("quote_token") or pair_fields["quote_token"]
    entry["quote_token_address"] = entry.get("quote_token_address") or pair_fields["quote_token_address"]
    entry["source"] = entry.get("source") or "dexscreener_profiles"
    entry["source_type"] = entry.get("source_type") or "token_profile"
    entry["discovered_at_utc"] = entry.get("discovered_at_utc") or now_text
    if token_created_at:
        entry["created_at_utc"] = entry.get("created_at_utc") or token_created_at
    entry["created_block"] = entry.get("created_block")
    entry["created_tx"] = entry.get("created_tx")
    entry["last_seen_at_utc"] = now_text
    entry["times_seen"] = int(entry.get("times_seen", 0)) + 1
    entry["social_status"] = entry.get("social_status") or "pendente"
    entry["monitor_status"] = entry.get("monitor_status") or "pendente"
    entry["discarded_reason"] = entry.get("discarded_reason")
    entry["telegram_alert_sent"] = bool(entry.get("telegram_alert_sent", False))
    entry["scanner_validation_status"] = "approved" if selected_pair else "pending"
    entry["scanner_validation_reason"] = (
        "dexscreener_profile_with_selected_pair"
        if selected_pair
        else "dexscreener_profile_without_pair"
    )
    if pair_fields["dex_id"]:
        entry["dex_id"] = entry.get("dex_id") or pair_fields["dex_id"]

    normalize_compact_watchlist_entry(entry, entry["watchlist_key"])


def is_token_scanner_entry(entry):
    return (
        isinstance(entry, dict)
        and entry.get("source") == "dexscreener_profiles"
        and entry.get("source_type") == "token_profile"
    )


def discard_stale_new_tokens(watchlist, seen_addresses, now, feed_disappearance_minutes):
    discarded = []
    max_age = timedelta(minutes=feed_disappearance_minutes)

    for token_address, entry in watchlist.items():
        if not is_token_scanner_entry(entry):
            continue
        if entry.get("status") != STATUS_NOVO:
            continue
        if token_address in seen_addresses:
            continue

        last_seen = parse_iso(entry.get("last_seen_at_utc"))
        if not last_seen:
            continue

        if now - last_seen > max_age:
            entry["status"] = STATUS_DESCARTE
            entry["status_reason"] = STATUS_REASON_DESCARTE_FEED
            entry["discarded_reason"] = STATUS_REASON_DESCARTE_FEED
            entry["discarded_at"] = to_iso(now)
            discarded.append(token_address)

    return discarded


def remove_expired_discards(watchlist, now, discard_retention_hours):
    removed = []
    retention = timedelta(hours=discard_retention_hours)

    for token_address, entry in list(watchlist.items()):
        if not is_token_scanner_entry(entry):
            continue
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
        (token_address, parse_iso(entry.get("last_seen_at_utc")) or datetime.min)
        for token_address, entry in watchlist.items()
        if is_token_scanner_entry(entry) and entry.get("status") == STATUS_NOVO
    ]
    removable.sort(key=lambda item: item[1])

    removed = []
    while len(watchlist) > max_tokens and removable:
        token_address, _ = removable.pop(0)
        del watchlist[token_address]
        removed.append(token_address)

    return removed


def merge_watchlist_for_save(current_watchlist, scanner_watchlist, removed_keys):
    merged = current_watchlist.copy()

    for watchlist_key in removed_keys:
        current_entry = merged.get(watchlist_key)
        if is_token_scanner_entry(current_entry):
            merged.pop(watchlist_key, None)

    for watchlist_key, scanner_entry in scanner_watchlist.items():
        current_entry = merged.get(watchlist_key)

        if not isinstance(current_entry, dict):
            merged[watchlist_key] = scanner_entry
            continue
        if not is_token_scanner_entry(scanner_entry):
            continue

        current_status = current_entry.get("status")
        current_status_reason = current_entry.get("status_reason")
        current_discarded_reason = current_entry.get("discarded_reason")
        normalized_current = normalize_compact_watchlist_entry(current_entry, watchlist_key)
        normalized_current.update(
            {
                key: value
                for key, value in scanner_entry.items()
                if key in SCANNER_OWNED_FIELDS
            }
        )

        if current_status_reason == STATUS_REASON_SOCIAL_TIMEOUT:
            normalized_current["status"] = STATUS_DESCARTE
            normalized_current["status_reason"] = STATUS_REASON_SOCIAL_TIMEOUT
            normalized_current["discarded_reason"] = STATUS_REASON_SOCIAL_TIMEOUT
        elif current_status == STATUS_ATIVO:
            normalized_current["status"] = STATUS_ATIVO
            normalized_current["status_reason"] = current_status_reason
            normalized_current["discarded_reason"] = current_discarded_reason

        merged[watchlist_key] = normalized_current

    return merged


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
        f"Base encontrados: {counters['base_found']}",
        f"Tokens alvo encontrados: {counters['target_chains_found']}",
        "Tokens alvo por chain: "
        + ", ".join(
            f"{chain}={count}"
            for chain, count in counters["target_chains_breakdown"].items()
        ),
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

    chain_ids = config["chain_ids"]
    chains_found = dict(Counter(token.get("chainId", "unknown") for token in tokens))
    target_tokens = filter_latest_evm_profiles(tokens, chain_ids)
    target_chains_found = dict(Counter(token.get("chainId", "unknown") for token in target_tokens))

    counters = {
        "tokens_returned": len(tokens),
        "ethereum_found": target_chains_found.get("ethereum", 0),
        "base_found": target_chains_found.get("base", 0),
        "target_chains_found": len(target_tokens),
        "target_chains_breakdown": target_chains_found,
        "chains_found": chains_found,
        "new_added": 0,
        "updated": 0,
        "ignored_discarded": 0,
        "ignored_external_status": 0,
        "enrichment_errors": 0,
        "trimmed_by_size": 0,
    }
    seen_keys = set()

    for token in target_tokens:
        chain_id = token.get("chainId")
        token_address = normalize_ethereum_address(token.get("tokenAddress"))
        if not token_address:
            continue

        token_key = make_watchlist_key(chain_id, token_address)
        if not token_key:
            continue

        seen_keys.add(token_key)
        existing = watchlist.get(token_key)

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
            watchlist[token_key] = build_new_watchlist_entry(
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
        seen_addresses=seen_keys,
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

    with watchlist_lock():
        current_watchlist = load_watchlist()
        watchlist = merge_watchlist_for_save(
            current_watchlist=current_watchlist,
            scanner_watchlist=watchlist,
            removed_keys=removed,
        )
        save_json(WATCHLIST_FILE, watchlist)

    snapshot = build_snapshot(
        now_text=now_text,
        config=config,
        counters=counters,
        watchlist=watchlist,
        seen_addresses=seen_keys,
        discarded=discarded,
        removed=removed,
    )

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
