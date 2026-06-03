import argparse
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests


MARKET_RANKER_VERSION = "krptov-market-ranker-v1-2026-06-03"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
MARKET_RANKER_DATA_DIR = DATA_DIR / "market_ranker"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
WATCHLIST_LOCK_FILE = DATA_DIR / "watchlist.lock"
STATE_FILE = MARKET_RANKER_DATA_DIR / "state.json"

DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chain}/{token_address}"
REQUEST_TIMEOUT_SECONDS = 20

STATUS_NOVO = "novo"
STATUS_ATIVO = "ativo"

WEIGHT_LIQUIDITY = 5
WEIGHT_VOLUME = 4
WEIGHT_TXNS = 3
WEIGHT_AGE = 5
TOTAL_WEIGHT = WEIGHT_LIQUIDITY + WEIGHT_VOLUME + WEIGHT_TXNS + WEIGHT_AGE


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


def calculate_market_score(pair, entry, current_time):
    start_time = token_start_time(entry)
    age_minutes = None
    if start_time:
        age_minutes = max(0, (current_time - start_time).total_seconds() / 60)

    liquidity = liquidity_usd(pair)
    volume = nested_number(pair, ["volume", "h24"])
    txns = h24_txns(pair)

    components = {
        "liquidity": component_liquidity(liquidity),
        "volume_h24": component_volume(volume),
        "txns_h24": component_txns(txns),
        "age": component_age(age_minutes),
    }
    score = (
        components["liquidity"] * WEIGHT_LIQUIDITY
        + components["volume_h24"] * WEIGHT_VOLUME
        + components["txns_h24"] * WEIGHT_TXNS
        + components["age"] * WEIGHT_AGE
    ) / TOTAL_WEIGHT

    return round(score, 2), components, {
        "liquidity_usd": liquidity,
        "volume_h24": volume,
        "txns_h24": txns,
        "age_minutes": round(age_minutes, 2) if age_minutes is not None else None,
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


def build_snapshot(token, dex_status, pairs_count, selected_pair, association_type, market_score, components, metrics, state_item, current_time, error=None):
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
        "selected_pair": pair_summary(selected_pair),
        "first_seen_by_ranker_at_utc": state_item.get("first_seen_by_ranker_at_utc"),
        "first_dex_found_at_utc": state_item.get("first_dex_found_at_utc"),
        "dex_delay_seconds": state_item.get("dex_delay_seconds"),
        "check_count": state_item.get("check_count"),
        "error": error,
    }


def update_watchlist_scores(scores_by_key):
    if not scores_by_key:
        return

    with watchlist_lock():
        watchlist = load_watchlist()
        for watchlist_key, market_score in scores_by_key.items():
            entry = watchlist.get(watchlist_key)
            if isinstance(entry, dict):
                entry["market_score"] = market_score
        atomic_save_json(WATCHLIST_FILE, watchlist)


def print_summary(summary, results):
    print("=== KRPTO-V | Market Ranker ===")
    print(f"Versao: {MARKET_RANKER_VERSION}")
    print(f"Ciclo: {summary['timestamp']}")
    print(f"Dry-run: {str(summary['dry_run']).lower()}")
    print(f"Tokens lidos da WL: {summary['watchlist_total']}")
    print(f"Tokens consultados: {summary['tokens_checked']}")
    print(f"Encontrados na Dexscreener: {summary['dex_found']}")
    print(f"Nao encontrados: {summary['dex_not_found']}")
    print(f"Erros: {summary['errors']}")

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
    current_time = utc_now()
    now_text = to_iso(current_time)
    watchlist = load_watchlist()
    state = load_state()
    rankable_tokens = select_rankable_tokens(watchlist)

    results = []
    scores_by_key = {}
    dex_found = 0
    dex_not_found = 0
    errors = 0

    for token in rankable_tokens:
        dex_status = "not_found"
        pairs = []
        selected_pair = None
        association_type = None
        market_score = None
        components = None
        metrics = None
        error_text = None

        try:
            pairs = fetch_token_pairs(token["chain"], token["token_address"], session=session)
            selected_pair, association_type = select_best_pair(pairs, token["entry"])
            if selected_pair:
                dex_status = "found"
                market_score, components, metrics = calculate_market_score(
                    selected_pair,
                    token["entry"],
                    current_time,
                )
                scores_by_key[token["watchlist_key"]] = market_score
                dex_found += 1
            else:
                dex_not_found += 1
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
            state_item=state_item,
            current_time=current_time,
            error=error_text,
        )
        append_jsonl(snapshots_file_path(current_time), snapshot)
        results.append(snapshot)

    atomic_save_json(STATE_FILE, state)

    if not dry_run:
        update_watchlist_scores(scores_by_key)

    summary = {
        "timestamp": now_text,
        "dry_run": dry_run,
        "watchlist_total": len(watchlist),
        "tokens_checked": len(rankable_tokens),
        "dex_found": dex_found,
        "dex_not_found": dex_not_found,
        "errors": errors,
    }
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
