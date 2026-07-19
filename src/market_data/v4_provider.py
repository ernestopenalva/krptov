"""Uniswap V4 StateView provider keyed by the scanner's canonical pool_id."""

from __future__ import annotations

from .evm_provider import EvmRpcClient, SELECTOR_DECIMALS, _now, _word
from .types import LiquidityObservation, MarketContext, MarketDataUnavailable, MarketTick


GET_SLOT0 = "0xc815641c"
GET_LIQUIDITY = "0xfa6793d5"
NATIVE = "0x0000000000000000000000000000000000000000"


class V4StateViewProvider:
    provider_name = "evm_uniswap_v4_state_view"

    def __init__(self, rpc_url: str, state_view_address: str, quote_usd, timeout_seconds: int = 15) -> None:
        self.rpc = EvmRpcClient(rpc_url, timeout_seconds)
        self.state_view = state_view_address
        self.quote_usd = quote_usd

    def _decimals(self, address: str) -> int:
        return 18 if address.lower() == NATIVE else _word(self.rpc.eth_call(address, SELECTOR_DECIMALS))

    def get_tick(self, context: MarketContext) -> MarketTick:
        try:
            if not context.pool_id: raise MarketDataUnavailable("missing_pool_id")
            if not context.quote_address: raise MarketDataUnavailable("missing_quote_address")
            pool_id = context.pool_id.removeprefix("0x").zfill(64)
            slot0 = self.rpc.eth_call(self.state_view, GET_SLOT0 + pool_id)
            sqrt_price = _word(slot0, 0)
            liquidity_raw = _word(self.rpc.eth_call(self.state_view, GET_LIQUIDITY + pool_id), 0)
            token, quote = context.token_address.lower(), context.quote_address.lower()
            currency0, currency1 = sorted((token, quote), key=lambda value: int(value, 16))
            dec0, dec1 = self._decimals(currency0), self._decimals(currency1)
            ratio1_per_0 = (sqrt_price * sqrt_price / 2**192) * (10 ** (dec0 - dec1))
            price_quote = ratio1_per_0 if token == currency0 else 1 / ratio1_per_0
            quote_usd = self.quote_usd(context)
            observation = LiquidityObservation(
                "concentrated_liquidity_v4", "ok", active_liquidity=float(liquidity_raw),
                sqrt_price_x96=sqrt_price,
                detail={"state_view": self.state_view, "warning": "active_liquidity_is_not_quote_reserve"},
            )
            return MarketTick(_now(), self.provider_name, "ok", None, context.chain, context.protocol,
                              context.pool_address, context.pool_id, context.token_address, context.quote_address,
                              price_quote, quote_usd["value"], price_quote * quote_usd["value"],
                              block_number=self.rpc.block_number(), data_age_seconds=quote_usd.get("age_seconds"),
                              liquidity=observation, raw={"quote_usd_source": quote_usd["source"]})
        except Exception as exc:
            return MarketTick(_now(), self.provider_name, "unavailable", str(exc), context.chain, context.protocol,
                              context.pool_address, context.pool_id, context.token_address, context.quote_address,
                              None, None, None, liquidity=LiquidityObservation("concentrated_liquidity_v4", "unavailable"))
