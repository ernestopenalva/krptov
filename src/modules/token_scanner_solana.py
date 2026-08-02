"""Discover graduated Pump.fun tokens on PumpSwap and feed the market ranker."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
import websockets
import yaml
from dotenv import load_dotenv

from src.modules.chain_identity import make_watchlist_key, normalize_solana_address
from src.modules.runtime_ops import RuntimeOpsAlerter


SCANNER_VERSION = "krptov-token-scanner-solana-v1"
DEXSCREENER_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/tokens/v1/{chain}/{addresses}"
PUMPPORTAL_DATA_URL = "wss://pumpportal.fun/api/data"

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
    config["pumpportal_index_file"] = configured_path(
        scanner.get("pumpportal_index_file"), DATA_DIR / "token_scanner_solana" / "pumpportal_migration_index.json"
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


def build_dexscreener_provider(config: Dict[str, Any], session: requests.Session):
    """The Dex provider is also used by the observer, regardless of primary source."""
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


def optional_number(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def technical_eligibility(
    pair: Dict[str, Any], config: Dict[str, Any], observed_at: str
) -> Dict[str, Any]:
    filters = config.get("technical_entry_filters") or {}
    changes = pair.get("priceChange") or {}
    m5 = optional_number(changes.get("m5"))
    h1 = optional_number(changes.get("h1"))
    max_m5 = optional_number(filters.get("max_price_change_m5"))
    max_h1 = optional_number(filters.get("max_price_change_h1"))
    reasons = []
    if m5 is not None and max_m5 is not None and m5 > max_m5:
        reasons.append("price_change_m5_above_max")
    if h1 is not None and max_h1 is not None and h1 > max_h1:
        reasons.append("price_change_h1_above_max")
    return {
        "technical_eligibility": "blocked_exhaustion" if reasons else "eligible",
        "technical_eligibility_reason": ",".join(reasons) if reasons else "initial_market_snapshot_ok",
        "technical_eligibility_updated_at_utc": observed_at,
        "technical_filter_snapshot": {
            "price_change_m5": m5,
            "price_change_h1": h1,
            "max_price_change_m5": max_m5,
            "max_price_change_h1": max_h1,
        },
    }


def technical_not_evaluated(observed_at: str) -> Dict[str, Any]:
    """Do not turn missing early-source market history into a synthetic approval."""
    return {
        "technical_eligibility": "not_evaluated_early_source",
        "technical_eligibility_reason": "pumpportal_migration_has_no_m5_h1_snapshot",
        "technical_eligibility_updated_at_utc": observed_at,
        "technical_filter_snapshot": {
            "price_change_m5": None,
            "price_change_h1": None,
            "max_price_change_m5": None,
            "max_price_change_h1": None,
        },
    }


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
    technical = technical_eligibility(pair, config, observed_at)
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
        **technical,
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


def pumpportal_value(event: Dict[str, Any], *names: str) -> Any:
    for name in names:
        value = event.get(name)
        if value not in (None, ""):
            return value
    return None


def allowed_pumpportal_migration(event: Dict[str, Any]) -> bool:
    """Keep the Solana universe to PumpSwap even if the provider expands migrations."""
    venue = str(pumpportal_value(event, "pool", "poolType", "dex", "dexId") or "").lower()
    return "raydium" not in venue and "bonk" not in venue


def build_pumpportal_buffer_entry(
    event: Dict[str, Any], jupiter: Dict[str, Any], config: Dict[str, Any], observed_at: str
) -> Dict[str, Any]:
    """Normalize a migration event without inventing Dexscreener market history."""
    token_address = normalize_solana_address(pumpportal_value(event, "mint", "tokenAddress", "token_address"))
    if not token_address:
        raise ValueError("PumpPortal migration sem mint")
    chain = str(config.get("chain_id") or "solana")
    pool_address = normalize_solana_address(pumpportal_value(event, "pool", "poolAddress", "pairAddress"))
    quote = normalize_solana_address(pumpportal_value(event, "quoteMint", "quote_mint"))
    return {
        "watchlist_key": make_watchlist_key(chain, token_address),
        "chain": chain,
        "chain_id": chain,
        "token_address": token_address,
        "token_symbol": pumpportal_value(event, "symbol"),
        "token_name": pumpportal_value(event, "name"),
        "pool_address": pool_address,
        "pair_address": pool_address,
        "base_mint": token_address,
        "quote_mint": quote or config.get("quote_token_address"),
        "quote_token": config.get("quote_token") or "SOL",
        "quote_token_address": config.get("quote_token_address"),
        "source": "pumpswap",
        "source_type": "token_discovered",
        "discovery_provider": "pumpportal",
        "discovery_event_type": "migration",
        "discovery_event_signature": pumpportal_value(event, "signature", "txSignature", "transactionSignature"),
        "discovery_event_received_at_utc": observed_at,
        "dex_id": "pumpswap",
        "discovered_at_utc": observed_at,
        "created_at_utc": observed_at,
        "last_seen_at_utc": observed_at,
        "times_seen": 1,
        "status": "novo", "social_status": "pendente", "monitor_status": "pendente",
        "telegram_alert_sent": False, "discarded_reason": None,
        "scanner_validation_status": "approved",
        "scanner_validation_reason": "pumpportal_migration",
        **technical_not_evaluated(observed_at),
        "ranking_status": "pending_dexscreener",
        "ranking_first_seen_at_utc": observed_at,
        "ranking_last_seen_at_utc": observed_at,
        "ranking_attempts": 0,
        "discovery_profile": {"provider_event": event},
        "discovery_market_snapshot": {},
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
    emitted = duplicates = waiting = jupiter_unavailable = technical_social_only = 0

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
        if entry.get("technical_eligibility") == "blocked_exhaustion":
            technical_social_only += 1
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
                "technical_eligibility": {
                    "status": entry.get("technical_eligibility"),
                    "reason": entry.get("technical_eligibility_reason"),
                    "snapshot": entry.get("technical_filter_snapshot"),
                },
            })

    if not dry_run:
        atomic_save_json(config["state_file"], state)
    return {
        "profiles": len(profiles), "new_profiles": len(pending), "pairs": len(pairs),
        "emitted": emitted, "duplicates": duplicates, "waiting_pumpswap": waiting,
        "technical_social_only": technical_social_only,
        "jupiter_unavailable": jupiter_unavailable, "state_pruned": pruned, "dry_run": dry_run,
    }


def process_pumpportal_migration(
    event: Dict[str, Any], config: Dict[str, Any], session: Optional[requests.Session] = None,
    dry_run: bool = False, current_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Admit one PumpPortal migration. This is the sole PP -> buffer boundary."""
    current_time = current_time or utc_now()
    observed_at = to_iso(current_time)
    if not allowed_pumpportal_migration(event):
        return {"action": "ignored_non_pumpswap_migration"}
    session = session or requests
    jupiter = observe_jupiter(
        normalize_solana_address(pumpportal_value(event, "mint", "tokenAddress", "token_address")) or "",
        config, session, observed_at,
    )
    entry = build_pumpportal_buffer_entry(event, jupiter, config, observed_at)
    action = "dry_run" if dry_run else admit_to_buffer(entry)
    if not dry_run:
        index_path = config.get("pumpportal_index_file") or config["audit_dir"] / "pumpportal_migration_index.json"
        index = load_json_dict(index_path)
        index.setdefault(entry["token_address"], {
            "first_received_at_utc": observed_at,
            "pool_address": entry.get("pool_address"),
            "signature": entry.get("discovery_event_signature"),
        })
        atomic_save_json(index_path, index)
        append_jsonl(config["audit_dir"] / f"pumpportal_migrations_{current_time:%Y-%m-%d}.jsonl", {
            "timestamp": observed_at, "scanner_version": SCANNER_VERSION,
            "action": action, "event": event, "watchlist_key": entry["watchlist_key"],
            "jupiter": jupiter,
        })
    return {"action": action, "watchlist_key": entry["watchlist_key"], "jupiter_available": jupiter["summary"].get("available")}


