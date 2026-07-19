from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict

import requests

from .types import MarketContext, MarketDataUnavailable


STABLE_SYMBOLS = {"USDC", "USDT", "DAI", "USDS", "USDBC", "USDG"}


class QuoteUsdProvider:
    def __init__(self, api_key: str, cache_seconds: int = 60, max_staleness_seconds: int = 120) -> None:
        self.api_key = api_key
        self.cache_seconds = cache_seconds
        self.max_staleness_seconds = max_staleness_seconds
        self.cache: Dict[str, tuple[float, Dict[str, Any]]] = {}

    def __call__(self, context: MarketContext) -> Dict[str, Any]:
        symbol = str(context.quote_symbol or "").upper()
        if symbol in STABLE_SYMBOLS:
            return {"value": 1.0, "source": "configured_usd_stablecoin", "age_seconds": 0.0}
        if not symbol: raise MarketDataUnavailable("missing_quote_symbol")
        cached = self.cache.get(symbol)
        if cached and time.monotonic() - cached[0] < self.cache_seconds: return cached[1]
        if not self.api_key: raise MarketDataUnavailable("ALCHEMY_API_KEY missing for quote/USD")
        response = requests.get("https://api.g.alchemy.com/prices/v1/tokens/by-symbol",
                                params=[("symbols", symbol)], headers={"Authorization": f"Bearer {self.api_key}"}, timeout=10)
        response.raise_for_status()
        rows = (response.json() or {}).get("data") or []
        prices = (rows[0] if rows else {}).get("prices") or []
        usd = next((row for row in prices if str(row.get("currency")).upper() == "USD"), None)
        if not usd: raise MarketDataUnavailable(f"missing {symbol}/USD")
        updated = datetime.fromisoformat(str(usd["lastUpdatedAt"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        age = max(0.0, (datetime.now(timezone.utc) - updated).total_seconds())
        if age > self.max_staleness_seconds: raise MarketDataUnavailable(f"stale {symbol}/USD: {age:.1f}s")
        result = {"value": float(usd["value"]), "source": "alchemy_prices", "age_seconds": age,
                  "last_updated_at": usd["lastUpdatedAt"]}
        self.cache[symbol] = (time.monotonic(), result)
        return result
