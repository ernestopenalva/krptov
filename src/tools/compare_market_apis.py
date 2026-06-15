import argparse
import json
import math
import os
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ModuleNotFoundError:
    class _MissingRequests:
        class RequestException(Exception):
            pass

        @staticmethod
        def get(*_args, **_kwargs):
            raise RuntimeError("Instale requests antes de consultar APIs: pip install requests")

    requests = _MissingRequests()


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POOL_SCANNER_DATA_DIR = PROJECT_ROOT / "data" / "pool_scanner"
OUTPUT_DIR = PROJECT_ROOT / "data" / "market_api_compare"

DEXSCREENER_PAIR_URL = "https://api.dexscreener.com/latest/dex/pairs/{chain}/{pool_address}"
DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chain}/{token_address}"
GECKOTERMINAL_POOL_URL = "https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool_address}"
GECKOTERMINAL_TOKEN_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{token_address}/pools"
DEXPAPRIKA_POOL_URL = "https://api.dexpaprika.com/networks/{network}/pools/{pool_address}"
DEXPAPRIKA_TOKEN_POOLS_URL = "https://api.dexpaprika.com/networks/{network}/tokens/{token_address}/pools"

REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_SLEEP_SECONDS = 0.35
DEFAULT_RETRIES = 3
DEFAULT_APIS = ("dexscreener", "geckoterminal", "dexpaprika")

GECKOTERMINAL_NETWORKS = {
    "ethereum": "eth",
    "base": "base",
    "bsc": "bsc",
}

DEXPAPRIKA_NETWORKS = {
    "ethereum": "ethereum",
    "base": "base",
    "bsc": "bsc",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Compara Dexscreener, GeckoTerminal e DexPaprika usando pools/tokens "
            "ja descobertos pelo pool_scanner."
        ),
    )
    parser.add_argument(
        "--date",
        help="Data UTC dos eventos do pool scanner: YYYY-MM-DD. Se omitida, usa todos.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limita a quantidade de tokens/pools testados. 0 = sem limite.",
    )
    parser.add_argument(
        "--source",
        action="append",
        help="Filtra source, como uniswap_v2, uniswap_v3 ou uniswap_v4. Pode repetir.",
    )
    parser.add_argument(
        "--api",
        action="append",
        choices=DEFAULT_APIS,
        help="API a testar. Pode repetir. Padrao: as tres.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=DEFAULT_SLEEP_SECONDS,
        help="Pausa entre chamadas para reduzir risco de rate limit. Padrao: 0.35.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="Tentativas extras para 429/rate limit. Padrao: 3.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Arquivo JSONL de saida. Padrao: data/market_api_compare/results_YYYY-MM-DD_HHMMSS.jsonl.",
    )
    return parser.parse_args()


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")


