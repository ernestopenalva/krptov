"""Discover graduated Pump.fun tokens on PumpSwap and feed the market ranker."""

from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
import yaml
from dotenv import load_dotenv

from src.modules.chain_identity import make_watchlist_key, normalize_solana_address
from src.modules.runtime_ops import RuntimeOpsAlerter


SCANNER_VERSION = "krptov-token-scanner-solana-v1"
DEXSCREENER_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/tokens/v1/{chain}/{addresses}"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"
DATA_DIR = PROJECT_ROOT / "data"
RANKING_BUFFER_FILE = DATA_DIR / "ranking_buffer.json"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
MONITOR_WATCHLIST_FILE = DATA_DIR / "monitor_watchlist.json"
WATCHLIST_LOCK_FILE = DATA_DIR / "watchlist.lock"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def configured_path(value: object, default: Path) -> Path:
    if not value:
        return default
    path = Path(str(value))
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_config(path: Path = CONFIG_FILE) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as file:
        root = yaml.safe_load(file) or {}
    scanner = root.get("token_scanner_solana") or {}
    if not isinstance(scanner, dict):
        raise ValueError("config.yaml: token_scanner_solana precisa ser um objeto.")
    config = dict(scanner)
    config["_root_config"] = root
    config["state_file"] = configured_path(
        scanner.get("state_file"), DATA_DIR / "token_scanner_solana" / "state.json"
    )
    config["audit_dir"] = configured_path(
        scanner.get("audit_dir"), DATA_DIR / "token_scanner_solana"
    )
    return config


def load_json_dict(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} precisa conter um objeto JSON.")
    return payload


def atomic_save_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")
        file.flush()


