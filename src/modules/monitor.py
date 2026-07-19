"""One-token Monitor worker and the KRPTO3 entry strategy.

Every worker owns exactly one token.  Concurrency belongs to ``scheduler.py``;
this module intentionally has no global candidate selection and no process
spawning.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
HISTORY_DIR = PROJECT_ROOT / "data" / "monitor" / "history"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class MonitorConfig:
    poll_interval_seconds: float = 5
    max_monitoring_minutes: float = 15
    decision_window_minutes: float = 5
    pullback_min_ticks: int = 12
    min_pullback_pct: float = 2
    max_pullback_pct: float = 8
    max_drawdown_discard_pct: float = 10
    min_buy_pressure: float = 0.52
    min_volume_keep_ratio: float = 0.40
    health_min_score: float = 0.60
    health_min_volume_ratio: float = 0.35
    health_min_buy_pressure: float = 0.48
    health_max_liquidity_drop_pct: float = 35
    health_recent_ticks: int = 6
    pullback_recent_ticks: int = 24
    breakout_margin_pct: float = 0.2
    momentum_enabled: bool = True
    momentum_min_ticks: int = 3
    momentum_min_pct: float = 4
    momentum_max_runup_pct: float = 12
    momentum_max_pullback_pct: float = 3
    momentum_min_liquidity_growth_pct: float = 0
    momentum_max_liquidity_drop_pct: float = 5
    momentum_health_min_score: float = 0.60
    momentum_min_buy_pressure: float = 0.52
    momentum_block_if_price_falling: bool = True
    momentum_price_falling_window_ticks: int = 3

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "MonitorConfig":
        entry = raw.get("entry") or {}
        momentum = raw.get("momentum_entry") or {}
        return cls(
            poll_interval_seconds=float(raw.get("poll_interval_seconds", 5)),
            max_monitoring_minutes=float(raw.get("max_monitoring_minutes", 15)),
            decision_window_minutes=float(raw.get("decision_window_minutes", 5)),
            pullback_min_ticks=max(2, int(entry.get("min_ticks_before_decision", 12))),
            min_pullback_pct=float(entry.get("min_pullback_pct", 2)),
            max_pullback_pct=float(entry.get("max_pullback_pct", 8)),
            max_drawdown_discard_pct=float(entry.get("max_drawdown_discard_pct", 10)),
            min_buy_pressure=float(entry.get("min_buy_pressure", .52)),
            min_volume_keep_ratio=float(entry.get("min_volume_keep_ratio", .40)),
            health_min_score=float(entry.get("health_min_score", .60)),
            health_min_volume_ratio=float(entry.get("health_min_volume_ratio", .35)),
            health_min_buy_pressure=float(entry.get("health_min_buy_pressure", .48)),
            health_max_liquidity_drop_pct=float(entry.get("health_max_liquidity_drop_pct", 35)),
            health_recent_ticks=int(entry.get("health_recent_ticks", 6)),
            pullback_recent_ticks=int(entry.get("pullback_recent_ticks", 24)),
            breakout_margin_pct=float(entry.get("breakout_margin_pct", .2)),
            momentum_enabled=bool(momentum.get("enabled", True)),
            momentum_min_ticks=max(2, int(momentum.get("min_ticks_before_decision", 3))),
            momentum_min_pct=float(momentum.get("min_momentum_pct", 4)),
            momentum_max_runup_pct=float(momentum.get("max_runup_pct", 12)),
            momentum_max_pullback_pct=float(momentum.get("max_pullback_from_peak_pct", 3)),
            momentum_min_liquidity_growth_pct=float(momentum.get("min_liquidity_growth_pct", 0)),
            momentum_max_liquidity_drop_pct=float(momentum.get("max_liquidity_drop_pct", 5)),
            momentum_health_min_score=float(momentum.get("health_min_score", .60)),
            momentum_min_buy_pressure=float(momentum.get("min_buy_pressure", .52)),
            momentum_block_if_price_falling=bool(momentum.get("block_if_price_falling", True)),
            momentum_price_falling_window_ticks=int(momentum.get("price_falling_window_ticks", 3)),
        )


def build_tick(candidate: Dict[str, Any], pair: Dict[str, Any]) -> Dict[str, Any]:
    txns = (pair.get("txns") or {}).get("m5") or {}
    buys, sells = _integer(txns.get("buys")), _integer(txns.get("sells"))
    total = buys + sells
    return {
        "timestamp": utc_now_iso(),
        "watchlist_key": candidate.get("watchlist_key"),
        "monitor_attempt_id": candidate.get("monitor_attempt_id"),
        "token_address": candidate.get("token_address"),
        "symbol": candidate.get("symbol"),
        "chain_id": candidate.get("chain") or candidate.get("chain_id"),
        "pair_address": pair.get("pairAddress") or candidate.get("pool_address"),
        "dex_id": pair.get("dexId") or candidate.get("source"),
        "price_usd": _number(pair.get("priceUsd")),
        "price_native": _number(pair.get("priceNative")),
        "volume_m5": _number((pair.get("volume") or {}).get("m5")),
        "buys_m5": buys,
        "sells_m5": sells,
        "buy_pressure": buys / total if total else 0.0,
        "liquidity_usd": _number((pair.get("liquidity") or {}).get("usd")),
        "price_change_m5": _number((pair.get("priceChange") or {}).get("m5")),
    }


def _recent(history: List[Dict[str, Any]], cfg: MonitorConfig) -> List[Dict[str, Any]]:
    max_ticks = max(1, int(cfg.decision_window_minutes * 60 / cfg.poll_interval_seconds))
    return history[-max_ticks:]


def compute_health(
    history: List[Dict[str, Any]], cfg: MonitorConfig, required_ticks: Optional[int] = None
) -> Dict[str, Any]:
    window = _recent(history, cfg)
    required = required_ticks or cfg.pullback_min_ticks
    if len(window) < required:
        return {"score": 0.0, "alive": True, "reason": "historico insuficiente", "metrics": {}}

    recent = window[-cfg.health_recent_ticks:]
    recent_prices = [tick["price_usd"] for tick in recent if tick.get("price_usd", 0) > 0]
    volumes = [tick["volume_m5"] for tick in window if tick.get("volume_m5", 0) > 0]
    pressures = [tick.get("buy_pressure", 0) for tick in recent]
    current = window[-1]
    initial_liquidity = next(
        (tick["liquidity_usd"] for tick in window if tick.get("liquidity_usd", 0) > 0),
        current.get("liquidity_usd", 0),
    )
    average_volume = sum(volumes) / len(volumes) if volumes else 0
    volume_ratio = current.get("volume_m5", 0) / average_volume if average_volume else 0
    average_pressure = sum(pressures) / len(pressures) if pressures else 0
    liquidity_drop = (
        (initial_liquidity - current.get("liquidity_usd", 0)) / initial_liquidity * 100
        if initial_liquidity else 0
    )
    returns = [
        (recent_prices[index] / recent_prices[index - 1] - 1) * 100
        for index in range(1, len(recent_prices)) if recent_prices[index - 1] > 0
    ]
    bounce_ratio = sum(value > 0 for value in returns) / len(returns) if returns else 0
    current_price = current.get("price_usd", 0)
    recent_drop = ((max(recent_prices) - current_price) / max(recent_prices) * 100) if recent_prices else 0
    recent_range = ((max(recent_prices) - min(recent_prices)) / min(recent_prices) * 100) if recent_prices and min(recent_prices) else 0

    score, reasons = 0.0, []
    if volume_ratio >= cfg.health_min_volume_ratio:
        score += .25; reasons.append("volume vivo")
    else: reasons.append("volume fraco")
    if average_pressure >= cfg.health_min_buy_pressure:
        score += .25; reasons.append("buy pressure aceitavel")
    else: reasons.append("buy pressure fraca")
    if bounce_ratio >= .25:
        score += .25; reasons.append("recuperacao presente")
    elif bounce_ratio >= .10:
        score += .12; reasons.append("recuperacao fraca")
    else: reasons.append("sem recuperacao")
    if liquidity_drop <= cfg.health_max_liquidity_drop_pct:
        score += .15; reasons.append("liquidez preservada")
    else: reasons.append("liquidez deteriorada")
    if recent_range < .8 and volume_ratio < cfg.health_min_volume_ratio:
        score -= .10; reasons.append("range estreito + volume fraco")
    elif recent_range >= .8:
        score += .10; reasons.append("range recente vivo")
    score = max(0.0, min(1.0, score))
    hard = (
        liquidity_drop > cfg.health_max_liquidity_drop_pct
        or (volume_ratio < .20 and average_pressure < .45)
        or (bounce_ratio < .10 and recent_drop > 20)
    )
    metrics = {
        "health_score": score, "volume_ratio_vs_avg": volume_ratio,
        "avg_recent_buy_pressure": average_pressure, "recent_range_pct": recent_range,
        "bounce_ratio": bounce_ratio, "recent_drop_pct": recent_drop,
        "liquidity_drop_pct": liquidity_drop,
    }
    return {"score": score, "alive": score >= cfg.health_min_score and not hard,
            "hard_deterioration": hard, "reason": " | ".join(reasons), "metrics": metrics}


def evaluate_momentum(history: List[Dict[str, Any]], cfg: MonitorConfig) -> Dict[str, Any]:
    if not cfg.momentum_enabled or len(history) < cfg.momentum_min_ticks:
        return {"entry": False, "reason": "historico insuficiente para momentum"}
    prices = [tick["price_usd"] for tick in history if tick.get("price_usd", 0) > 0]
    if len(prices) < cfg.momentum_min_ticks:
        return {"entry": False, "reason": "precos insuficientes para momentum"}
    current, first, peak = history[-1], prices[0], max(prices)
    runup = (current["price_usd"] / first - 1) * 100
    pullback = (peak - current["price_usd"]) / peak * 100
    liquidities = [tick["liquidity_usd"] for tick in history if tick.get("liquidity_usd", 0) > 0]
    if not liquidities or current.get("liquidity_usd", 0) <= 0:
        return {"entry": False, "reason": "liquidez insuficiente para momentum"}
    growth = (current["liquidity_usd"] / liquidities[0] - 1) * 100
    drop = (max(liquidities) - current["liquidity_usd"]) / max(liquidities) * 100
    health = compute_health(history, cfg, cfg.momentum_min_ticks)
    recent_prices = prices[-max(1, cfg.momentum_price_falling_window_ticks):]
    falling = cfg.momentum_block_if_price_falling and current["price_usd"] < sum(recent_prices) / len(recent_prices)
    metrics = {"entry_reason": "MOMENTUM_CONTINUATION", "runup_since_first_tick_pct": runup,
               "pullback_from_peak_pct": pullback, "liquidity_growth_pct": growth,
               "liquidity_drop_pct": drop, "health_score": health["score"],
               "buy_pressure": current.get("buy_pressure", 0)}
    if runup < cfg.momentum_min_pct: return {"entry": False, "reason": "momentum insuficiente", "metrics": metrics}
    if pullback > cfg.momentum_max_pullback_pct: return {"entry": False, "reason": "momentum longe do topo", "metrics": metrics}
    if not (growth >= cfg.momentum_min_liquidity_growth_pct or drop <= cfg.momentum_max_liquidity_drop_pct):
        return {"entry": False, "reason": "liquidez drenando no momentum", "metrics": metrics}
    if health["score"] < cfg.momentum_health_min_score: return {"entry": False, "reason": "health insuficiente para momentum", "metrics": metrics}
    if current.get("buy_pressure", 0) < cfg.momentum_min_buy_pressure: return {"entry": False, "reason": "buy pressure insuficiente para momentum", "metrics": metrics}
    if falling: return {"entry": False, "reason": "preco caindo na janela de momentum", "metrics": metrics}
    if runup > cfg.momentum_max_runup_pct:
        return {"entry": False, "blocked": True, "block_reason": "MC_RUNUP_TOO_EXTENDED", "reason": "MC_RUNUP_TOO_EXTENDED", "metrics": metrics}
    return {"entry": True, "entry_reason": "MOMENTUM_CONTINUATION", "reason": "momentum_continuation", "metrics": metrics}


def evaluate_entry(history: List[Dict[str, Any]], cfg: MonitorConfig) -> Dict[str, Any]:
    if len(history) < cfg.pullback_min_ticks:
        momentum = evaluate_momentum(history, cfg)
        return momentum if momentum.get("entry") or momentum.get("blocked") else {"entry": False, "reason": "historico insuficiente"}
    window = _recent(history, cfg)
    prices = [tick["price_usd"] for tick in window if tick.get("price_usd", 0) > 0]
    volumes = [tick["volume_m5"] for tick in window if tick.get("volume_m5", 0) > 0]
    if len(prices) < cfg.pullback_min_ticks:
        return {"entry": False, "reason": "precos insuficientes"}
    current, previous = window[-1], window[-2]
    operational = prices[-cfg.pullback_recent_ticks:]
    top, bottom = max(operational), min(operational)
    pullback = (top - current["price_usd"]) / top * 100 if top else 0
    volume_ratio = current["volume_m5"] / max(volumes) if volumes and max(volumes) else 0
    if pullback > cfg.max_drawdown_discard_pct:
        health = compute_health(history, cfg)
        return {"entry": False, "discard": not health["alive"],
                "reason": f"queda forte: pullback={pullback:.2f}% | health={health['score']:.2f}", "metrics": health["metrics"]}
    if not cfg.min_pullback_pct <= pullback <= cfg.max_pullback_pct:
        momentum = evaluate_momentum(history, cfg)
        return momentum if momentum.get("entry") or momentum.get("blocked") else {"entry": False, "reason": f"pullback fora da faixa: {pullback:.2f}%"}
    if current["price_usd"] < previous["price_usd"] * .998: return {"entry": False, "reason": "preco ainda caindo"}
    if current["buy_pressure"] < cfg.min_buy_pressure: return {"entry": False, "reason": "pressao compradora fraca"}
    if volume_ratio < cfg.min_volume_keep_ratio: return {"entry": False, "reason": "volume minguando"}
    move = (top / bottom - 1) * 100 if bottom else 0
    if move > 30 and pullback >= 3: return {"entry": False, "reason": "cenario de exaustao"}
    recent = window[-3:]
    if sum(t.get("sells_m5", 0) for t in recent) > sum(t.get("buys_m5", 0) for t in recent):
        return {"entry": False, "reason": "vendas dominando"}
    previous_prices = [t["price_usd"] for t in window[-cfg.health_recent_ticks:-1] if t.get("price_usd", 0) > 0]
    required = max(previous_prices) * (1 + cfg.breakout_margin_pct / 100) if previous_prices else 0
    if required and current["price_usd"] < required:
        health = compute_health(history, cfg)
        return {"entry": False, "discard": not health["alive"], "reason": "confirmacao de rompimento ausente", "metrics": health["metrics"]}
    return {"entry": True, "entry_reason": "PULLBACK_RECOVERY", "reason": "pullback valido + cenario ok + confirmacao",
            "metrics": {"entry_reason": "PULLBACK_RECOVERY", "pullback_pct": pullback,
                        "move_from_bottom_pct": move, "buy_pressure": current["buy_pressure"],
                        "volume_ratio": volume_ratio}}


def _select_pair(candidate: Dict[str, Any], pairs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    expected = str(candidate.get("pool_address") or candidate.get("pair_address") or "").lower()
    if expected:
        exact = next((pair for pair in pairs if str(pair.get("pairAddress") or "").lower() == expected), None)
        if exact: return exact
    return max(pairs, key=lambda p: _number((p.get("liquidity") or {}).get("usd")), default=None)


def fetch_pair(candidate: Dict[str, Any], session=requests) -> Optional[Dict[str, Any]]:
    chain = candidate.get("chain") or candidate.get("chain_id")
    pair = candidate.get("pool_address") or candidate.get("pair_address")
    token = candidate.get("token_address")
    url = (f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair}" if pair
           else f"https://api.dexscreener.com/token-pairs/v1/{chain}/{token}")
    response = session.get(url, timeout=10)
    response.raise_for_status()
    payload = response.json()
    pairs = payload.get("pairs") if isinstance(payload, dict) else payload
    return _select_pair(candidate, pairs or [])


def _append_tick(watchlist_key: str, tick: Dict[str, Any]) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    safe_key = "".join(char if char.isalnum() or char in "-_" else "_" for char in watchlist_key)
    path = HISTORY_DIR / f"{safe_key}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(tick, ensure_ascii=False) + "\n")


async def monitor_token(
    candidate: Dict[str, Any],
    cfg: MonitorConfig,
    open_position: Callable[[Dict[str, Any]], Awaitable[Optional[str]]],
    stop_event: asyncio.Event,
    session=requests,
    on_error: Optional[Callable[[str, str], Awaitable[None]]] = None,
    on_tick: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
) -> Dict[str, Any]:
    history: List[Dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.max_monitoring_minutes * 60
    last_reason = "monitoring_started"
    while loop.time() < deadline and not stop_event.is_set():
        try:
            pair = await asyncio.to_thread(fetch_pair, candidate, session)
            if pair:
                tick = build_tick(candidate, pair)
                if tick["price_usd"] > 0:
                    history.append(tick)
                    await asyncio.to_thread(_append_tick, candidate["watchlist_key"], tick)
                    if on_tick:
                        await on_tick(candidate["watchlist_key"], tick)
                    evaluation = evaluate_entry(history, cfg)
                    last_reason = evaluation.get("reason", "no_signal")
                    if evaluation.get("entry"):
                        signal = {**candidate, "signal_at_utc": utc_now_iso(), "signal_price_usd": tick["price_usd"],
                                  "entry_reason": evaluation.get("entry_reason"), "signal_metrics": evaluation.get("metrics", {}),
                                  "signal_tick": tick}
                        position_id = await open_position(signal)
                        if position_id:
                            return {"outcome": "buy", "reason": last_reason, "position_id": position_id, "ticks": len(history)}
                    if evaluation.get("discard"):
                        return {"outcome": "no_buy", "reason": last_reason, "ticks": len(history)}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_reason = f"market_data_error: {exc}"
            print(f"[MONITOR][ERRO] {candidate['watchlist_key']} | {exc}", flush=True)
            if on_error:
                await on_error(candidate["watchlist_key"], str(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=cfg.poll_interval_seconds)
        except asyncio.TimeoutError:
            pass
    return {"outcome": "stopped" if stop_event.is_set() else "no_buy", "reason": last_reason if stop_event.is_set() else "monitor_timeout", "ticks": len(history)}
