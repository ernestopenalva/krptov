"""Pure KRPTO3 v0.4.0 ABB/S3 protection engine.

The engine knows prices and time, never chains or RPC protocols. This boundary
makes exactly the same stop, ladder, trailing and breathing rules apply to every
market-data provider.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, List, Optional, Tuple


def parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


@dataclass(frozen=True)
class PositionEngineConfig:
    stop_loss_pct: float = 5
    trailing_gap_pct: float = 4
    stop_persist_seconds: float = 3
    trailing_persist_seconds: float = 3
    profit_lock_steps: Tuple[Tuple[float, float], ...] = ((5, 1), (6, 3), (10, 5))
    breathing_enabled: bool = True
    breathing_candle_seconds: int = 3
    breathing_lookback_candles: int = 5
    breathing_band_fraction: float = .5
    breathing_band_min_pct: float = 2
    breathing_band_max_pct: float = 8

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "PositionEngineConfig":
        band = raw.get("adaptive_breathing_band") or {}
        steps = tuple(
            (float(row["trigger_pct"]), float(row["lock_pct"]))
            for row in raw.get("profit_lock_steps", []) if isinstance(row, dict)
        ) or cls.profit_lock_steps
        return cls(
            stop_loss_pct=float(raw.get("stop_loss_pct", 5)),
            trailing_gap_pct=float(raw.get("trailing_gap_pct", 4)),
            stop_persist_seconds=float(raw.get("stop_persist_seconds", 3)),
            trailing_persist_seconds=float(raw.get("trailing_persist_seconds", 3)),
            profit_lock_steps=steps if raw.get("profit_lock_enabled", True) else (),
            breathing_enabled=bool(band.get("enabled", True)),
            breathing_candle_seconds=int(band.get("candle_seconds", 3)),
            breathing_lookback_candles=int(band.get("lookback_candles", 5)),
            breathing_band_fraction=float(band.get("band_fraction", .5)),
            breathing_band_min_pct=float(band.get("band_min_pct", 2)),
            breathing_band_max_pct=float(band.get("band_max_pct", 8)),
        )


@dataclass
class PositionState:
    position_id: str
    watchlist_key: str
    chain: str
    protocol: str
    token_address: str
    quote_address: Optional[str]
    pool_address: Optional[str]
    symbol: str
    entry_time: str
    entry_price_usd: float
    entry_price_quote: float
    signal_price_usd: Optional[float]
    entry_divergence_pct: Optional[float]
    highest_price_usd: float
    min_price_usd: float
    stop_price: float
    trailing_stop_price: Optional[float] = None
    breakeven_activated: bool = False
    stop_condition_started_at: Optional[str] = None
    trailing_condition_started_at: Optional[str] = None
    recent_prices: List[Tuple[str, float]] = field(default_factory=list)
    ticks: int = 0
    source_signal: Dict[str, Any] = field(default_factory=dict)
    last_tick: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PositionEngine:
    def __init__(self, config: PositionEngineConfig) -> None:
        self.cfg = config

    def open(self, *, position_id: str, signal: Dict[str, Any], tick: Dict[str, Any]) -> PositionState:
        price = float(tick["price_usd"])
        quote_price = float(tick["price_quote"])
        signal_price = signal.get("signal_price_usd")
        divergence = ((price / float(signal_price)) - 1) * 100 if signal_price else None
        return PositionState(
            position_id=position_id, watchlist_key=signal["watchlist_key"],
            chain=signal.get("chain") or signal.get("chain_id"),
            protocol=signal.get("source") or signal.get("dex_id") or "unknown",
            token_address=signal["token_address"], quote_address=signal.get("quote_token_address"),
            pool_address=signal.get("pool_address") or signal.get("pair_address"),
            symbol=signal.get("symbol") or signal["token_address"][:8],
            entry_time=tick["observed_at"], entry_price_usd=price, entry_price_quote=quote_price,
            signal_price_usd=float(signal_price) if signal_price else None,
            entry_divergence_pct=divergence, highest_price_usd=price, min_price_usd=price,
            stop_price=price * (1 - self.cfg.stop_loss_pct / 100), source_signal=signal,
            last_tick=tick,
        )

    def _band(self, state: PositionState, observed_at: str, price: float) -> Dict[str, Any]:
        now = parse_time(observed_at) or datetime.now(timezone.utc)
        keep = self.cfg.breathing_candle_seconds * (self.cfg.breathing_lookback_candles + 2)
        cutoff = now - timedelta(seconds=keep)
        recent = [(parse_time(stamp), value) for stamp, value in state.recent_prices]
        recent = [(stamp, value) for stamp, value in recent if stamp and stamp >= cutoff and value > 0]
        recent.append((now, price))
        state.recent_prices = [(stamp.isoformat(timespec="seconds"), value) for stamp, value in recent]
        minimum = self.cfg.breathing_band_min_pct
        result = {"breathing_pct": None, "down_band_pct": minimum, "breathing_method": "fallback_min",
                  "candle_seconds": self.cfg.breathing_candle_seconds,
                  "lookback_candles": self.cfg.breathing_lookback_candles, "closed_candles": 0}
        if not self.cfg.breathing_enabled:
            result.update({"down_band_pct": 0.0, "breathing_method": "disabled"})
            return result
        current_bucket = int(now.timestamp() // self.cfg.breathing_candle_seconds)
        buckets: Dict[int, List[float]] = defaultdict(list)
        for stamp, value in recent:
            bucket = int(stamp.timestamp() // self.cfg.breathing_candle_seconds)
            if bucket < current_bucket: buckets[bucket].append(value)
        candles = [buckets[key] for key in sorted(buckets)[-self.cfg.breathing_lookback_candles:]]
        result["closed_candles"] = len(candles)
        if len(candles) < self.cfg.breathing_lookback_candles: return result
        ranges = [(max(values) - min(values)) / min(values) * 100 for values in candles if min(values) > 0]
        if len(ranges) < self.cfg.breathing_lookback_candles: return result
        breathing = median(ranges)
        band = max(minimum, min(self.cfg.breathing_band_max_pct, breathing * self.cfg.breathing_band_fraction))
        result.update({"breathing_pct": breathing, "down_band_pct": band, "breathing_method": "median_candle_range"})
        return result

    def update(self, state: PositionState, tick: Dict[str, Any]) -> Dict[str, Any]:
        price = float(tick["price_usd"])
        now = tick["observed_at"]
        state.ticks += 1
        state.last_tick = tick
        state.highest_price_usd = max(state.highest_price_usd, price)
        state.min_price_usd = min(state.min_price_usd, price)
        pnl = (price / state.entry_price_usd - 1) * 100
        for trigger, lock in self.cfg.profit_lock_steps:
            if pnl >= trigger:
                new_stop = state.entry_price_usd * (1 + lock / 100)
                if new_stop > state.stop_price:
                    state.stop_price = new_stop
                    state.breakeven_activated = True
        max_pnl = (state.highest_price_usd / state.entry_price_usd - 1) * 100
        if max_pnl >= self.cfg.trailing_gap_pct:
            state.trailing_stop_price = state.highest_price_usd * (1 - self.cfg.trailing_gap_pct / 100)
        band = self._band(state, now, price)

        if pnl <= -self.cfg.stop_loss_pct:
            state.stop_condition_started_at = state.stop_condition_started_at or now
            started = parse_time(state.stop_condition_started_at); current = parse_time(now)
            elapsed = (current - started).total_seconds() if started and current else 0
            if elapsed >= self.cfg.stop_persist_seconds:
                return self._result(state, tick, band, "STOP_LOSS", pnl, max_pnl)
        else:
            state.stop_condition_started_at = None

        if state.breakeven_activated and price <= state.stop_price:
            reason = "STOP_LOSS" if pnl <= -self.cfg.stop_loss_pct else "BREAKEVEN_STOP"
            return self._result(state, tick, band, reason, pnl, max_pnl)

        trailing = state.trailing_stop_price
        threshold = trailing * (1 - band["down_band_pct"] / 100) if trailing else None
        if threshold is None or price > threshold:
            state.trailing_condition_started_at = None
        else:
            state.trailing_condition_started_at = state.trailing_condition_started_at or now
            started = parse_time(state.trailing_condition_started_at); current = parse_time(now)
            elapsed = (current - started).total_seconds() if started and current else 0
            if elapsed >= self.cfg.trailing_persist_seconds:
                return self._result(state, tick, band, "TRAILING_STOP", pnl, max_pnl)
        return self._result(state, tick, band, None, pnl, max_pnl)

    @staticmethod
    def _result(state: PositionState, tick: Dict[str, Any], band: Dict[str, Any], reason: Optional[str], pnl: float, max_pnl: float) -> Dict[str, Any]:
        return {"closed": reason is not None, "exit_reason": reason, "pnl_pct": pnl,
                "max_pnl_pct": max_pnl, "band": band, "tick": tick, "position": state.to_dict()}
