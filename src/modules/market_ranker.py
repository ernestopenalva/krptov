import argparse
import json
import os
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.modules import telegram_notifier


MARKET_RANKER_VERSION = "krptov-market-ranker-v1-2026-06-03"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"
DATA_DIR = PROJECT_ROOT / "data"
MARKET_RANKER_DATA_DIR = DATA_DIR / "market_ranker"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
WATCHLIST_LOCK_FILE = DATA_DIR / "watchlist.lock"
STATE_FILE = MARKET_RANKER_DATA_DIR / "state.json"
OPS_ALERT_STATE_FILE = MARKET_RANKER_DATA_DIR / "ops_alert_state.json"

DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chain}/{token_address}"
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/tokens/v1/{chain}/{token_addresses}"
REQUEST_TIMEOUT_SECONDS = 20
OPS_ALERT_DEFAULT_COOLDOWN_SECONDS = 1800
DEXSCREENER_MAX_BATCH_TOKENS = 30

STATUS_NOVO = "novo"
STATUS_ATIVO = "ativo"
SOCIAL_ELIGIBILITY_PENDING = "pending"
SOCIAL_ELIGIBILITY_ELIGIBLE = "eligible"
SOCIAL_ELIGIBILITY_BLOCKED_OLD_MARKET = "blocked_old_market"
SOCIAL_ELIGIBILITY_REASON_PENDING_DEX = "pending_dexscreener"
SOCIAL_ELIGIBILITY_REASON_FRESH_MARKET = "fresh_market"
SOCIAL_ELIGIBILITY_REASON_OLD_MARKET = "old_market"
SOCIAL_ELIGIBILITY_MAX_MARKET_AGE_HOURS = 24
MINIMUM_TOKEN_AGE_SOURCE_OLDEST_PAIR = "oldest_pair"
MINIMUM_TOKEN_AGE_SOURCE_SELECTED_PAIR = "selected_pair"
MARKET_SANITY_OK = "ok"
MARKET_SANITY_MISLEADING_LIQUIDITY = "misleading_liquidity"
MARKET_SANITY_REASON_MISLEADING_LIQUIDITY = "high_dex_liquidity_low_quote_liquidity"
TRUSTED_QUOTE_SYMBOLS = {
    "ETH",
    "WETH",
    "USDC",
    "USDT",
    "USDBC",
    "DAI",
}
STABLE_QUOTE_SYMBOLS = {
    "USDC",
    "USDT",
    "USDBC",
    "DAI",
}
MISLEADING_LIQUIDITY_USD_THRESHOLD = 100_000
MISLEADING_QUOTE_LIQUIDITY_USD_THRESHOLD = 1_000
DEPRECATED_WATCHLIST_FIELDS = {
    "token_created_at_utc",
    "token_age_minutes",
    "token_age_status",
    "token_age_source",
    "token_age_updated_at",
}

DEFAULT_CONFIG = {
    "market_ranker": {
        "score_weights": {
            "quote_liquidity": 5,
            "volume_h24": 3,
            "txns_h24": 4,
            "minimum_token_age_inferred": 5,
        },
        "watchlist_retention": {
            "enabled": True,
            "max_entries": 500,
            "archive_removed": True,
            "archive_file": "data/watchlist_archive.jsonl",
            "unranked_retention_hours": 6,
            "pending_retention_hours": 12,
            "blocked_old_market_retention_hours": 6,
            "low_score_retention_hours": 24,
            "low_score_threshold": 35,
        },
        "social_eligibility": {
            "max_pool_age_minutes": 30,
        },
        "market_sanity": {
            "misleading_liquidity_usd_threshold": 100_000,
            "misleading_quote_liquidity_usd_threshold": 1_000,
            "misleading_score_multiplier": 0.25,
        },
    },
    "ops_alerts": {
        "enabled": True,
        "cooldown_seconds": OPS_ALERT_DEFAULT_COOLDOWN_SECONDS,
    },
}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_config_value(value):
    value = value.strip()

    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False

    try:
        return int(value)
    except ValueError:
        return value.strip('"').strip("'")


def merge_dict(base, updates):
    merged = base.copy()

    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dict(merged[key], value)
            continue
        merged[key] = value

    return merged


def load_simple_yaml_sections(config_file, section_names):
    sections = {section_name: {} for section_name in section_names}
    current_section = None
    stack = []

    if not Path(config_file).exists():
        return sections

    for raw_line in Path(config_file).read_text(encoding="utf-8").splitlines():
        line_without_comment = raw_line.split("#", 1)[0].rstrip()

        if not line_without_comment:
            continue

        stripped = line_without_comment.strip()
        indent = len(raw_line) - len(raw_line.lstrip(" "))

        if indent == 0 and stripped.endswith(":"):
            section_name = stripped[:-1]
            if section_name in sections:
                current_section = section_name
                stack = [(0, sections[section_name])]
                continue

            current_section = None
            stack = []
            continue

        if not current_section or ":" not in stripped:
            continue

        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        current = stack[-1][1]
        if value == "":
            current[key] = {}
            stack.append((indent, current[key]))
            continue

        current[key] = parse_config_value(value)

    return sections


def load_config(config_file=CONFIG_FILE):
    config = merge_dict({}, DEFAULT_CONFIG)
    loaded_sections = load_simple_yaml_sections(config_file, {"market_ranker", "ops_alerts"})
    config["market_ranker"] = merge_dict(
        config.get("market_ranker", {}),
        loaded_sections.get("market_ranker", {}),
    )
    config["ops_alerts"] = merge_dict(
        config.get("ops_alerts", {}),
        loaded_sections.get("ops_alerts", {}),
    )
    return config


