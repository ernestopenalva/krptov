"""Reserve/sqrtPrice based EVM providers; no off-chain price fallback."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests

from .types import LiquidityObservation, MarketContext, MarketDataUnavailable, MarketTick


SELECTOR_TOKEN0 = "0x0dfe1681"
SELECTOR_TOKEN1 = "0xd21220a7"
SELECTOR_DECIMALS = "0x313ce567"
SELECTOR_GET_RESERVES = "0x0902f1ac"
SELECTOR_SLOT0 = "0x3850c7bd"
SELECTOR_LIQUIDITY = "0x1a686502"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _word(data: str, index: int = 0) -> int:
    clean = data.removeprefix("0x")
    start = index * 64
    return int(clean[start:start + 64], 16)


class EvmRpcClient:
    def __init__(self, rpc_url: str, timeout_seconds: int = 15, session=requests) -> None:
        self.rpc_url = rpc_url
        self.timeout_seconds = timeout_seconds
        self.session = session
        self.request_id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self.request_id += 1
        response = self.session.post(self.rpc_url, json={"jsonrpc": "2.0", "id": self.request_id,
                                     "method": method, "params": params}, timeout=self.timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise MarketDataUnavailable(f"{method}: {payload['error']}")
        return payload.get("result")

    def eth_call(self, address: str, selector: str) -> str:
        return self.call("eth_call", [{"to": address, "data": selector}, "latest"])

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)


class EvmPoolProvider:
    def __init__(self, rpc_url: str, quote_usd, *, model: str, timeout_seconds: int = 15) -> None:
        self.rpc = EvmRpcClient(rpc_url, timeout_seconds)
        self.quote_usd = quote_usd
        self.model = model
        self.provider_name = f"evm_{model}"
        self._token_cache: Dict[str, Tuple[str, str, int, int]] = {}

    def _pool_tokens(self, pool: str) -> Tuple[str, str, int, int]:
        cached = self._token_cache.get(pool.lower())
        if cached: return cached
        token0 = "0x" + self.rpc.eth_call(pool, SELECTOR_TOKEN0)[-40:].lower()
        token1 = "0x" + self.rpc.eth_call(pool, SELECTOR_TOKEN1)[-40:].lower()
        dec0 = _word(self.rpc.eth_call(token0, SELECTOR_DECIMALS))
        dec1 = _word(self.rpc.eth_call(token1, SELECTOR_DECIMALS))
        self._token_cache[pool.lower()] = (token0, token1, dec0, dec1)
        return token0, token1, dec0, dec1

    def get_tick(self, context: MarketContext) -> MarketTick:
        try:
            if not context.pool_address: raise MarketDataUnavailable("missing_pool_address")
            token0, token1, dec0, dec1 = self._pool_tokens(context.pool_address)
            token = context.token_address.lower()
            if token not in {token0, token1}: raise MarketDataUnavailable("token_not_in_pool")
            if self.model in {"v2", "aerodrome_stable"}:
                raw = self.rpc.eth_call(context.pool_address, SELECTOR_GET_RESERVES)
                reserve0_raw, reserve1_raw = _word(raw, 0), _word(raw, 1)
                reserve0, reserve1 = reserve0_raw / 10**dec0, reserve1_raw / 10**dec1
                if reserve0 <= 0 or reserve1 <= 0: raise MarketDataUnavailable("non_positive_reserves")
                if token == token0:
                    price_quote, token_reserve, quote_reserve = reserve1 / reserve0, reserve0, reserve1
                else:
                    price_quote, token_reserve, quote_reserve = reserve0 / reserve1, reserve1, reserve0
                liquidity_model = "constant_product_reserves"
                if self.model == "aerodrome_stable":
                    # Marginal price of x^3*y + y^3*x = k. Values are already
                    # decimal-normalized human reserves, so this also handles
                    # mixed-decimal stable pairs without pretending x/y is spot.
                    x, y = token_reserve, quote_reserve
                    numerator = 3 * x * x * y + y * y * y
                    denominator = x * x * x + 3 * y * y * x
                    if denominator <= 0: raise MarketDataUnavailable("invalid_stable_curve_reserves")
                    price_quote = numerator / denominator
                    liquidity_model = "aerodrome_stable_curve_reserves"
                liquidity = LiquidityObservation(liquidity_model, "ok", token_reserve, quote_reserve,
                                                 detail={"reserve0_raw": str(reserve0_raw), "reserve1_raw": str(reserve1_raw)})
            else:
                raw = self.rpc.eth_call(context.pool_address, SELECTOR_SLOT0)
                sqrt_price = _word(raw, 0)
                ratio_token1_per_token0 = (sqrt_price * sqrt_price / 2**192) * (10 ** (dec0 - dec1))
                price_quote = ratio_token1_per_token0 if token == token0 else 1 / ratio_token1_per_token0
                active = _word(self.rpc.eth_call(context.pool_address, SELECTOR_LIQUIDITY))
                liquidity = LiquidityObservation("concentrated_liquidity", "ok", active_liquidity=float(active),
                                                 sqrt_price_x96=sqrt_price,
                                                 detail={"warning": "active_liquidity_is_not_quote_reserve"})
            quote = self.quote_usd(context)
            return MarketTick(_now(), self.provider_name, "ok", None, context.chain, context.protocol,
                              context.pool_address, context.pool_id, context.token_address, context.quote_address,
                              price_quote, quote["value"], price_quote * quote["value"],
                              block_number=self.rpc.block_number(), data_age_seconds=quote.get("age_seconds"),
                              liquidity=liquidity, raw={"quote_usd_source": quote["source"]})
        except Exception as exc:
            return MarketTick(_now(), self.provider_name, "unavailable", str(exc), context.chain, context.protocol,
                              context.pool_address, context.pool_id, context.token_address, context.quote_address,
                              None, None, None, liquidity=LiquidityObservation(self.model, "unavailable"))
