"""Multichain Position public module and in-process PositionSupervisor."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.market_data.registry import ProviderRegistry
from src.modules.monitor_watchlist import exclude_and_remove
from src.modules.runtime_ops import RuntimeOpsAlerter
from src.position.engine import PositionEngine, PositionEngineConfig, PositionState


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POSITION_DIR = PROJECT_ROOT / "data" / "position"
LIVE_DIR = POSITION_DIR / "live"
HISTORY_DIR = POSITION_DIR / "history"
TRADING_HISTORY_FILE = PROJECT_ROOT / "data" / "trading_history.jsonl"


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class PositionSupervisor:
    def __init__(self, config: Dict[str, Any], registry: Optional[ProviderRegistry] = None) -> None:
        self.raw_config = config.get("position") or {}
        self.enabled = bool(self.raw_config.get("enabled", True))
        if str(self.raw_config.get("mode", "paper")).lower() != "paper":
            raise ValueError("Position suporta somente mode=paper nesta versao")
        self.engine = PositionEngine(PositionEngineConfig.from_dict(self.raw_config))
        self.registry = registry or ProviderRegistry(config)
        self.ops_alerter = RuntimeOpsAlerter(config)
        self.poll_seconds = float(self.raw_config.get("poll_interval_seconds", 1))
        self.entry_tick_wait_seconds = float(self.raw_config.get("entry_tick_wait_seconds", 30))
        self.max_entry_divergence_pct = float(self.raw_config.get("max_entry_divergence_pct", 10))
        raw_capacity = self.raw_config.get("max_active_positions_by_chain") or {}
        self.max_active_positions_by_chain = {
            str(chain).strip().lower(): int(limit)
            for chain, limit in raw_capacity.items()
            if int(limit) > 0
        }
        self.tasks: Dict[str, asyncio.Task] = {}
        self.states: Dict[str, PositionState] = {}
        self.last_ticks: Dict[str, Dict[str, Any]] = {}
        self.capacity_rejections: set[str] = set()
        self.stop_event = asyncio.Event()

    def start_fresh_paper_session(self) -> int:
        """Forget persisted live positions; paper mode never reconnects them."""
        abandoned = 0
        if not LIVE_DIR.exists():
            return abandoned
        for path in LIVE_DIR.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {"unreadable_state_file": str(path)}
            _append_jsonl(TRADING_HISTORY_FILE, {
                "timestamp": _now(), "event": "position_abandoned_on_restart",
                "reason": "paper_mode_no_recovery", "final_pnl": None,
                "position": payload,
            })
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            abandoned += 1
        return abandoned

    async def open_position(self, signal: Dict[str, Any]) -> Optional[str]:
        if not self.enabled or self.stop_event.is_set(): return None
        if not self._has_capacity(signal, record_rejection=True):
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.entry_tick_wait_seconds
        tick = None
        while not self.stop_event.is_set() and loop.time() <= deadline:
            try:
                candidate_tick = await asyncio.to_thread(self.registry.tick, signal)
                if candidate_tick.status == "ok" and candidate_tick.price_usd and candidate_tick.price_quote:
                    tick = candidate_tick
                    break
                print(f"[POSITION][WARN] aguardando tick onchain confiavel: {candidate_tick.reason}", flush=True)
                await asyncio.to_thread(
                    self.ops_alerter.send,
                    f"entry-data:{signal['watchlist_key']}",
                    "position",
                    candidate_tick.reason or "tick onchain indisponivel",
                )
            except Exception as exc:
                print(f"[POSITION][ERRO] provider {signal['watchlist_key']} | {exc}", flush=True)
                await asyncio.to_thread(self.ops_alerter.send, f"entry:{signal['watchlist_key']}", "position", str(exc))
            await asyncio.sleep(min(self.poll_seconds, max(0, deadline - loop.time())))
        if tick is None:
            return None
        payload = tick.to_dict()
        signal_price = signal.get("signal_price_usd")
        divergence = abs((tick.price_usd / float(signal_price) - 1) * 100) if signal_price else 0
        if divergence > self.max_entry_divergence_pct:
            _append_jsonl(TRADING_HISTORY_FILE, {"timestamp": _now(), "event": "entry_rejected",
                          "reason": "max_entry_divergence_exceeded", "divergence_pct": divergence,
                          "limit_pct": self.max_entry_divergence_pct, "signal": signal, "tick": payload})
            print(f"[POSITION][WARN] entrada rejeitada por divergencia {divergence:.2f}%", flush=True)
            return None
        # Another monitor may have occupied the last slot while this entry tick
        # was being fetched. Recheck immediately before creating the position.
        if not self._has_capacity(signal, record_rejection=True):
            return None
        position_id = f"pos-{uuid.uuid4().hex[:12]}"
        state = self.engine.open(position_id=position_id, signal=signal, tick=payload)
        self.states[position_id] = state
        self.last_ticks[position_id] = payload
        self._persist_live(state)
        self.tasks[position_id] = asyncio.create_task(self._run(position_id), name=position_id)
        print(f"[POSITION][OPEN] {position_id} | {state.watchlist_key} | entry={state.entry_price_usd}", flush=True)
        return position_id

    def _has_capacity(self, signal: Dict[str, Any], record_rejection: bool = False) -> bool:
        chain = str(signal.get("chain") or signal.get("chain_id") or "").strip().lower()
        limit = self.max_active_positions_by_chain.get(chain)
        if not limit:
            return True
        active = sum(1 for state in self.states.values() if str(state.chain).lower() == chain)
        watchlist_key = str(signal.get("watchlist_key") or f"{chain}:unknown")
        if active < limit:
            self.capacity_rejections.discard(watchlist_key)
            return True
        if record_rejection and watchlist_key not in self.capacity_rejections:
            self.capacity_rejections.add(watchlist_key)
            _append_jsonl(TRADING_HISTORY_FILE, {
                "timestamp": _now(),
                "event": "entry_rejected",
                "reason": "position_capacity_reached",
                "chain": chain,
                "active_positions_chain": active,
                "limit": limit,
                "watchlist_key": watchlist_key,
                "signal": signal,
            })
            print(
                f"[POSITION][CAPACITY] entrada adiada {watchlist_key} | "
                f"chain={chain} | ativas={active}/{limit}",
                flush=True,
            )
        return False

    async def _run(self, position_id: str) -> None:
        state = self.states[position_id]
        try:
            while not self.stop_event.is_set():
                try:
                    tick = await asyncio.to_thread(self.registry.tick, state.source_signal)
                    payload = tick.to_dict()
                    self.last_ticks[position_id] = payload
                    # Invalid/unavailable on-chain prices never close a Position.
                    if tick.status == "ok" and tick.price_usd and tick.price_quote:
                        result = self.engine.update(state, payload)
                        self._persist_tick(state, result)
                        self._persist_live(state)
                        if result["closed"]:
                            self._close(state, result)
                            return
                    else:
                        self._persist_tick(state, {"closed": False, "exit_reason": None,
                                                   "market_data_unavailable": True, "tick": payload})
                        await asyncio.to_thread(
                            self.ops_alerter.send,
                            f"tick-data:{state.watchlist_key}",
                            "position",
                            tick.reason or "tick onchain indisponivel",
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"[POSITION][ERRO] {position_id} | {exc}", flush=True)
                    await asyncio.to_thread(self.ops_alerter.send, f"tick:{state.watchlist_key}", "position", str(exc))
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.tasks.pop(position_id, None)

    def _persist_live(self, state: PositionState) -> None:
        _atomic_json(LIVE_DIR / f"{state.position_id}.json", state.to_dict())

    def _persist_tick(self, state: PositionState, result: Dict[str, Any]) -> None:
        _append_jsonl(HISTORY_DIR / f"{state.position_id}.jsonl", {"timestamp": _now(), **result})

    def _close(self, state: PositionState, result: Dict[str, Any]) -> None:
        _append_jsonl(TRADING_HISTORY_FILE, {"timestamp": _now(), "event": "position_closed",
                      "position_id": state.position_id, "watchlist_key": state.watchlist_key,
                      "exit_reason": result["exit_reason"], "pnl_pct": result["pnl_pct"],
                      "position": state.to_dict(), "last_tick": result["tick"]})
        try: (LIVE_DIR / f"{state.position_id}.json").unlink()
        except FileNotFoundError: pass
        exclude_and_remove(
            state.watchlist_key,
            "position_closed",
            {"position_id": state.position_id, "exit_reason": result["exit_reason"]},
        )
        self.states.pop(state.position_id, None)
        self.last_ticks.pop(state.position_id, None)
        print(f"[POSITION][CLOSE] {state.position_id} | {result['exit_reason']} | pnl={result['pnl_pct']:.2f}%", flush=True)

    async def stop_now(self) -> None:
        self.stop_event.set()
        for task in list(self.tasks.values()): task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        for position_id, state in list(self.states.items()):
            _append_jsonl(TRADING_HISTORY_FILE, {"timestamp": _now(), "event": "position_aborted",
                          "reason": "stop_now", "position_id": position_id,
                          "watchlist_key": state.watchlist_key, "final_pnl": None})
            try: (LIVE_DIR / f"{position_id}.json").unlink()
            except FileNotFoundError: pass
        self.states.clear(); self.last_ticks.clear()

    def status(self) -> Dict[str, Any]:
        active_by_chain: Dict[str, int] = {}
        for state in self.states.values():
            chain = str(state.chain).lower()
            active_by_chain[chain] = active_by_chain.get(chain, 0) + 1
        return {"active_positions": len(self.states),
                "active_positions_by_chain": active_by_chain,
                "position_capacity_by_chain": self.max_active_positions_by_chain,
                "positions": [{"position_id": key, "watchlist_key": state.watchlist_key,
                               "symbol": state.symbol, "chain": state.chain,
                               "entry_price_usd": state.entry_price_usd,
                               "last_tick": self.last_ticks.get(key)} for key, state in self.states.items()]}
