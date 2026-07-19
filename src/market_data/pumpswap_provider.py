"""Port of the KRPTO3 v0.4.0 on-chain PumpSwap reserve provider."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

import requests

from .types import LiquidityObservation, MarketContext, MarketDataUnavailable, MarketTick


PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
BASE_MINT_OFFSET, QUOTE_MINT_OFFSET = 43, 75
BASE_VAULT_OFFSET, QUOTE_VAULT_OFFSET = 139, 171
PUBKEY_LENGTH = 32
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SolanaRpcClient:
    def __init__(self, rpc_url: str, timeout_seconds: int = 15) -> None:
        self.rpc_url, self.timeout, self.request_id = rpc_url, timeout_seconds, 0

    def call(self, method: str, params: list[Any]) -> Any:
        self.request_id += 1
        response = requests.post(self.rpc_url, json={"jsonrpc": "2.0", "id": self.request_id,
                                 "method": method, "params": params}, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"): raise MarketDataUnavailable(f"{method}: {payload['error']}")
        return payload.get("result")

    def account(self, address: str, encoding: str = "base64") -> Optional[Dict[str, Any]]:
        result = self.call("getAccountInfo", [address, {"encoding": encoding}])
        return result.get("value") if isinstance(result, dict) else None


def _b58(data: bytes) -> str:
    number, encoded = int.from_bytes(data, "big"), ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = BASE58[remainder] + encoded
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + encoded


class PumpSwapProvider:
    provider_name = "solana_pumpswap_onchain"

    def __init__(self, rpc_url: str, quote_usd, timeout_seconds: int = 15) -> None:
        self.rpc = SolanaRpcClient(rpc_url, timeout_seconds)
        self.quote_usd = quote_usd

    def _vault(self, address: str) -> tuple[str, Decimal, int]:
        info = self.rpc.account(address, "jsonParsed") or {}
        parsed = (((info.get("data") or {}).get("parsed") or {}).get("info") or {})
        mint = parsed.get("mint")
        balance = self.rpc.call("getTokenAccountBalance", [address])
        value = balance.get("value") if isinstance(balance, dict) else None
        if not mint or not value: raise MarketDataUnavailable("vault_balance_unavailable")
        decimals = int(value.get("decimals") or 0)
        amount = Decimal(str(value["amount"])) / (Decimal(10) ** decimals)
        return str(mint), amount, decimals

    def get_tick(self, context: MarketContext) -> MarketTick:
        try:
            if not context.pool_address: raise MarketDataUnavailable("missing_pool_address")
            info = self.rpc.account(context.pool_address) or {}
            raw_data = info.get("data") or []
            data = base64.b64decode(raw_data[0]) if isinstance(raw_data, list) and raw_data else b""
            if len(data) < QUOTE_VAULT_OFFSET + PUBKEY_LENGTH: raise MarketDataUnavailable("pool_layout_unavailable")
            if info.get("owner") != PUMPSWAP_PROGRAM_ID: raise MarketDataUnavailable("pool_owner_mismatch")
            base_mint = _b58(data[BASE_MINT_OFFSET:BASE_MINT_OFFSET + PUBKEY_LENGTH])
            quote_mint = _b58(data[QUOTE_MINT_OFFSET:QUOTE_MINT_OFFSET + PUBKEY_LENGTH])
            base_vault = _b58(data[BASE_VAULT_OFFSET:BASE_VAULT_OFFSET + PUBKEY_LENGTH])
            quote_vault = _b58(data[QUOTE_VAULT_OFFSET:QUOTE_VAULT_OFFSET + PUBKEY_LENGTH])
            base_vault_mint, base_amount, _ = self._vault(base_vault)
            quote_vault_mint, quote_amount, _ = self._vault(quote_vault)
            if base_vault_mint != base_mint or quote_vault_mint != quote_mint:
                raise MarketDataUnavailable("vault_mint_mismatch")
            if context.token_address == base_mint and context.quote_address == quote_mint:
                token_reserve, quote_reserve = base_amount, quote_amount
            elif context.token_address == quote_mint and context.quote_address == base_mint:
                token_reserve, quote_reserve = quote_amount, base_amount
            else: raise MarketDataUnavailable("pool_mint_mismatch")
            if token_reserve <= 0 or quote_reserve <= 0: raise MarketDataUnavailable("non_positive_reserves")
            price_quote = float(quote_reserve / token_reserve)
            quote_usd = self.quote_usd(context)
            slot = self.rpc.call("getSlot", [])
            observation = LiquidityObservation("pumpswap_reserves", "ok", float(token_reserve), float(quote_reserve),
                                               detail={"base_vault": base_vault, "quote_vault": quote_vault})
            return MarketTick(_now(), self.provider_name, "ok", None, context.chain, context.protocol,
                              context.pool_address, context.pool_id, context.token_address, context.quote_address,
                              price_quote, quote_usd["value"], price_quote * quote_usd["value"], slot=int(slot),
                              data_age_seconds=quote_usd.get("age_seconds"), liquidity=observation,
                              raw={"quote_usd_source": quote_usd["source"]})
        except Exception as exc:
            return MarketTick(_now(), self.provider_name, "unavailable", str(exc), context.chain, context.protocol,
                              context.pool_address, context.pool_id, context.token_address, context.quote_address,
                              None, None, None, liquidity=LiquidityObservation("pumpswap_reserves", "unavailable"))