def normalize_address(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if len(value) == 42 and value.startswith("0x"):
        return value
    return None


def number(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def nested(data, path, default=None):
    current = data
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def decode_block_timestamp(scanner_event):
    raw_log = scanner_event.get("raw_log") or {}
    block_timestamp = raw_log.get("blockTimestamp")
    if isinstance(block_timestamp, str):
        try:
            return datetime.fromtimestamp(int(block_timestamp, 16), tz=timezone.utc).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z")
        except ValueError:
            pass
    return scanner_event.get("received_at_utc")


def event_identity(scanner_event):
    decoded = scanner_event.get("decoded_event") or {}
    return decoded.get("pool_address") or decoded.get("pool_id")


def load_scanner_events(date=None, sources=None):
    events = []
    paths = sorted(POOL_SCANNER_DATA_DIR.glob(f"events_{date}.jsonl" if date else "events_*.jsonl"))
    allowed_sources = set(sources or [])

    for path in paths:
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not event.get("candidate"):
                    continue
                if allowed_sources and event.get("source") not in allowed_sources:
                    continue
                identity = event_identity(event)
                if not identity:
                    continue
                events.append(event)

    return events


def select_unique_events(events):
    selected = {}
    for event in events:
        key = f"{event.get('chain')}:{event_identity(event).lower()}"
        selected.setdefault(key, event)
    return list(selected.values())


def build_case(scanner_event):
    decoded = scanner_event.get("decoded_event") or {}
    candidate = scanner_event["candidate"]
    pool_address = normalize_address(decoded.get("pool_address"))
    pool_id = decoded.get("pool_id")
    return {
        "case_id": f"{scanner_event['chain']}:{(pool_address or pool_id).lower()}",
        "chain": scanner_event["chain"],
        "source": scanner_event["source"],
        "source_type": scanner_event.get("source_type"),
        "token_address": candidate["token_address"].lower(),
        "quote_token": candidate["quote_token"],
        "quote_token_address": candidate["quote_token_address"].lower(),
        "pool_address": pool_address,
        "pool_id": pool_id,
        "pool_created_at_utc": decode_block_timestamp(scanner_event),
        "scanner_received_at_utc": scanner_event.get("received_at_utc"),
    }


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


def output_path_from_args(args):
    if args.output:
        return args.output
    return OUTPUT_DIR / f"results_{utc_stamp()}.jsonl"


def pair_addresses(pair):
    return {
        normalize_address(nested(pair, ["baseToken", "address"])),
        normalize_address(nested(pair, ["quoteToken", "address"])),
    }


def gecko_pair_addresses(resource):
    relationships = resource.get("relationships") or {}
    base_id = nested(relationships, ["base_token", "data", "id"], "")
    quote_id = nested(relationships, ["quote_token", "data", "id"], "")
    return {
        normalize_address(str(base_id).split("_")[-1]),
        normalize_address(str(quote_id).split("_")[-1]),
    }


def paprika_pair_addresses(pool):
    return {
        normalize_address(token.get("id"))
        for token in pool.get("tokens") or []
        if isinstance(token, dict)
    }


def matches_candidate(addresses, case):
    return {case["token_address"], case["quote_token_address"]}.issubset(addresses)


def created_distance_seconds(created_at, pool_created_at):
    if not created_at or not pool_created_at:
        return math.inf
    try:
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        target = datetime.fromisoformat(pool_created_at.replace("Z", "+00:00"))
    except ValueError:
        return math.inf
    return abs((created - target).total_seconds())


def choose_best(candidates, case, liquidity_getter, created_getter, address_getter):
    if not candidates:
        return None

    matching = [
        item
        for item in candidates
        if matches_candidate(address_getter(item), case)
    ]
    pool = matching or candidates

    return min(
        pool,
        key=lambda item: (
            created_distance_seconds(created_getter(item), case.get("pool_created_at_utc")),
            -liquidity_getter(item),
        ),
    )


def retry_after_seconds(error, attempt):
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) or {}
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return max(1.0, float(retry_after))
        except ValueError:
            pass
    return min(10.0, 1.5 * (attempt + 1))


def get_json(session, url, cache=None, retries=DEFAULT_RETRIES):
    if cache is not None and url in cache:
        return cache[url]

    last_error = None
    for attempt in range(retries + 1):
        if attempt > 0:
            time.sleep(retry_after_seconds(last_error, attempt - 1))

        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
            if cache is not None:
                cache[url] = payload
            return payload
        except requests.RequestException as error:
            last_error = error
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            if status_code != 429:
                raise

    raise last_error


def get_json_no_retry(session, url):
    response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def normalize_dexscreener_pair(pair):
    txns_h24 = nested(pair, ["txns", "h24"], {}) or {}
    return {
        "dex_id": pair.get("dexId"),
        "dex_name": pair.get("dexId"),
        "pool_address": normalize_address(pair.get("pairAddress")),
        "pair_created_at": pair.get("pairCreatedAt"),
        "liquidity_usd": number(nested(pair, ["liquidity", "usd"])),
        "volume_h24_usd": number(nested(pair, ["volume", "h24"])),
        "txns_h24": int(number(txns_h24.get("buys")) + number(txns_h24.get("sells"))),
        "raw_url": pair.get("url"),
    }


def query_dexscreener(case, session=requests, cache=None, retries=DEFAULT_RETRIES):
    if case["pool_address"]:
        url = DEXSCREENER_PAIR_URL.format(
            chain=case["chain"],
            pool_address=case["pool_address"],
        )
        payload = get_json(session, url, cache=cache, retries=retries)
        pairs = payload.get("pairs") or []
        pair = next(
            (
                item for item in pairs
                if normalize_address(item.get("pairAddress")) == case["pool_address"]
            ),
            pairs[0] if pairs else None,
        )
        query_mode = "exact_pool"
    else:
        url = DEXSCREENER_TOKEN_PAIRS_URL.format(
            chain=case["chain"],
            token_address=case["token_address"],
        )
        pairs = get_json(session, url, cache=cache, retries=retries) or []
        pair = choose_best(
            pairs,
            case,
            liquidity_getter=lambda item: number(nested(item, ["liquidity", "usd"])),
            created_getter=lambda item: None,
            address_getter=pair_addresses,
        )
        query_mode = "token_pools"

    return build_api_result("dexscreener", case, query_mode, pair, normalize_dexscreener_pair)


def normalize_gecko_pair(resource):
    attributes = resource.get("attributes") or {}
    relationships = resource.get("relationships") or {}
    txns_h24 = nested(attributes, ["transactions", "h24"], {}) or {}
    return {
        "dex_id": nested(relationships, ["dex", "data", "id"]),
        "dex_name": nested(relationships, ["dex", "data", "id"]),
        "pool_address": normalize_address(str(resource.get("id", "")).split("_")[-1]),
        "pair_created_at": attributes.get("pool_created_at"),
        "liquidity_usd": number(attributes.get("reserve_in_usd")),
        "volume_h24_usd": number(nested(attributes, ["volume_usd", "h24"])),
        "txns_h24": int(number(txns_h24.get("buys")) + number(txns_h24.get("sells"))),
        "raw_url": None,
    }


def query_geckoterminal(case, session=requests, cache=None, retries=DEFAULT_RETRIES):
    network = GECKOTERMINAL_NETWORKS.get(case["chain"], case["chain"])
    if case["pool_address"]:
        url = GECKOTERMINAL_POOL_URL.format(
            network=network,
            pool_address=case["pool_address"],
        )
        payload = get_json(session, url, cache=cache, retries=retries)
        pair = payload.get("data")
        query_mode = "exact_pool"
    else:
        url = GECKOTERMINAL_TOKEN_POOLS_URL.format(
            network=network,
            token_address=case["token_address"],
        )
        payload = get_json(session, url, cache=cache, retries=retries)
        pairs = payload.get("data") or []
        pair = choose_best(
            pairs,
            case,
            liquidity_getter=lambda item: number(nested(item, ["attributes", "reserve_in_usd"])),
            created_getter=lambda item: nested(item, ["attributes", "pool_created_at"]),
            address_getter=gecko_pair_addresses,
        )
        query_mode = "token_pools"

    return build_api_result("geckoterminal", case, query_mode, pair, normalize_gecko_pair)


def normalize_dexpaprika_pool(pool):
    txns_h24 = pool.get("24h") or {}
    return {
        "dex_id": pool.get("dex_id"),
        "dex_name": pool.get("dex_name"),
        "pool_address": normalize_address(pool.get("id")),
        "pair_created_at": pool.get("created_at"),
        "liquidity_usd": number(pool.get("liquidity_usd") or pool.get("liquidity")),
        "volume_h24_usd": number(txns_h24.get("volume_usd") or txns_h24.get("volume")),
        "txns_h24": int(
            number(txns_h24.get("txns"))
            or number(txns_h24.get("transactions"))
            or (number(txns_h24.get("buys")) + number(txns_h24.get("sells")))
        ),
        "raw_url": None,
    }


def extract_paprika_pools(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("pools", "data", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def query_dexpaprika(case, session=requests, cache=None, retries=DEFAULT_RETRIES):
    network = DEXPAPRIKA_NETWORKS.get(case["chain"], case["chain"])
    if case["pool_address"]:
        url = DEXPAPRIKA_POOL_URL.format(
            network=network,
            pool_address=case["pool_address"],
        )
        pair = get_json(session, url, cache=cache, retries=retries)
        query_mode = "exact_pool"
    else:
        url = DEXPAPRIKA_TOKEN_POOLS_URL.format(
            network=network,
            token_address=case["token_address"],
        )
        payload = get_json(session, url, cache=cache, retries=retries)
        pairs = extract_paprika_pools(payload)
        pair = choose_best(
            pairs,
            case,
            liquidity_getter=lambda item: number(item.get("liquidity_usd") or item.get("liquidity")),
            created_getter=lambda item: item.get("created_at"),
            address_getter=paprika_pair_addresses,
        )
        query_mode = "token_pools"

    return build_api_result("dexpaprika", case, query_mode, pair, normalize_dexpaprika_pool)


def build_api_result(api_name, case, query_mode, pair, normalizer):
    result = {
        "api": api_name,
        "query_mode": query_mode,
        "found": bool(pair),
        "dex_id": None,
        "dex_name": None,
        "pool_address": None,
        "pair_created_at": None,
        "liquidity_usd": None,
        "volume_h24_usd": None,
        "txns_h24": None,
        "raw_url": None,
        "error": None,
    }
    if pair:
        result.update(normalizer(pair))
    return result


QUERY_FUNCTIONS = {
    "dexscreener": query_dexscreener,
    "geckoterminal": query_geckoterminal,
    "dexpaprika": query_dexpaprika,
}


def compare_case(case, apis, session=requests, cache=None, retries=DEFAULT_RETRIES):
    api_results = {}
    for api_name in apis:
        try:
            api_results[api_name] = QUERY_FUNCTIONS[api_name](
                case,
                session=session,
                cache=cache,
                retries=retries,
            )
        except requests.RequestException as error:
            api_results[api_name] = {
                "api": api_name,
                "query_mode": "error",
                "found": False,
                "dex_id": None,
                "dex_name": None,
                "pool_address": None,
                "pair_created_at": None,
                "liquidity_usd": None,
                "volume_h24_usd": None,
                "txns_h24": None,
                "raw_url": None,
                "error": str(error),
            }
    return {
        "checked_at_utc": utc_now_iso(),
        "case": case,
        "apis": api_results,
    }


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def summarize_results(results, apis):
    summary = {
        "cases": len(results),
        "sources": dict(Counter(item["case"]["source"] for item in results)),
        "by_api": {},
        "overlap": {},
    }

    for api_name in apis:
        api_rows = [item["apis"][api_name] for item in results]
        found = [item for item in api_rows if item.get("found")]
        liquidity = [item["liquidity_usd"] for item in found if item.get("liquidity_usd") is not None]
        volume = [item["volume_h24_usd"] for item in found if item.get("volume_h24_usd") is not None]
        txns = [item["txns_h24"] for item in found if item.get("txns_h24") is not None]
        summary["by_api"][api_name] = {
            "found": len(found),
            "not_found": len(api_rows) - len(found),
            "errors": sum(1 for item in api_rows if item.get("error")),
            "found_by_source": dict(
                Counter(
                    result["case"]["source"]
                    for result in results
                    if result["apis"][api_name].get("found")
                )
            ),
            "liquidity_median": statistics.median(liquidity) if liquidity else None,
            "volume_h24_median": statistics.median(volume) if volume else None,
            "txns_h24_median": statistics.median(txns) if txns else None,
        }

    for left in apis:
        for right in apis:
            if left == right:
                continue
            key = f"{left}_not_found__{right}_found"
            summary["overlap"][key] = sum(
                1
                for item in results
                if not item["apis"][left].get("found") and item["apis"][right].get("found")
            )

    return summary


def print_summary(summary):
    print("=== KRPTO-V | Market API Compare ===")
    print(f"Casos analisados: {summary['cases']}")
    print(f"Sources: {summary['sources']}")
    print()
    print("Por API:")
    for api_name, data in summary["by_api"].items():
        print(
            f"- {api_name}: found={data['found']} | not_found={data['not_found']} | "
            f"errors={data['errors']} | sources={data['found_by_source']} | "
            f"liq_mediana={data['liquidity_median']} | "
            f"vol_h24_mediano={data['volume_h24_median']} | "
            f"txns_h24_mediano={data['txns_h24_median']}"
        )
    print()
    print("Cobertura complementar:")
    for key, value in summary["overlap"].items():
        print(f"- {key}: {value}")


def main():
    args = parse_args()
    apis = args.api or list(DEFAULT_APIS)
    events = select_unique_events(load_scanner_events(date=args.date, sources=args.source))
    if args.limit > 0:
        events = events[: args.limit]

    output_path = output_path_from_args(args)
    results = []
    response_cache = {}

    print(f"Casos carregados: {len(events)}")
    print(f"APIs: {', '.join(apis)}")
    print(f"Saida: {output_path}")

    for index, scanner_event in enumerate(events, start=1):
        case = build_case(scanner_event)
        print(
            f"[{index}/{len(events)}] {case['source']} | {case['token_address']} | "
            f"{case['pool_address'] or case['pool_id']}"
        )
        result = compare_case(
            case,
            apis,
            cache=response_cache,
            retries=args.retries,
        )
        append_jsonl(output_path, result)
        results.append(result)
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    summary = summarize_results(results, apis)
    summary_path = output_path.with_suffix(".summary.json")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print()
    print_summary(summary)
    print()
    print(f"JSONL: {output_path}")
    print(f"Resumo: {summary_path}")


if __name__ == "__main__":
    main()