def run_dexscreener_observer_cycle(
    config: Dict[str, Any], provider=None, current_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Record what the legacy DS scanner could see; deliberately never writes the buffer."""
    current_time = current_time or utc_now()
    observed_at = to_iso(current_time)
    provider = provider or build_dexscreener_provider(config, requests)
    profiles = provider.latest_profiles()
    candidates = []
    chain = str(config.get("chain_id") or "solana")
    for profile in profiles:
        address = normalize_solana_address(profile.get("tokenAddress"))
        if str(profile.get("chainId") or "").lower() != chain or not address:
            continue
        if config.get("require_pump_mint_suffix", True) and not address.lower().endswith("pump"):
            continue
        item = dict(profile); item["tokenAddress"] = address
        candidates.append(item)
    pairs = provider.pairs_for_tokens(chain, [item["tokenAddress"] for item in candidates]) if candidates else []
    index_path = config.get("pumpportal_index_file") or config["audit_dir"] / "pumpportal_migration_index.json"
    pp_index = load_json_dict(index_path)
    observed = 0
    audit_path = config["audit_dir"] / f"dexscreener_observations_{current_time:%Y-%m-%d}.jsonl"
    for profile in candidates:
        address = profile["tokenAddress"]
        pair = select_pumpswap_pair(pairs, address, config)
        pp_observation = pp_index.get(address) or {}
        pp_at = parse_iso(pp_observation.get("first_received_at_utc"))
        lead_seconds = None if pp_at is None else round((current_time - pp_at).total_seconds(), 3)
        append_jsonl(audit_path, {
            "timestamp": observed_at, "scanner_version": SCANNER_VERSION,
            "observer_only": True, "discovery_provider": "dexscreener",
            "token_address": address, "watchlist_key": make_watchlist_key(chain, address),
            "profile": profile, "selected_pair": pair,
            "would_be_captured": pair is not None,
            "pumpportal_first_received_at_utc": pp_observation.get("first_received_at_utc"),
            "pumpportal_to_dexscreener_seconds": lead_seconds,
        })
        observed += 1
    return {"profiles": len(profiles), "candidates": len(candidates), "would_capture": sum(1 for p in candidates if select_pumpswap_pair(pairs, p["tokenAddress"], config)), "observed": observed}


def pumpportal_url(config: Dict[str, Any]) -> str:
    pp = config.get("pumpportal") or {}
    url = str(pp.get("data_url") or PUMPPORTAL_DATA_URL)
    key = os.getenv(str(pp.get("api_key_env") or "PUMPPORTAL_API_KEY"))
    if not key:
        raise ValueError(f"Defina a chave PumpPortal em {pp.get('api_key_env') or 'PUMPPORTAL_API_KEY'}.")
    return f"{url}?api-key={key}"


async def run_pumpportal_worker(config: Dict[str, Any], dry_run: bool = False) -> None:
    """Long-running PP migration worker; DS observation is non-blocking audit work."""
    pp = config.get("pumpportal") or {}
    reconnect_seconds = float(pp.get("reconnect_seconds") or 5)
    alert_after_seconds = float(pp.get("operational_alert_interval_seconds") or 300)
    observer_seconds = float((config.get("dexscreener_observer") or {}).get("interval_seconds") or 60)
    next_observer = 0.0
    last_alert_at = 0.0
    while True:
        try:
            async with websockets.connect(pumpportal_url(config), ping_interval=20, ping_timeout=20) as websocket:
                await websocket.send(json.dumps({"method": "subscribeMigration"}))
                print("[PUMPPORTAL] Assinado em subscribeMigration.", flush=True)
                while True:
                    now = time.monotonic()
                    if (config.get("dexscreener_observer") or {}).get("enabled", True) and now >= next_observer:
                        summary = await asyncio.to_thread(run_dexscreener_observer_cycle, config)
                        print(f"[DEXSCREENER_OBSERVER] {summary}", flush=True)
                        next_observer = now + observer_seconds
                    try:
                        payload = json.loads(await asyncio.wait_for(websocket.recv(), timeout=1))
                    except asyncio.TimeoutError:
                        continue
                    if not isinstance(payload, dict) or not pumpportal_value(payload, "mint", "tokenAddress", "token_address"):
                        continue
                    if not allowed_pumpportal_migration(payload):
                        print("[PUMPPORTAL] Migracao fora de PumpSwap ignorada.", flush=True)
                        continue
                    result = await asyncio.to_thread(process_pumpportal_migration, payload, config, None, dry_run)
                    print(f"[PUMPPORTAL] migration {result}", flush=True)
        except Exception as error:
            print(f"[PUMPPORTAL][ERRO] {type(error).__name__}: {error}; reconectando em {reconnect_seconds:g}s", flush=True)
            now = time.monotonic()
            if now - last_alert_at >= alert_after_seconds:
                last_alert_at = now
                await asyncio.to_thread(
                    RuntimeOpsAlerter(config.get("_root_config") or {}).send,
                    "token_scanner_solana", "pumpportal", f"{type(error).__name__}: {error}",
                )
            await asyncio.sleep(reconnect_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Descobre tokens Solana graduados no PumpSwap.")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-forever", action="store_true", help="Mantem o stream PumpPortal conectado.")
    return parser.parse_args()


def run_token_scanner_solana(
    config_path: Path = CONFIG_FILE, dry_run: bool = False, run_forever: bool = False,
) -> Dict[str, Any]:
    load_dotenv(PROJECT_ROOT / ".env")
    config = load_config(config_path)
    if not config.get("enabled", True):
        print("Token Scanner Solana desabilitado.", flush=True)
        return {"disabled": True}
    provider_name = str(config.get("discovery_provider") or "dexscreener").lower()
    print(f"=== KRPTO-V | Token Scanner Solana | {SCANNER_VERSION} | {provider_name} ===", flush=True)
    if provider_name == "pumpportal":
        if not run_forever:
            raise ValueError("PumpPortal exige --run-forever para nao perder eventos ao vivo.")
        asyncio.run(run_pumpportal_worker(config, dry_run=dry_run))
        return {"running": True, "provider": provider_name}
    summary = run_scanner_cycle(config, dry_run=dry_run)
    print(
        "Ciclo concluido | " + " | ".join(f"{key}={value}" for key, value in summary.items()),
        flush=True,
    )
    return summary


def main() -> None:
    args = parse_args()
    try:
        run_token_scanner_solana(args.config, args.dry_run, args.run_forever)
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
