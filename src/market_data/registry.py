from __future__ import annotations

import os
from typing import Any, Dict

from .evm_provider import EvmPoolProvider
from .quote_usd import QuoteUsdProvider
from .pumpswap_provider import PumpSwapProvider
from .types import MarketContext, MarketDataUnavailable
from .v4_provider import V4StateViewProvider


V2_PROTOCOL_MARKERS = ("v2", "sushiswap", "aerodrome")
V3_PROTOCOL_MARKERS = ("v3", "slipstream")


class ProviderRegistry:
    def __init__(self, config: Dict[str, Any]) -> None:
        market = config.get("market_data") or {}
        self.rpc_env = market.get("rpc_urls") or {}
        self.state_view_env = market.get("uniswap_v4_state_view_addresses") or {}
        self.quote = QuoteUsdProvider(os.getenv("ALCHEMY_API_KEY", ""),
                                      int(market.get("quote_usd_cache_seconds", 60)),
                                      int(market.get("quote_usd_max_staleness_seconds", 120)))
        self.providers: Dict[tuple[str, str], Any] = {}

    def _rpc_url(self, chain: str) -> str:
        env_name = self.rpc_env.get(chain)
        rpc_url = os.getenv(str(env_name or ""))
        if not rpc_url:
            wss = os.getenv({"ethereum": "ALCHEMY_ETH_WSS_URL", "base": "ALCHEMY_BASE_WSS_URL",
                             "bsc": "ALCHEMY_BNB_WSS_URL", "robinhood": "ALCHEMY_ROBINHOOD_WSS_URL"}.get(chain, ""), "")
            rpc_url = wss.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
        if not rpc_url: raise MarketDataUnavailable(f"RPC HTTP ausente para {chain}")
        return rpc_url

    def provider_for(self, signal: Dict[str, Any]):
        chain = str(signal.get("chain") or signal.get("chain_id") or "").lower()
        protocol = str(signal.get("source") or signal.get("dex_id") or signal.get("source_type") or "").lower()
        if chain == "solana":
            key = (chain, "pumpswap")
            self.providers.setdefault(key, PumpSwapProvider(self._rpc_url(chain), self.quote))
            return self.providers[key]
        if "v4" in protocol or signal.get("pool_id"):
            state_view = os.getenv(str(self.state_view_env.get(chain) or ""))
            if not state_view: raise MarketDataUnavailable(f"Uniswap V4 StateView ausente para {chain}")
            key = (chain, "v4")
            self.providers.setdefault(key, V4StateViewProvider(self._rpc_url(chain), state_view, self.quote))
            return self.providers[key]
        if "aerodrome" in protocol and signal.get("stable_pool") is True:
            model = "aerodrome_stable"
        else:
            model = "v3" if any(marker in protocol for marker in V3_PROTOCOL_MARKERS) else "v2"
        key = (chain, model)
        self.providers.setdefault(key, EvmPoolProvider(self._rpc_url(chain), self.quote, model=model))
        return self.providers[key]

    def context_for(self, signal: Dict[str, Any]) -> MarketContext:
        return MarketContext(chain=signal.get("chain") or signal.get("chain_id"),
                             protocol=signal.get("source") or signal.get("dex_id") or signal.get("source_type") or "unknown",
                             token_address=signal["token_address"], quote_address=signal.get("quote_token_address"),
                             pool_address=signal.get("pool_address") or signal.get("pair_address"),
                             pool_id=signal.get("pool_id"), pool_manager_address=signal.get("pool_manager_address"),
                             symbol=signal.get("symbol") or signal["token_address"][:8],
                             quote_symbol=signal.get("quote_token"), stable_pool=signal.get("stable_pool"))

    def tick(self, signal: Dict[str, Any]):
        return self.provider_for(signal).get_tick(self.context_for(signal))
