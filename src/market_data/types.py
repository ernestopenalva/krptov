from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class MarketContext:
    chain: str
    protocol: str
    token_address: str
    quote_address: Optional[str]
    pool_address: Optional[str]
    pool_id: Optional[str] = None
    pool_manager_address: Optional[str] = None
    symbol: str = "UNKNOWN"
    quote_symbol: Optional[str] = None
    stable_pool: Optional[bool] = None


@dataclass(frozen=True)
class LiquidityObservation:
    model: str
    status: str
    token_reserve: Optional[float] = None
    quote_reserve: Optional[float] = None
    active_liquidity: Optional[float] = None
    sqrt_price_x96: Optional[int] = None
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketTick:
    observed_at: str
    provider: str
    status: str
    reason: Optional[str]
    chain: str
    protocol: str
    pool_address: Optional[str]
    pool_id: Optional[str]
    token_address: str
    quote_address: Optional[str]
    price_quote: Optional[float]
    quote_usd: Optional[float]
    price_usd: Optional[float]
    block_number: Optional[int] = None
    slot: Optional[int] = None
    data_age_seconds: Optional[float] = None
    liquidity: Optional[LiquidityObservation] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MarketDataUnavailable(RuntimeError):
    pass