def market_ranker_config(config):
    return (config or {}).get("market_ranker") or {}


def score_weights(config):
    configured = market_ranker_config(config).get("score_weights") or {}
    defaults = DEFAULT_CONFIG["market_ranker"]["score_weights"]
    weights = {}

    for key, default_value in defaults.items():
        try:
            value = float(configured.get(key, default_value))
        except (TypeError, ValueError):
            value = float(default_value)
        weights[key] = max(0.0, value)

    if sum(weights.values()) <= 0:
        return defaults.copy()

    return weights


def social_eligibility_config(config):
    configured = market_ranker_config(config).get("social_eligibility") or {}
    defaults = DEFAULT_CONFIG["market_ranker"]["social_eligibility"]
    return merge_dict(defaults, configured)


def market_sanity_config(config):
    configured = market_ranker_config(config).get("market_sanity") or {}
    defaults = DEFAULT_CONFIG["market_ranker"]["market_sanity"]
    return merge_dict(defaults, configured)


def normalize_evm_address(address):
    if not isinstance(address, str):
        return None

    address = address.strip()
    if len(address) != 42 or not address.startswith("0x"):
        return None
    if not all(character in "0123456789abcdefABCDEF" for character in address[2:]):
        return None
    return address.lower()


def split_watchlist_key(key):
    if not isinstance(key, str) or ":" not in key:
        return None, None

    chain, token_address = key.split(":", 1)
    return chain, normalize_evm_address(token_address)


def make_watchlist_key(chain, token_address):
    normalized_address = normalize_evm_address(token_address)
    if not chain or not normalized_address:
        return None
    return f"{chain}:{normalized_address}"


def ensure_directories():
    DATA_DIR.mkdir(exist_ok=True)
    MARKET_RANKER_DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path, default):
    if not path.exists():
        return default

    with path.open("r", encoding="utf-8") as file:
        loaded = json.load(file)

    return loaded if isinstance(loaded, type(default)) else default


def load_watchlist():
    watchlist = load_json(WATCHLIST_FILE, {})
    if not isinstance(watchlist, dict):
        raise ValueError("data/watchlist.json precisa ser um dict indexado por token.")
    return watchlist


def load_state():
    state = load_json(STATE_FILE, {})
    if not isinstance(state, dict):
        return {}
    return state