@contextmanager
def watchlist_lock(timeout_seconds: float = 120, poll_seconds: float = 0.2):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    handle = None
    while True:
        try:
            handle = os.open(WATCHLIST_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(handle, str(os.getpid()).encode("ascii"))
            break
        except FileExistsError:
            if time.monotonic() - started >= timeout_seconds:
                raise TimeoutError(f"Timeout aguardando lock: {WATCHLIST_LOCK_FILE}")
            time.sleep(poll_seconds)
    try:
        yield
    finally:
        if handle is not None:
            os.close(handle)
        try:
            WATCHLIST_LOCK_FILE.unlink()
        except FileNotFoundError:
            pass


def chunked(values: List[str], size: int = 30) -> Iterable[List[str]]:
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


class DexscreenerDiscoveryProvider:
    """Small replaceable boundary around the current discovery provider."""

    name = "dexscreener"

    def __init__(self, session: requests.Session, timeout: float, backoff: float) -> None:
        self.session = session
        self.timeout = timeout
        self.backoff = backoff

    def _get_json(self, url: str) -> Any:
        response = self.session.get(url, timeout=self.timeout)
        if response.status_code == 429:
            print(f"[DEXSCREENER][429] Backoff de {self.backoff:g}s | {url}", flush=True)
            time.sleep(self.backoff)
            response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def latest_profiles(self) -> List[Dict[str, Any]]:
        payload = self._get_json(DEXSCREENER_PROFILES_URL)
        return payload if isinstance(payload, list) else []

    def pairs_for_tokens(self, chain: str, addresses: List[str]) -> List[Dict[str, Any]]:
        pairs: List[Dict[str, Any]] = []
        for group in chunked(addresses):
            url = DEXSCREENER_TOKENS_URL.format(chain=chain, addresses=",".join(group))
            payload = self._get_json(url)
            if isinstance(payload, list):
                pairs.extend(item for item in payload if isinstance(item, dict))
        return pairs


def build_discovery_provider(config: Dict[str, Any], session: requests.Session):
    provider_name = str(config.get("discovery_provider") or "dexscreener").lower()
    if provider_name != "dexscreener":
        raise ValueError(f"Discovery provider ainda nao implementado: {provider_name}")
    return DexscreenerDiscoveryProvider(
        session,
        float(config.get("request_timeout_seconds") or 20),
        float(config.get("rate_limit_backoff_seconds") or 10),
    )


def pair_token(pair: Dict[str, Any], side: str) -> Dict[str, Any]:
    value = pair.get(side)
    return value if isinstance(value, dict) else {}


def eligible_pumpswap_pair(
    pair: Dict[str, Any], token_address: str, config: Dict[str, Any]
) -> bool:
    if str(pair.get("chainId") or "").lower() != str(config.get("chain_id") or "solana"):
        return False
    if str(pair.get("dexId") or "").lower() != str(config.get("allowed_dex_id") or "pumpswap"):
        return False
    pair_address = normalize_solana_address(pair.get("pairAddress"))
    base = normalize_solana_address(pair_token(pair, "baseToken").get("address"))
    quote = normalize_solana_address(pair_token(pair, "quoteToken").get("address"))
    expected_quote = normalize_solana_address(config.get("quote_token_address"))
    return bool(
        pair_address and expected_quote and
        ((base == token_address and quote == expected_quote) or
         (quote == token_address and base == expected_quote))
    )


def liquidity_usd(pair: Dict[str, Any]) -> float:
    try:
        return float((pair.get("liquidity") or {}).get("usd") or 0)
    except (TypeError, ValueError):
        return 0.0


def select_pumpswap_pair(
    pairs: List[Dict[str, Any]], token_address: str, config: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    eligible = [pair for pair in pairs if eligible_pumpswap_pair(pair, token_address, config)]
    return max(eligible, key=liquidity_usd) if eligible else None


def jupiter_headers(config: Dict[str, Any]) -> Dict[str, str]:
    env_name = str((config.get("jupiter") or {}).get("api_key_env") or "JUPITER_API_KEY")
    api_key = os.getenv(env_name)
    return {"x-api-key": api_key} if api_key else {}


def jupiter_request(
    session: requests.Session, url: str, params: Dict[str, Any], config: Dict[str, Any]
) -> Dict[str, Any]:
    try:
        response = session.get(
            url,
            params=params,
            headers=jupiter_headers(config),
            timeout=float(config.get("request_timeout_seconds") or 20),
        )
        if response.status_code != 200:
            return {"ok": False, "status_code": response.status_code, "error": response.text[:500]}
        return {"ok": True, "data": response.json()}
    except Exception as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}


def price_impact(quote: Dict[str, Any]) -> Optional[float]:
    data = quote.get("data") if quote.get("ok") else None
    try:
        return float((data or {}).get("priceImpactPct"))
    except (TypeError, ValueError):
        return None


def observe_jupiter(
    token_address: str, config: Dict[str, Any], session: requests.Session, observed_at: str
) -> Dict[str, Any]:
    jupiter = config.get("jupiter") or {}
    sol_mint = config["quote_token_address"]
    common = {
        "slippageBps": int(jupiter.get("slippage_bps") or 100),
        "restrictIntermediateTokens": "true",
        "swapMode": "ExactIn",
    }
    buy = jupiter_request(session, jupiter["quote_url"], {
        **common, "inputMint": sol_mint, "outputMint": token_address,
        "amount": int(jupiter.get("buy_amount_lamports") or 10_000_000),
    }, config)
    sell = jupiter_request(session, jupiter["quote_url"], {
        **common, "inputMint": token_address, "outputMint": sol_mint,
        "amount": int(jupiter.get("sell_amount_raw") or 1_000_000),
    }, config)
    token_search = jupiter_request(
        session, jupiter["token_search_url"], {"query": token_address}, config
    )
    token_info = None
    data = token_search.get("data") if token_search.get("ok") else None
    if isinstance(data, list):
        token_info = next((item for item in data if item.get("id") == token_address), None)
    audit = (token_info or {}).get("audit") or {}
    mint_ok = None if token_info is None else (
        token_info.get("mintAuthority") is None or audit.get("mintAuthorityDisabled") is True
    )
    freeze_ok = None if token_info is None else (
        token_info.get("freezeAuthority") is None or audit.get("freezeAuthorityDisabled") is True
    )
    buy_route = bool(buy.get("ok") and (buy.get("data") or {}).get("routePlan"))
    sell_route = bool(sell.get("ok") and (sell.get("data") or {}).get("routePlan"))
    summary = {
        "observed_at_utc": observed_at,
        "available": bool(buy.get("ok") or sell.get("ok") or token_info),
        "jupiter_buy_quote_ok": buy_route,
        "jupiter_sell_quote_ok": sell_route,
        "mint_authority_ok": mint_ok,
        "freeze_authority_ok": freeze_ok,
        "approved_by_jupiter": bool(buy_route and sell_route and mint_ok and freeze_ok),
        "holder_count": (token_info or {}).get("holderCount"),
        "top_holders_percentage": audit.get("topHoldersPercentage"),
        "organic_score": (token_info or {}).get("organicScore"),
        "organic_score_label": (token_info or {}).get("organicScoreLabel"),
        "num_traders_1h": ((token_info or {}).get("stats1h") or {}).get("numTraders"),
        "buy_price_impact_pct": price_impact(buy),
        "sell_price_impact_pct": price_impact(sell),
    }
    return {"summary": summary, "raw": {"buy_quote": buy, "sell_quote": sell, "token_search": token_search}}


def pair_created_iso(pair: Dict[str, Any], fallback: str) -> str:
    try:
        value = int(pair.get("pairCreatedAt") or 0)
        if value > 0:
            return to_iso(datetime.fromtimestamp(value / 1000, tz=timezone.utc))
    except (TypeError, ValueError, OSError):
        pass
    return fallback


def token_identity(pair: Dict[str, Any], token_address: str) -> Dict[str, Any]:
    for side in ("baseToken", "quoteToken"):
        token = pair_token(pair, side)
        if normalize_solana_address(token.get("address")) == token_address:
            return {"token_symbol": token.get("symbol"), "token_name": token.get("name")}
    return {"token_symbol": None, "token_name": None}


def build_buffer_entry(
    token_address: str,
    profile: Dict[str, Any],
    pair: Dict[str, Any],
    jupiter: Dict[str, Any],
    config: Dict[str, Any],
    observed_at: str,
) -> Dict[str, Any]:
    chain = str(config.get("chain_id") or "solana")
    key = make_watchlist_key(chain, token_address)
    base = pair_token(pair, "baseToken")
    quote = pair_token(pair, "quoteToken")
    return {
        "watchlist_key": key,
        "chain": chain,
        "chain_id": chain,
        "token_address": token_address,
        **token_identity(pair, token_address),
        "pool_address": pair.get("pairAddress"),
        "pair_address": pair.get("pairAddress"),
        "base_mint": base.get("address"),
        "quote_mint": quote.get("address"),
        "quote_token": config.get("quote_token") or "SOL",
        "quote_token_address": config.get("quote_token_address"),
        "source": "pumpswap",
        "source_type": "token_discovered",
        "discovery_provider": str(config.get("discovery_provider") or "dexscreener"),
        "dex_id": pair.get("dexId"),
        "discovered_at_utc": observed_at,
        "created_at_utc": pair_created_iso(pair, observed_at),
        "last_seen_at_utc": observed_at,
        "times_seen": 1,
        "status": "novo",
        "social_status": "pendente",
        "monitor_status": "pendente",
        "telegram_alert_sent": False,
        "discarded_reason": None,
        "scanner_validation_status": "approved",
        "scanner_validation_reason": "dexscreener_pumpswap_wsol",
        "ranking_status": "pending_dexscreener",
        "ranking_first_seen_at_utc": observed_at,
        "ranking_last_seen_at_utc": observed_at,
        "ranking_attempts": 0,
        "discovery_profile": profile,
        "discovery_market_snapshot": {
            "liquidity_usd": liquidity_usd(pair),
            "volume": pair.get("volume"),
            "txns": pair.get("txns"),
            "price_change": pair.get("priceChange"),
        },
        "jupiter_observation": jupiter["summary"],
    }


def current_known_keys() -> set:
    known = set()
    for path in (RANKING_BUFFER_FILE, WATCHLIST_FILE, MONITOR_WATCHLIST_FILE):
        try:
            known.update(load_json_dict(path))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return known


def load_state(path: Path) -> Dict[str, Any]:
    payload = load_json_dict(path)
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {}
    return {"version": 1, "tokens": tokens}


def prune_state(state: Dict[str, Any], current_time: datetime, retention_hours: float) -> int:
    cutoff = current_time - timedelta(hours=retention_hours)
    removed = 0
    for address, item in list(state["tokens"].items()):
        last_seen = parse_iso((item or {}).get("last_seen_at_utc"))
        if last_seen and last_seen < cutoff:
            del state["tokens"][address]
            removed += 1
    return removed


def admit_to_buffer(entry: Dict[str, Any]) -> str:
    key = entry["watchlist_key"]
    with watchlist_lock():
        if key in current_known_keys():
            return "duplicate"
        buffer = load_json_dict(RANKING_BUFFER_FILE)
        buffer[key] = entry
        atomic_save_json(RANKING_BUFFER_FILE, buffer)
    return "created"


def profiles_to_process(
    profiles: List[Dict[str, Any]], config: Dict[str, Any], state: Dict[str, Any], known: set
) -> List[Dict[str, Any]]:
    chain = str(config.get("chain_id") or "solana")
    unique: Dict[str, Dict[str, Any]] = {}
    for profile in profiles:
        if str(profile.get("chainId") or "").lower() != chain:
            continue
        address = normalize_solana_address(profile.get("tokenAddress"))
        if not address:
            continue
        if config.get("require_pump_mint_suffix", True) and not address.lower().endswith("pump"):
            continue
        key = make_watchlist_key(chain, address)
        if key in known or (state["tokens"].get(address) or {}).get("status") == "emitted":
            continue
        normalized_profile = dict(profile)
        normalized_profile["tokenAddress"] = address
        unique.setdefault(address, normalized_profile)
    return list(unique.values())


def run_scanner_cycle(
    config: Dict[str, Any],
    provider=None,
    session: Optional[requests.Session] = None,
    dry_run: bool = False,
    current_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    current_time = current_time or utc_now()
    observed_at = to_iso(current_time)
    # Aceita tanto requests quanto uma Session injetada. Isso mantem o modulo
    # simples para testes e evita exigir estado de sessao para um unico ciclo.
    session = session or requests
    provider = provider or build_discovery_provider(config, session)
    state = load_state(config["state_file"])
    pruned = prune_state(state, current_time, float(config.get("emitted_retention_hours") or 24))
    known = current_known_keys()
    profiles = provider.latest_profiles()
    pending = profiles_to_process(profiles, config, state, known)
    addresses = [profile["tokenAddress"] for profile in pending]
    pairs = provider.pairs_for_tokens(str(config.get("chain_id") or "solana"), addresses) if addresses else []
    emitted = duplicates = waiting = jupiter_unavailable = 0

    for profile in pending:
        address = profile["tokenAddress"]
        pair = select_pumpswap_pair(pairs, address, config)
        state_item = state["tokens"].setdefault(address, {"first_seen_at_utc": observed_at})
        state_item["last_seen_at_utc"] = observed_at
        if pair is None:
            state_item["status"] = "waiting_pumpswap"
            waiting += 1
            continue

        jupiter = observe_jupiter(address, config, session, observed_at)
        if not jupiter["summary"].get("available"):
            jupiter_unavailable += 1
        entry = build_buffer_entry(address, profile, pair, jupiter, config, observed_at)
        action = "dry_run" if dry_run else admit_to_buffer(entry)
        if action == "created":
            emitted += 1
        elif action == "duplicate":
            duplicates += 1
        state_item.update({
            "status": "emitted" if action in {"created", "duplicate"} else "dry_run",
            "emitted_at_utc": observed_at if action in {"created", "duplicate"} else None,
            "pool_address": pair.get("pairAddress"),
        })
        if not dry_run:
            audit_path = config["audit_dir"] / f"observations_{current_time:%Y-%m-%d}.jsonl"
            append_jsonl(audit_path, {
                "timestamp": observed_at,
                "scanner_version": SCANNER_VERSION,
                "action": action,
                "profile": profile,
                "selected_pair": pair,
                "jupiter": jupiter,
            })

    if not dry_run:
        atomic_save_json(config["state_file"], state)
    return {
        "profiles": len(profiles), "new_profiles": len(pending), "pairs": len(pairs),
        "emitted": emitted, "duplicates": duplicates, "waiting_pumpswap": waiting,
        "jupiter_unavailable": jupiter_unavailable, "state_pruned": pruned, "dry_run": dry_run,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Descobre tokens Solana graduados no PumpSwap.")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def run_token_scanner_solana(config_path: Path = CONFIG_FILE, dry_run: bool = False) -> Dict[str, Any]:
    load_dotenv(PROJECT_ROOT / ".env")
    config = load_config(config_path)
    if not config.get("enabled", True):
        print("Token Scanner Solana desabilitado.", flush=True)
        return {"disabled": True}
    print(f"=== KRPTO-V | Token Scanner Solana | {SCANNER_VERSION} ===", flush=True)
    summary = run_scanner_cycle(config, dry_run=dry_run)
    print(
        "Ciclo concluido | " + " | ".join(f"{key}={value}" for key, value in summary.items()),
        flush=True,
    )
    return summary


def main() -> None:
    args = parse_args()
    try:
        run_token_scanner_solana(args.config, args.dry_run)
    except Exception as error:
        print(f"[ERRO][TOKEN_SCANNER_SOLANA] {type(error).__name__}: {error}", flush=True)
        try:
            config = load_config(args.config)
            RuntimeOpsAlerter(config.get("_root_config") or {}).send(
                "token_scanner_solana", "token_scanner_solana", f"{type(error).__name__}: {error}"
            )
        except Exception as alert_error:
            print(f"[OPS_ALERT][ERRO] {alert_error}", flush=True)
        raise


if __name__ == "__main__":
    main()
