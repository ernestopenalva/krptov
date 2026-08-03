"""Singleton Scheduler for Monitor workers and PositionSupervisor."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv

from src.modules.monitor import MonitorConfig, monitor_token
from src.modules.monitor_watchlist import (
    finish_monitor_attempt,
    load_monitor_watchlist,
    mutate_entry,
    pop_completed_entries,
    reserve_next_candidate,
    reset_stale_live_states,
)
from src.modules.position import PositionSupervisor
from src.modules.runtime_ops import RuntimeOpsAlerter


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"
RUNTIME_DIR = PROJECT_ROOT / "data" / "trading_runtime"
CONTROL_FILE = RUNTIME_DIR / "control.json"
STATUS_FILE = RUNTIME_DIR / "status.json"
SCHEDULER_LOCK_FILE = RUNTIME_DIR / "scheduler.lock"
HISTORY_FILE = PROJECT_ROOT / "data" / "trading_history.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def _append_history(payload: Dict[str, Any]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": _now(), **payload}, ensure_ascii=False) + "\n")


def load_config(path: Path = CONFIG_FILE) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


class Scheduler:
    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or load_config()
        raw = self.config.get("monitor") or {}
        self.enabled = bool(raw.get("enabled", True))
        scheduler = raw.get("scheduler") or {}
        self.monitor_cfg = MonitorConfig.from_dict(raw)
        self.max_active = int(scheduler.get("max_active_monitors", 5))
        self.max_social = int(scheduler.get("max_social_alert_monitors", 2))
        self.loop_interval = float(scheduler.get("loop_interval_seconds", 1))
        self.max_attempts = int(raw.get("max_attempts", 3))
        self.cooldown_minutes = float(raw.get("cooldown_minutes", 15))
        self.session_id = f"runtime-{uuid.uuid4().hex[:12]}"
        self.monitors: Dict[str, asyncio.Task] = {}
        self.monitor_stop_events: Dict[str, asyncio.Event] = {}
        self.last_monitor_ticks: Dict[str, Dict[str, Any]] = {}
        self.position = PositionSupervisor(self.config)
        self.ops_alerter = RuntimeOpsAlerter(self.config)
        self.mode = "running"
        self.started_at = _now()

    async def run(self) -> None:
        if not self.enabled:
            raise RuntimeError("Monitor esta desabilitado em config/config.yaml")
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        try:
            lock_descriptor = os.open(SCHEDULER_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError("Ja existe um Scheduler ativo (ou lock residual em data/trading_runtime)") from exc
        os.write(lock_descriptor, f"{os.getpid()} {self.session_id}".encode("ascii"))
        try:
            reset_stale_live_states()
            abandoned = self.position.start_fresh_paper_session()
            _atomic_json(CONTROL_FILE, {"command": "run", "updated_at": _now()})
            print(f"[SCHEDULER] session={self.session_id} | max_monitors={self.max_active} | social={self.max_social}", flush=True)
            if abandoned:
                print(f"[POSITION] estados paper abandonados no restart: {abandoned}", flush=True)
            while True:
                await self._read_control()
                self._reap_monitors()
                self._archive_completed()
                if self.mode == "stop_now":
                    await self._stop_everything()
                    break
                if self.mode == "running":
                    await self._fill_slots()
                self._write_status()
                if self.mode == "draining" and not self.monitors and not self.position.states:
                    break
                await asyncio.sleep(self.loop_interval)
        finally:
            self.mode = "stopped"
            self._write_status()
            os.close(lock_descriptor)
            try: SCHEDULER_LOCK_FILE.unlink()
            except FileNotFoundError: pass
            print("[SCHEDULER] encerrado com seguranca", flush=True)

    async def _fill_slots(self) -> None:
        while len(self.monitors) < self.max_active:
            active_social = sum(
                1 for key in self.monitors
                if (load_monitor_watchlist().get(key) or {}).get("rank_bypass") is True
            )
            reserved = reserve_next_candidate(active_keys=set(self.monitors), active_social=active_social,
                                               max_social=self.max_social, max_attempts=self.max_attempts)
            if not reserved: return
            key, candidate = reserved
            if not candidate.get("pair_address") and not candidate.get("pool_address"):
                mutate_entry(
                    key,
                    {
                        "monitor_status": "completed",
                        "monitor_finished_at_utc": _now(),
                        "monitor_last_reason": "missing_pool_address",
                    },
                )
                print(
                    f"[SCHEDULER][SKIP] ignorado {key} | motivo=missing_pool_address",
                    flush=True,
                )
                continue
            attempt_id = f"mon-{uuid.uuid4().hex[:12]}"
            candidate["monitor_attempt_id"] = attempt_id
            stop_event = asyncio.Event()
            self.monitor_stop_events[key] = stop_event
            mutate_entry(key, {"monitor_status": "monitoring", "monitor_session_id": self.session_id,
                               "monitor_attempt_id": attempt_id})
            self.monitors[key] = asyncio.create_task(self._run_monitor(key, candidate, stop_event),
                                                     name=f"monitor:{key}")
            print(f"[SCHEDULER][MONITOR] iniciou {key} | origem={candidate.get('admission_source')}", flush=True)
            await asyncio.sleep(0)

    async def _run_monitor(self, key: str, candidate: Dict[str, Any], stop_event: asyncio.Event) -> None:
        result = await monitor_token(candidate, self.monitor_cfg, self.position.open_position, stop_event,
                                     on_error=self._monitor_error, on_tick=self._monitor_tick)
        entry = finish_monitor_attempt(key, result, max_attempts=self.max_attempts,
                                       cooldown_minutes=self.cooldown_minutes)
        _append_history({"event": "monitor_finished", "watchlist_key": key, "result": result,
                         "attempts": (entry or {}).get("monitor_attempts"),
                         "next_status": (entry or {}).get("monitor_status")})
        print(f"[SCHEDULER][MONITOR] terminou {key} | {result['outcome']} | {result['reason']}", flush=True)

    def _reap_monitors(self) -> None:
        for key, task in list(self.monitors.items()):
            if not task.done(): continue
            self.monitors.pop(key, None); self.monitor_stop_events.pop(key, None)
            self.last_monitor_ticks.pop(key, None)
            if not task.cancelled() and task.exception():
                print(f"[SCHEDULER][ERRO] monitor {key}: {task.exception()}", flush=True)

    async def _monitor_error(self, watchlist_key: str, detail: str) -> None:
        await asyncio.to_thread(self.ops_alerter.send, f"monitor:{watchlist_key}", "monitor", detail)

    async def _monitor_tick(self, watchlist_key: str, tick: Dict[str, Any]) -> None:
        self.last_monitor_ticks[watchlist_key] = tick
        await asyncio.to_thread(
            mutate_entry,
            watchlist_key,
            {
                "monitor_last_tick_at_utc": tick.get("timestamp"),
                "campaign_first_price_usd": tick.get("campaign_first_price_usd"),
                "campaign_first_price_at_utc": tick.get("campaign_first_price_at_utc"),
                "campaign_peak_price_usd": tick.get("campaign_peak_price_usd"),
                "campaign_peak_price_at_utc": tick.get("campaign_peak_price_at_utc"),
            },
        )

    def _archive_completed(self) -> None:
        for key, entry in pop_completed_entries():
            _append_history({"event": "monitor_campaign_completed", "watchlist_key": key,
                             "attempts": entry.get("monitor_attempts"),
                             "reason": entry.get("monitor_last_reason"),
                             "admission_source": entry.get("admission_source")})

    async def _read_control(self) -> None:
        if not CONTROL_FILE.exists(): return
        try:
            command = json.loads(CONTROL_FILE.read_text(encoding="utf-8")).get("command")
        except (OSError, ValueError): return
        if command == "drain" and self.mode == "running":
            self.mode = "draining"
            print("[SCHEDULER] drain: nenhuma nova instancia sera iniciada", flush=True)
        elif command == "stop_now":
            self.mode = "stop_now"
            print("[SCHEDULER] stop_now recebido", flush=True)

    async def _stop_everything(self) -> None:
        for event in self.monitor_stop_events.values(): event.set()
        for task in self.monitors.values(): task.cancel()
        await asyncio.gather(*list(self.monitors.values()), return_exceptions=True)
        self.monitors.clear(); self.monitor_stop_events.clear()
        await self.position.stop_now()

    def _write_status(self) -> None:
        watchlist = load_monitor_watchlist()
        social_fifo = sorted(
            [{"watchlist_key": key, "ready_at": entry.get("social_ready_at_utc")}
             for key, entry in watchlist.items() if isinstance(entry, dict)
             and entry.get("rank_bypass") is True and entry.get("monitor_status") in {"eligible", "cooldown"}],
            key=lambda row: row["ready_at"] or "",
        )
        cooldowns = [{"watchlist_key": key, "until": entry.get("monitor_cooldown_until_utc"),
                      "attempts": entry.get("monitor_attempts")}
                     for key, entry in watchlist.items() if isinstance(entry, dict)
                     and entry.get("monitor_status") == "cooldown"]
        monitors = [{"watchlist_key": key, "symbol": (watchlist.get(key) or {}).get("symbol"),
                     "chain": (watchlist.get(key) or {}).get("chain"),
                     "source": (watchlist.get(key) or {}).get("admission_source"),
                     "last_tick": self.last_monitor_ticks.get(key)}
                    for key in self.monitors]
        _atomic_json(STATUS_FILE, {"updated_at": _now(), "session_id": self.session_id,
                                  "started_at": self.started_at, "mode": self.mode,
                                  "monitor_capacity": self.max_active,
                                  "active_monitors": len(self.monitors), "monitors": monitors,
                                  "social_fifo": social_fifo, "cooldowns": cooldowns,
                                  **self.position.status()})


def issue_command(command: str) -> None:
    _atomic_json(CONTROL_FILE, {"command": command, "updated_at": _now()})
    print(f"Comando registrado: {command}")


def print_status() -> None:
    if not STATUS_FILE.exists():
        print("Runtime sem status: o Scheduler ainda nao foi iniciado.")
        return
    print(json.dumps(json.loads(STATUS_FILE.read_text(encoding="utf-8")), ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="KRPTO-V Monitor/Position Scheduler")
    parser.add_argument("command", choices=("run", "drain", "stop_now", "status"), nargs="?", default="run")
    args = parser.parse_args()
    if args.command == "run":
        load_dotenv(PROJECT_ROOT / ".env")
        asyncio.run(Scheduler().run())
    elif args.command == "status": print_status()
    else: issue_command(args.command)


if __name__ == "__main__":
    main()