def atomic_save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")

    try:
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def append_jsonl(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded_line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

    with path.open("ab+") as file:
        file.seek(0, os.SEEK_END)
        end_position = file.tell()
        if end_position:
            file.seek(end_position - 1)
            if file.read(1) != b"\n":
                file.seek(0)
                existing = file.read()
                last_complete_line = existing.rfind(b"\n")
                file.seek(last_complete_line + 1 if last_complete_line >= 0 else 0)
                file.truncate()
        file.seek(0, os.SEEK_END)
        file.write(encoded_line)
        file.flush()
        os.fsync(file.fileno())


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


def snapshots_file_path(current_time):
    return MARKET_RANKER_DATA_DIR / f"snapshots_{current_time.strftime('%Y-%m-%d')}.jsonl"


def ops_alerts_enabled(config=None):
    config = config or {}
    ops_config = config.get("ops_alerts") or {}
    if "enabled" in ops_config:
        return bool(ops_config.get("enabled"))

    return os.getenv("KRPTO_OPS_ALERTS_ENABLED", "true").lower() not in {"0", "false", "no"}


def ops_alert_cooldown_seconds(config=None):
    config = config or {}
    ops_config = config.get("ops_alerts") or {}
    if ops_config.get("cooldown_seconds") is not None:
        try:
            return int(ops_config.get("cooldown_seconds"))
        except (TypeError, ValueError):
            return OPS_ALERT_DEFAULT_COOLDOWN_SECONDS

    try:
        return int(os.getenv("KRPTO_OPS_ALERT_COOLDOWN_SECONDS", OPS_ALERT_DEFAULT_COOLDOWN_SECONDS))
    except ValueError:
        return OPS_ALERT_DEFAULT_COOLDOWN_SECONDS


def classify_error(error_text):
    if not error_text:
        return None

    text = str(error_text)
    lower_text = text.lower()

    if "429" in text or "too many requests" in lower_text:
        return "dexscreener_rate_limit"
    if "timeout" in lower_text or "timed out" in lower_text:
        return "dexscreener_timeout"
    if " 5" in text or "500" in text or "502" in text or "503" in text or "504" in text:
        return "dexscreener_http_5xx"
    if " client error" in lower_text or " 4" in text:
        return "dexscreener_http_4xx"
    if "json" in lower_text:
        return "dexscreener_json_error"

    return "unknown_error"


def error_summary_from_results(results):
    counts = Counter()
    examples = {}

    for item in results:
        error_text = item.get("error")
        error_type = classify_error(error_text)
        if not error_type:
            continue
        counts[error_type] += 1
        examples.setdefault(error_type, item.get("watchlist_key") or item.get("token_address"))

    return counts, examples


def should_send_ops_alert(alert_key, current_time, config=None):
    state = load_json(OPS_ALERT_STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}

    cooldown_seconds = ops_alert_cooldown_seconds(config)
    last_sent = parse_iso(state.get(alert_key, {}).get("last_sent_at_utc"))
    if last_sent and (current_time - last_sent).total_seconds() < cooldown_seconds:
        return False

    state[alert_key] = {"last_sent_at_utc": to_iso(current_time)}
    atomic_save_json(OPS_ALERT_STATE_FILE, state)
    return True


def build_ops_alert_message(summary, error_counts, examples):
    lines = [
        "<b>KRPTO-V | Alerta operacional</b>",
        "<b>Modulo:</b> market_ranker",
        f"<b>Ciclo:</b> {telegram_notifier.escape_html(summary.get('timestamp'))}",
        f"<b>Tokens consultados:</b> {telegram_notifier.escape_html(summary.get('tokens_checked'))}",
        f"<b>Erros:</b> {telegram_notifier.escape_html(summary.get('errors'))}",
    ]

    if error_counts.get("dexscreener_rate_limit"):
        lines.append("<b>Atencao:</b> Dexscreener rate limit detectado.")

    lines.append("<b>Erros por tipo:</b>")
    for error_type, count in error_counts.most_common():
        example = examples.get(error_type) or "indisponivel"
        lines.append(
            "- "
            f"{telegram_notifier.escape_html(count)} | "
            f"{telegram_notifier.escape_html(error_type)} | "
            f"exemplo: <code>{telegram_notifier.escape_html(example)}</code>"
        )

    return "\n".join(lines)


def maybe_send_ops_alert(summary, results, current_time, config=None):
    if not ops_alerts_enabled(config):
        return None

    error_counts, examples = error_summary_from_results(results)
    if not error_counts:
        return None

    alert_key = (
        "market_ranker:dexscreener_rate_limit"
        if error_counts.get("dexscreener_rate_limit")
        else "market_ranker:external_errors"
    )
    if not should_send_ops_alert(alert_key, current_time, config):
        return {"success": False, "error": "cooldown"}

    message = build_ops_alert_message(summary, error_counts, examples)
    result = telegram_notifier.send_message(
        "system",
        message,
        config={"dry_run": False, "parse_mode": "HTML", "timeout_seconds": 20},
        env=telegram_notifier.load_telegram_env(PROJECT_ROOT / ".env"),
    )
    return result


def normalize_watchlist_entry(key, entry):
    if not isinstance(entry, dict):
        return None

    key_chain, key_address = split_watchlist_key(key)
    chain = entry.get("chain_id") or entry.get("chain") or key_chain
    token_address = normalize_evm_address(entry.get("token_address")) or key_address
    watchlist_key = entry.get("watchlist_key") or make_watchlist_key(chain, token_address) or key

    if not chain or not token_address:
        return None

    return {
        "watchlist_key": watchlist_key,
        "chain": chain,
        "token_address": token_address,
        "entry": entry,
    }


def should_rank(entry):
    return entry.get("status") in {STATUS_NOVO, STATUS_ATIVO}


def select_rankable_tokens(watchlist):
    selected = []

    for key, entry in watchlist.items():
        normalized = normalize_watchlist_entry(key, entry)
        if not normalized:
            continue
        if not should_rank(normalized["entry"]):
            continue
        selected.append(normalized)

    return selected


def fetch_token_pairs(chain, token_address, session=requests):
    url = DEXSCREENER_TOKEN_PAIRS_URL.format(chain=chain, token_address=token_address)
    response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else []


def fetch_token_pairs_batch(chain, token_addresses, session=requests):
    normalized_addresses = [
        normalize_evm_address(token_address)
        for token_address in token_addresses
    ]
    normalized_addresses = [address for address in normalized_addresses if address]
    if not normalized_addresses:
        return []

    url = DEXSCREENER_TOKENS_URL.format(
        chain=chain,
        token_addresses=",".join(normalized_addresses),
    )
    response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else []


def batched(items, batch_size):
    for index in range(0, len(items), batch_size):
        yield items[index:index + batch_size]


def pair_token_addresses(pair):
    addresses = set()
    if not isinstance(pair, dict):
        return addresses

    for key in ("baseToken", "quoteToken"):
        token = pair.get(key)
        if not isinstance(token, dict):
            continue
        address = normalize_evm_address(token.get("address"))
        if address:
            addresses.add(address)

    return addresses


def map_pairs_to_tokens(tokens, pairs):
    wanted = {token["token_address"] for token in tokens}
    mapped = {token["watchlist_key"]: [] for token in tokens}

    for pair in pairs:
        for address in pair_token_addresses(pair):
            if address not in wanted:
                continue
            for token in tokens:
                if token["token_address"] == address:
                    mapped[token["watchlist_key"]].append(pair)

    return mapped


def fetch_pairs_for_rankable_tokens(tokens, session=requests):
    pairs_by_key = {token["watchlist_key"]: [] for token in tokens}
    errors_by_key = {}
    batch_calls = 0
    tokens_by_chain = {}

    for token in tokens:
        tokens_by_chain.setdefault(token["chain"], []).append(token)

    for chain, chain_tokens in tokens_by_chain.items():
        for batch in batched(chain_tokens, DEXSCREENER_MAX_BATCH_TOKENS):
            batch_calls += 1
            try:
                pairs = fetch_token_pairs_batch(
                    chain,
                    [token["token_address"] for token in batch],
                    session=session,
                )
            except Exception as error:
                error_text = str(error)
                for token in batch:
                    errors_by_key[token["watchlist_key"]] = error_text
                continue

            batch_pairs_by_key = map_pairs_to_tokens(batch, pairs)
            for watchlist_key, token_pairs in batch_pairs_by_key.items():
                pairs_by_key[watchlist_key].extend(token_pairs)

    return pairs_by_key, errors_by_key, batch_calls


def nested_number(data, path, default=0):
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


def h24_txns(pair):
    buys = nested_number(pair, ["txns", "h24", "buys"])
    sells = nested_number(pair, ["txns", "h24", "sells"])
    return buys + sells


def liquidity_usd(pair):
    return nested_number(pair, ["liquidity", "usd"])


def token_side_in_pair(pair, token_address):
    normalized_token = normalize_evm_address(token_address)
    if not normalized_token or not isinstance(pair, dict):
        return None

    for side, key in (("base", "baseToken"), ("quote", "quoteToken")):
        token = pair.get(key)
        if not isinstance(token, dict):
            continue
        if normalize_evm_address(token.get("address")) == normalized_token:
            return side

    return None


def quote_symbol_for_side(pair, side):
    key = "baseToken" if side == "base" else "quoteToken"
    token = pair.get(key) if isinstance(pair, dict) else None
    if not isinstance(token, dict):
        return None
    symbol = token.get("symbol")
    if not symbol:
        return None
    return str(symbol).upper()


def trusted_quote_side(pair):
    for side in ("quote", "base"):
        symbol = quote_symbol_for_side(pair, side)
        if symbol and symbol in TRUSTED_QUOTE_SYMBOLS:
            return side
    return None


def quote_token_usd_price(pair, side):
    symbol = quote_symbol_for_side(pair, side)
    if not symbol:
        return None
    if symbol in STABLE_QUOTE_SYMBOLS:
        return 1.0

    price_usd = nested_number(pair, ["priceUsd"], None)
    if side == "base":
        return price_usd

    price_native = nested_number(pair, ["priceNative"], None)
    if price_usd is None or price_native is None or price_native <= 0:
        return None
    return price_usd / price_native


def quote_liquidity_metrics(pair, token_address, config=None):
    config = config or DEFAULT_CONFIG["market_ranker"]["market_sanity"]
    base_amount = nested_number(pair, ["liquidity", "base"], None)
    quote_amount = nested_number(pair, ["liquidity", "quote"], None)
    side = trusted_quote_side(pair)
    trusted_amount = None
    quote_liquidity_usd = None
    quote_symbol = quote_symbol_for_side(pair, side) if side else None

    if side == "base":
        trusted_amount = base_amount
    elif side == "quote":
        trusted_amount = quote_amount

    trusted_price = quote_token_usd_price(pair, side) if side else None
    if trusted_amount is not None and trusted_price is not None:
        quote_liquidity_usd = trusted_amount * trusted_price

    dex_liquidity = liquidity_usd(pair)
    misleading = (
        dex_liquidity > float(config.get("misleading_liquidity_usd_threshold") or MISLEADING_LIQUIDITY_USD_THRESHOLD)
        and quote_liquidity_usd is not None
        and quote_liquidity_usd < float(
            config.get("misleading_quote_liquidity_usd_threshold")
            or MISLEADING_QUOTE_LIQUIDITY_USD_THRESHOLD
        )
    )
    status = MARKET_SANITY_MISLEADING_LIQUIDITY if misleading else MARKET_SANITY_OK
    reason = MARKET_SANITY_REASON_MISLEADING_LIQUIDITY if misleading else None

    return {
        "liquidity_usd": dex_liquidity,
        "base_liquidity_amount": base_amount,
        "quote_liquidity_amount": quote_amount,
        "quote_liquidity_side": side,
        "quote_liquidity_symbol": quote_symbol,
        "quote_liquidity_usd": round(quote_liquidity_usd, 2) if quote_liquidity_usd is not None else None,
        "token_pair_side": token_side_in_pair(pair, token_address),
        "market_sanity_status": status,
        "market_sanity_reason": reason,
        "misleading_liquidity": misleading,
    }


def aggregate_quote_liquidity_metrics(pairs, selected_pair, token_address, config=None):
    config = config or DEFAULT_CONFIG["market_ranker"]["market_sanity"]
    selected_metrics = quote_liquidity_metrics(selected_pair, token_address, config=config)
    total_quote_liquidity_usd = 0
    has_quote_liquidity = False

    for pair in pairs or []:
        metrics = quote_liquidity_metrics(pair, token_address, config=config)
        quote_liquidity_usd = metrics.get("quote_liquidity_usd")
        if quote_liquidity_usd is None:
            continue
        total_quote_liquidity_usd += quote_liquidity_usd
        has_quote_liquidity = True

    aggregate_quote_liquidity_usd = round(total_quote_liquidity_usd, 2) if has_quote_liquidity else None
    dex_liquidity = selected_metrics["liquidity_usd"]
    misleading = (
        dex_liquidity > float(config.get("misleading_liquidity_usd_threshold") or MISLEADING_LIQUIDITY_USD_THRESHOLD)
        and aggregate_quote_liquidity_usd is not None
        and aggregate_quote_liquidity_usd < float(
            config.get("misleading_quote_liquidity_usd_threshold")
            or MISLEADING_QUOTE_LIQUIDITY_USD_THRESHOLD
        )
    )

    selected_metrics.update(
        {
            "selected_quote_liquidity_usd": selected_metrics.get("quote_liquidity_usd"),
            "quote_liquidity_usd": aggregate_quote_liquidity_usd,
            "market_sanity_status": MARKET_SANITY_MISLEADING_LIQUIDITY if misleading else MARKET_SANITY_OK,
            "market_sanity_reason": MARKET_SANITY_REASON_MISLEADING_LIQUIDITY if misleading else None,
            "misleading_liquidity": misleading,
        }
    )
    return selected_metrics


def select_best_pair(pairs, entry):
    if not pairs:
        return None, "not_found"

    pool_address = normalize_evm_address(entry.get("pool_address"))
    if pool_address:
        for pair in pairs:
            pair_address = normalize_evm_address(pair.get("pairAddress"))
            if pair_address == pool_address:
                return pair, "exact_pool"

    return max(pairs, key=liquidity_usd), "token_level"


def pair_created_at_datetime(pair):
    if not isinstance(pair, dict):
        return None

    try:
        value = int(pair.get("pairCreatedAt") or 0)
    except (TypeError, ValueError):
        return None

    if value <= 0:
        return None

    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def pair_created_at_iso(pair):
    created_at = pair_created_at_datetime(pair)
    return to_iso(created_at) if created_at else None


def select_oldest_pair(pairs):
    dated_pairs = [
        (pair_created_at_datetime(pair), pair)
        for pair in pairs
        if pair_created_at_datetime(pair)
    ]
    if not dated_pairs:
        return None

    return min(dated_pairs, key=lambda item: item[0])[1]


def minimum_token_age_inferred(pairs, selected_pair, current_time):
    oldest_pair = select_oldest_pair(pairs)
    oldest_created_at = pair_created_at_datetime(oldest_pair)
    selected_created_at = pair_created_at_datetime(selected_pair)

    source = None
    inferred_at = None
    if oldest_created_at:
        inferred_at = oldest_created_at
        source = MINIMUM_TOKEN_AGE_SOURCE_OLDEST_PAIR
    elif selected_created_at:
        inferred_at = selected_created_at
        source = MINIMUM_TOKEN_AGE_SOURCE_SELECTED_PAIR

    age_minutes = None
    if inferred_at:
        age_minutes = max(0, (current_time - inferred_at).total_seconds() / 60)

    return {
        "oldest_pair_created_at_utc": to_iso(oldest_created_at) if oldest_created_at else None,
        "oldest_pair_age_minutes": round(max(0, (current_time - oldest_created_at).total_seconds() / 60), 2)
        if oldest_created_at else None,
        "selected_pair_created_at_utc": to_iso(selected_created_at) if selected_created_at else None,
        "minimum_token_age_inferred_minutes": round(age_minutes, 2) if age_minutes is not None else None,
        "minimum_token_age_inferred_source": source,
    }


def calculate_social_eligibility(selected_pair, inferred_age, current_time, config=None):
    config = config or DEFAULT_CONFIG["market_ranker"]["social_eligibility"]
    if not selected_pair:
        return {
            "social_eligibility": SOCIAL_ELIGIBILITY_PENDING,
            "social_eligibility_reason": SOCIAL_ELIGIBILITY_REASON_PENDING_DEX,
            **inferred_age,
        }

    age_minutes = numeric_or_none(inferred_age.get("minimum_token_age_inferred_minutes"))
    if age_minutes is None:
        return {
            "social_eligibility": SOCIAL_ELIGIBILITY_PENDING,
            "social_eligibility_reason": SOCIAL_ELIGIBILITY_REASON_PENDING_DEX,
            **inferred_age,
        }

    max_pool_age_minutes = float(
        config.get("max_pool_age_minutes")
        or SOCIAL_ELIGIBILITY_MAX_MARKET_AGE_HOURS * 60
    )
    if age_minutes > max_pool_age_minutes:
        return {
            "social_eligibility": SOCIAL_ELIGIBILITY_BLOCKED_OLD_MARKET,
            "social_eligibility_reason": SOCIAL_ELIGIBILITY_REASON_OLD_MARKET,
            **inferred_age,
        }

    return {
        "social_eligibility": SOCIAL_ELIGIBILITY_ELIGIBLE,
        "social_eligibility_reason": SOCIAL_ELIGIBILITY_REASON_FRESH_MARKET,
        **inferred_age,
    }


def component_liquidity(value):
    if value >= 10000:
        return 100
    if value >= 5000:
        return 80
    if value >= 2000:
        return 50
    if value >= 1000:
        return 30
    if value > 0:
        return 10
    return 0


def component_volume(value):
    if value >= 10000:
        return 100
    if value >= 5000:
        return 80
    if value >= 1000:
        return 50
    if value >= 100:
        return 20
    return 0


def component_txns(value):
    if value >= 50:
        return 100
    if value >= 20:
        return 80
    if value >= 10:
        return 50
    if value >= 1:
        return 20
    return 0


def component_age(age_minutes):
    if age_minutes is None:
        return 0
    if age_minutes <= 5:
        return 100
    if age_minutes <= 15:
        return 80
    if age_minutes <= 30:
        return 50
    if age_minutes <= 60:
        return 20
    return 0


def token_start_time(entry):
    return parse_iso(entry.get("created_at_utc")) or parse_iso(entry.get("discovered_at_utc"))


def calculate_market_score(pair, entry, current_time, weights=None, sanity_config=None, inferred_age=None, pairs=None):
    weights = weights or DEFAULT_CONFIG["market_ranker"]["score_weights"]
    sanity_config = sanity_config or DEFAULT_CONFIG["market_ranker"]["market_sanity"]
    total_weight = sum(float(value) for value in weights.values())
    if total_weight <= 0:
        weights = DEFAULT_CONFIG["market_ranker"]["score_weights"]
        total_weight = sum(float(value) for value in weights.values())

    age_minutes = numeric_or_none(inferred_age.get("minimum_token_age_inferred_minutes")) if inferred_age else None

    quote_metrics = aggregate_quote_liquidity_metrics(
        pairs or [pair],
        pair,
        entry.get("token_address"),
        config=sanity_config,
    )
    quote_liquidity = quote_metrics.get("quote_liquidity_usd")
    if quote_liquidity is None:
        quote_liquidity = quote_metrics["liquidity_usd"]
    volume = nested_number(pair, ["volume", "h24"])
    txns = h24_txns(pair)

    components = {
        "quote_liquidity": component_liquidity(quote_liquidity),
        "volume_h24": component_volume(volume),
        "txns_h24": component_txns(txns),
        "minimum_token_age_inferred": component_age(age_minutes),
    }
    score = sum(
        components.get(key, 0) * float(weight)
        for key, weight in weights.items()
    ) / total_weight

    if quote_metrics["misleading_liquidity"]:
        score *= float(sanity_config.get("misleading_score_multiplier") or 0.25)

    return round(score, 2), components, {
        **quote_metrics,
        "volume_h24": volume,
        "txns_h24": txns,
        **(inferred_age or {}),
    }


def update_state_for_result(state, token, dex_status, market_score, current_time):
    watchlist_key = token["watchlist_key"]
    entry = token["entry"]
    now_text = to_iso(current_time)
    item = state.setdefault(
        watchlist_key,
        {
            "first_seen_by_ranker_at_utc": now_text,
            "first_dex_found_at_utc": None,
            "dex_delay_seconds": None,
            "last_checked_at_utc": None,
            "last_dex_status": None,
            "last_market_score": None,
            "check_count": 0,
        },
    )
    item.setdefault("first_seen_by_ranker_at_utc", now_text)
    item["last_checked_at_utc"] = now_text
    item["last_dex_status"] = dex_status
    item["last_market_score"] = market_score
    item["check_count"] = int(item.get("check_count") or 0) + 1

    if dex_status == "found" and not item.get("first_dex_found_at_utc"):
        item["first_dex_found_at_utc"] = now_text
        start_time = token_start_time(entry)
        if start_time:
            item["dex_delay_seconds"] = max(0, int((current_time - start_time).total_seconds()))

    return item


def pair_summary(pair):
    if not pair:
        return None

    return {
        "chain_id": pair.get("chainId"),
        "dex_id": pair.get("dexId"),
        "pair_address": pair.get("pairAddress"),
        "base_token": pair.get("baseToken"),
        "quote_token": pair.get("quoteToken"),
        "pair_created_at": pair.get("pairCreatedAt"),
    }


def token_identity_from_pair(pair, token_address):
    if not isinstance(pair, dict):
        return {}

    normalized_token = normalize_evm_address(token_address)
    for side in ("baseToken", "quoteToken"):
        token = pair.get(side)
        if not isinstance(token, dict):
            continue
        if normalize_evm_address(token.get("address")) != normalized_token:
            continue
        return {
            "token_name": token.get("name"),
            "token_symbol": token.get("symbol"),
        }

    return {}


def build_snapshot(
    token,
    dex_status,
    pairs_count,
    selected_pair,
    association_type,
    market_score,
    components,
    metrics,
    social_eligibility,
    state_item,
    current_time,
    error=None,
):
    return {
        "timestamp": to_iso(current_time),
        "ranker_version": MARKET_RANKER_VERSION,
        "watchlist_key": token["watchlist_key"],
        "chain": token["chain"],
        "token_address": token["token_address"],
        "status": token["entry"].get("status"),
        "dex_status": dex_status,
        "pairs_count": pairs_count,
        "association_type": association_type,
        "market_score": market_score,
        "score_components": components,
        "observed_metrics": metrics,
        "social_eligibility": social_eligibility,
        "selected_pair": pair_summary(selected_pair),
        "first_seen_by_ranker_at_utc": state_item.get("first_seen_by_ranker_at_utc"),
        "first_dex_found_at_utc": state_item.get("first_dex_found_at_utc"),
        "dex_delay_seconds": state_item.get("dex_delay_seconds"),
        "check_count": state_item.get("check_count"),
        "error": error,
    }


def update_watchlist_market_fields(updates_by_key):
    if not updates_by_key:
        return

    with watchlist_lock():
        watchlist = load_watchlist()
        for watchlist_key, update in updates_by_key.items():
            entry = watchlist.get(watchlist_key)
            if isinstance(entry, dict):
                for field in DEPRECATED_WATCHLIST_FIELDS:
                    entry.pop(field, None)
                entry.update(update)
        atomic_save_json(WATCHLIST_FILE, watchlist)


def retention_config(config):
    configured = market_ranker_config(config).get("watchlist_retention") or {}
    defaults = DEFAULT_CONFIG["market_ranker"]["watchlist_retention"]
    return merge_dict(defaults, configured)


def config_int(config, key, default_value):
    try:
        return int(config.get(key, default_value))
    except (TypeError, ValueError):
        return int(default_value)


def config_float(config, key, default_value):
    try:
        return float(config.get(key, default_value))
    except (TypeError, ValueError):
        return float(default_value)


def retention_archive_path(config):
    archive_file = config.get("archive_file") or DEFAULT_CONFIG["market_ranker"]["watchlist_retention"]["archive_file"]
    archive_path = Path(archive_file)
    if not archive_path.is_absolute():
        archive_path = PROJECT_ROOT / archive_path
    return archive_path


def numeric_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def entry_reference_time(entry):
    for field in (
        "social_eligibility_updated_at",
        "last_seen_at_utc",
        "created_at_utc",
        "discovered_at_utc",
    ):
        parsed = parse_iso(entry.get(field))
        if parsed:
            return parsed
    return None


def entry_age_hours(entry, current_time):
    reference_time = entry_reference_time(entry)
    if not reference_time:
        return 0
    return max(0, (current_time - reference_time).total_seconds() / 3600)


def is_retention_protected(entry):
    return (
        entry.get("status") == STATUS_ATIVO
        or entry.get("social_status") == STATUS_ATIVO
        or entry.get("monitor_status") == STATUS_ATIVO
        or entry.get("telegram_alert_sent") is True
    )


def retention_reason(entry, config, current_time):
    if is_retention_protected(entry):
        return None

    age_hours = entry_age_hours(entry, current_time)
    social_eligibility = entry.get("social_eligibility")
    market_score = numeric_or_none(entry.get("market_score"))

    if social_eligibility == SOCIAL_ELIGIBILITY_BLOCKED_OLD_MARKET:
        if age_hours >= config_int(config, "blocked_old_market_retention_hours", 6):
            return "retention_blocked_old_market"
        return None

    if social_eligibility == SOCIAL_ELIGIBILITY_PENDING:
        if age_hours >= config_int(config, "pending_retention_hours", 12):
            return "retention_pending_dexscreener"
        return None

    if social_eligibility is None and market_score is None:
        if age_hours >= config_int(config, "unranked_retention_hours", 6):
            return "retention_unranked"
        return None

    low_score_threshold = config_float(config, "low_score_threshold", 35)
    if market_score is not None and market_score < low_score_threshold:
        if age_hours >= config_int(config, "low_score_retention_hours", 24):
            return "retention_low_market_score"

    return None


def blind_cap_sort_key(item):
    watchlist_key, entry = item
    score = numeric_or_none(entry.get("market_score"))
    score_value = score if score is not None else -1
    eligibility = entry.get("social_eligibility")
    eligibility_rank = {
        SOCIAL_ELIGIBILITY_BLOCKED_OLD_MARKET: 0,
        SOCIAL_ELIGIBILITY_PENDING: 1,
        None: 2,
        SOCIAL_ELIGIBILITY_ELIGIBLE: 3,
    }.get(eligibility, 2)
    reference_time = entry_reference_time(entry) or datetime.min.replace(tzinfo=timezone.utc)
    return (eligibility_rank, score_value, reference_time, watchlist_key)


def archive_removed_watchlist_entries(records, config, current_time):
    if not records or not config.get("archive_removed", True):
        return

    path = retention_archive_path(config)
    for record in records:
        append_jsonl(
            path,
            {
                "archived_at_utc": to_iso(current_time),
                **record,
            },
        )


def apply_watchlist_retention(config, current_time):
    config = retention_config(config)
    if not config.get("enabled", True):
        return {"enabled": False, "removed": 0, "max_entries": None, "remaining": None}

    max_entries = config_int(config, "max_entries", 500)
    removed_records = []

    with watchlist_lock():
        watchlist = load_watchlist()
        kept = {}

        for watchlist_key, entry in watchlist.items():
            if not isinstance(entry, dict):
                kept[watchlist_key] = entry
                continue

            reason = retention_reason(entry, config, current_time)
            if reason:
                removed_records.append(
                    {
                        "reason": reason,
                        "watchlist_key": watchlist_key,
                        "entry": entry,
                    }
                )
                continue

            kept[watchlist_key] = entry

        if max_entries > 0 and len(kept) > max_entries:
            removable = [
                (watchlist_key, entry)
                for watchlist_key, entry in kept.items()
                if isinstance(entry, dict) and not is_retention_protected(entry)
            ]
            removable.sort(key=blind_cap_sort_key)
            excess = len(kept) - max_entries
            for watchlist_key, entry in removable[:excess]:
                kept.pop(watchlist_key, None)
                removed_records.append(
                    {
                        "reason": "retention_blind_cap",
                        "watchlist_key": watchlist_key,
                        "entry": entry,
                    }
                )

        if removed_records:
            atomic_save_json(WATCHLIST_FILE, kept)

    archive_removed_watchlist_entries(removed_records, config, current_time)
    return {
        "enabled": True,
        "removed": len(removed_records),
        "max_entries": max_entries,
        "remaining": len(kept) if removed_records else len(load_watchlist()),
    }


def print_summary(summary, results):
    print("=== KRPTO-V | Market Ranker ===")
    print(f"Versao: {MARKET_RANKER_VERSION}")
    print(f"Ciclo: {summary['timestamp']}")
    print(f"Dry-run: {str(summary['dry_run']).lower()}")
    print(f"Tokens lidos da WL: {summary['watchlist_total']}")
    print(f"Tokens consultados: {summary['tokens_checked']}")
    print(f"Chamadas Dexscreener em lote: {summary.get('dex_batch_calls', 0)}")
    print(f"Encontrados na Dexscreener: {summary['dex_found']}")
    print(f"Nao encontrados: {summary['dex_not_found']}")
    print(f"Erros: {summary['errors']}")
    retention = summary.get("watchlist_retention") or {}
    if retention.get("enabled"):
        print(
            "Retencao WL: "
            f"removidos={retention.get('removed', 0)} | "
            f"restantes={retention.get('remaining')} | "
            f"teto={retention.get('max_entries')}"
        )
    if "ops_alert_sent" in summary:
        print(f"Alerta operacional enviado: {str(summary['ops_alert_sent']).lower()}")
        if summary.get("ops_alert_message_id") is not None:
            print(f"Telegram message_id operacional: {summary['ops_alert_message_id']}")
        if summary.get("ops_alert_error"):
            print(f"Alerta operacional erro: {summary['ops_alert_error']}")
    error_counts, examples = error_summary_from_results(results)
    if error_counts:
        print("Erros por tipo:")
        for error_type, count in error_counts.most_common():
            print(f"- {count} | {error_type} | exemplo: {examples.get(error_type)}")

    top = sorted(
        [item for item in results if item.get("market_score") is not None],
        key=lambda item: item["market_score"],
        reverse=True,
    )[:10]
    print("Top 10 por market_score:")
    if not top:
        print("- nenhum token pontuado")
    for item in top:
        print(
            f"- {item['market_score']:>6.2f} | {item['chain']} | "
            f"{item['token_address']} | {item['association_type']}"
        )

    delayed = sorted(
        [
            item
            for item in results
            if item.get("dex_delay_seconds") is not None
        ],
        key=lambda item: item["dex_delay_seconds"],
        reverse=True,
    )[:10]
    print("Maiores delays ate Dexscreener:")
    if not delayed:
        print("- nenhum delay medido")
    for item in delayed:
        print(
            f"- {item['dex_delay_seconds']}s | {item['chain']} | "
            f"{item['token_address']} | score={item.get('market_score')}"
        )


def run_cycle(dry_run=False, session=requests):
    ensure_directories()
    config = load_config()
    weights = score_weights(config)
    eligibility_config = social_eligibility_config(config)
    sanity_config = market_sanity_config(config)
    current_time = utc_now()
    now_text = to_iso(current_time)
    watchlist = load_watchlist()
    state = load_state()
    rankable_tokens = select_rankable_tokens(watchlist)

    results = []
    updates_by_key = {}
    dex_found = 0
    dex_not_found = 0
    errors = 0
    pairs_by_key, errors_by_key, dex_batch_calls = fetch_pairs_for_rankable_tokens(
        rankable_tokens,
        session=session,
    )

    for token in rankable_tokens:
        dex_status = "not_found"
        pairs = []
        selected_pair = None
        association_type = None
        market_score = None
        components = None
        metrics = None
        social_eligibility = {
            "social_eligibility": SOCIAL_ELIGIBILITY_PENDING,
            "social_eligibility_reason": SOCIAL_ELIGIBILITY_REASON_PENDING_DEX,
            "oldest_pair_created_at_utc": None,
            "oldest_pair_age_minutes": None,
            "selected_pair_created_at_utc": None,
        }
        error_text = None

        try:
            if errors_by_key.get(token["watchlist_key"]):
                raise RuntimeError(errors_by_key[token["watchlist_key"]])

            pairs = pairs_by_key.get(token["watchlist_key"], [])
            selected_pair, association_type = select_best_pair(pairs, token["entry"])
            inferred_age = minimum_token_age_inferred(pairs, selected_pair, current_time)
            if selected_pair:
                dex_status = "found"
                market_score, components, metrics = calculate_market_score(
                    selected_pair,
                    token["entry"],
                    current_time,
                    weights=weights,
                    sanity_config=sanity_config,
                    inferred_age=inferred_age,
                    pairs=pairs,
                )
                dex_found += 1
            else:
                dex_not_found += 1
                metrics = inferred_age

            social_eligibility = calculate_social_eligibility(
                selected_pair,
                inferred_age,
                current_time,
                config=eligibility_config,
            )

            updates_by_key.setdefault(token["watchlist_key"], {}).update(
                {
                    "social_eligibility": social_eligibility["social_eligibility"],
                    "social_eligibility_reason": social_eligibility["social_eligibility_reason"],
                    "social_eligibility_updated_at": now_text,
                    "oldest_pair_created_at_utc": social_eligibility["oldest_pair_created_at_utc"],
                    "oldest_pair_age_minutes": social_eligibility["oldest_pair_age_minutes"],
                    "selected_pair_created_at_utc": social_eligibility["selected_pair_created_at_utc"],
                    "minimum_token_age_inferred_minutes": social_eligibility["minimum_token_age_inferred_minutes"],
                    "minimum_token_age_inferred_source": social_eligibility["minimum_token_age_inferred_source"],
                }
            )
            if market_score is not None:
                updates_by_key[token["watchlist_key"]]["market_score"] = market_score
                updates_by_key[token["watchlist_key"]].update(metrics or {})
                identity = token_identity_from_pair(selected_pair, token["token_address"])
                for field, value in identity.items():
                    if value:
                        updates_by_key[token["watchlist_key"]][field] = value
        except Exception as error:
            dex_status = "error"
            error_text = str(error)
            errors += 1

        state_item = update_state_for_result(
            state,
            token,
            dex_status,
            market_score,
            current_time,
        )
        snapshot = build_snapshot(
            token=token,
            dex_status=dex_status,
            pairs_count=len(pairs),
            selected_pair=selected_pair,
            association_type=association_type,
            market_score=market_score,
            components=components,
            metrics=metrics,
            social_eligibility=social_eligibility,
            state_item=state_item,
            current_time=current_time,
            error=error_text,
        )
        append_jsonl(snapshots_file_path(current_time), snapshot)
        results.append(snapshot)

    atomic_save_json(STATE_FILE, state)

    if not dry_run:
        update_watchlist_market_fields(updates_by_key)
        retention_summary = apply_watchlist_retention(config, current_time)
    else:
        retention_summary = {"enabled": False, "removed": 0, "max_entries": None, "remaining": len(watchlist)}

    summary = {
        "timestamp": now_text,
        "dry_run": dry_run,
        "watchlist_total": len(watchlist),
        "tokens_checked": len(rankable_tokens),
        "dex_found": dex_found,
        "dex_not_found": dex_not_found,
        "errors": errors,
        "dex_batch_calls": dex_batch_calls,
        "market_score_weights": weights,
        "watchlist_retention": retention_summary,
    }
    ops_alert_result = maybe_send_ops_alert(summary, results, current_time, config=config)
    if ops_alert_result and ops_alert_result.get("success"):
        summary["ops_alert_sent"] = True
        summary["ops_alert_message_id"] = ops_alert_result.get("message_id")
    elif ops_alert_result:
        summary["ops_alert_sent"] = False
        summary["ops_alert_error"] = ops_alert_result.get("error")
    print_summary(summary, results)
    return summary


def parse_args():
    parser = argparse.ArgumentParser(
        description="Ranqueia tokens da watchlist com dados de mercado da Dexscreener.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Calcula e grava snapshots/state sem atualizar data/watchlist.json.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    run_cycle(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
