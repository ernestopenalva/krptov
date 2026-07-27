#!/usr/bin/env python3
"""Replay PB Positions with a high-profit harvest regime."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRADING_HISTORY = PROJECT_ROOT / "data" / "trading_history.jsonl"
DEFAULT_POSITION_HISTORY_DIR = PROJECT_ROOT / "data" / "position" / "history"


def safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(row, dict):
                yield row


def load_pullbacks(path: Path) -> list[Dict[str, Any]]:
    rows = []
    for event in load_jsonl(path) or []:
        if event.get("event") != "position_closed":
            continue
        position = event.get("position") or {}
        signal = position.get("source_signal") or {}
        entry_reason = signal.get("entry_reason") or signal.get("entry_type")
        if entry_reason != "PULLBACK_RECOVERY":
            continue
        entry = safe_float(position.get("entry_price_usd"))
        maximum = safe_float(position.get("highest_price_usd"))
        if not entry:
            continue
        rows.append({
            "position_id": event.get("position_id") or position.get("position_id"),
            "token": (
                signal.get("token_symbol")
                or signal.get("token_name")
                or position.get("symbol")
                or "-"
            ),
            "token_address": position.get("token_address") or signal.get("token_address") or "-",
            "entry_time": position.get("entry_time"),
            "entry_price": entry,
            "actual_pnl_pct": safe_float(event.get("pnl_pct")),
            "actual_max_pnl_pct": ((maximum / entry) - 1) * 100 if maximum is not None else None,
            "actual_exit_reason": event.get("exit_reason") or "UNKNOWN",
        })
    return rows


def load_ticks(path: Path) -> list[Dict[str, Any]]:
    ticks = []
    for row in load_jsonl(path) or []:
        tick = row.get("tick") or {}
        price = safe_float(tick.get("price_usd"))
        observed_at = parse_time(tick.get("observed_at") or row.get("timestamp"))
        if price is None or price <= 0 or observed_at is None:
            continue
        ticks.append({
            "observed_at": observed_at,
            "observed_at_raw": tick.get("observed_at") or row.get("timestamp"),
            "price_usd": price,
        })
    ticks.sort(key=lambda tick: tick["observed_at"])
    return ticks


@dataclass(frozen=True)
class HarvestScenario:
    trigger_pct: float
    lock_pct: float
    trailing_gap_pct: float
    persistence_seconds: float

    @property
    def name(self) -> str:
        persistence = f"{self.persistence_seconds:g}"
        return (
            f"t{self.trigger_pct:g}_lock{self.lock_pct:g}_"
            f"gap{self.trailing_gap_pct:g}_p{persistence}s"
        )


def replay_harvest(
    entry_price: float,
    ticks: Iterable[Dict[str, Any]],
    scenario: HarvestScenario,
) -> Dict[str, Any]:
    active = False
    highest = entry_price
    condition_started_at: Optional[datetime] = None
    trigger_at = None
    trigger_price = None

    for tick in ticks:
        price = float(tick["price_usd"])
        observed_at = tick["observed_at"]
        highest = max(highest, price)
        max_pnl = (highest / entry_price - 1) * 100
        if not active and max_pnl >= scenario.trigger_pct:
            active = True
            trigger_at = tick["observed_at_raw"]
            trigger_price = price
        if not active:
            continue

        floor_price = entry_price * (1 + scenario.lock_pct / 100)
        trailing_price = highest * (1 - scenario.trailing_gap_pct / 100)
        threshold = max(floor_price, trailing_price)
        if price > threshold:
            condition_started_at = None
            continue

        condition_started_at = condition_started_at or observed_at
        elapsed = (observed_at - condition_started_at).total_seconds()
        if elapsed >= scenario.persistence_seconds:
            return {
                "triggered": True,
                "closed": True,
                "trigger_at": trigger_at,
                "trigger_price_usd": trigger_price,
                "exit_at": tick["observed_at_raw"],
                "exit_price_usd": price,
                "exit_pnl_pct": (price / entry_price - 1) * 100,
                "highest_price_usd": highest,
                "max_pnl_pct": max_pnl,
                "threshold_price_usd": threshold,
                "persistence_elapsed_seconds": elapsed,
            }

    return {
        "triggered": active,
        "closed": False,
        "trigger_at": trigger_at,
        "trigger_price_usd": trigger_price,
        "exit_at": None,
        "exit_price_usd": None,
        "exit_pnl_pct": None,
        "highest_price_usd": highest,
        "max_pnl_pct": (highest / entry_price - 1) * 100,
        "threshold_price_usd": None,
        "persistence_elapsed_seconds": None,
    }


def run_replays(
    positions: Iterable[Dict[str, Any]],
    history_dir: Path,
    scenarios: Iterable[HarvestScenario],
) -> list[Dict[str, Any]]:
    scenario_list = list(scenarios)
    results = []
    for position in positions:
        position_id = position.get("position_id")
        history_path = history_dir / f"{position_id}.jsonl"
        ticks = load_ticks(history_path) if position_id else []
        row = {
            **position,
            "history_path": history_path,
            "history_found": history_path.exists(),
            "ticks": len(ticks),
            "scenarios": {},
        }
        for scenario in scenario_list:
            row["scenarios"][scenario.name] = replay_harvest(
                position["entry_price"],
                ticks,
                scenario,
            )
        results.append(row)
    return results


def fmt_pct(value: Optional[float]) -> str:
    return f"{value:+.2f}%" if value is not None else "-"


def scenario_summary(rows: list[Dict[str, Any]], scenario: HarvestScenario) -> Dict[str, Any]:
    comparable = []
    fallback_actual = 0
    missing_target_history = 0
    for row in rows:
        actual = row["actual_pnl_pct"]
        if not row["history_found"] or not row["ticks"]:
            maximum = row.get("actual_max_pnl_pct")
            if maximum is not None and maximum >= scenario.trigger_pct:
                missing_target_history += 1
            continue
        replay = row["scenarios"][scenario.name]
        if actual is None:
            continue
        if replay["triggered"] and not replay["closed"]:
            fallback_actual += 1
        simulated = replay["exit_pnl_pct"] if replay["closed"] else actual
        comparable.append((actual, simulated))
    return {
        "positions": len(rows),
        "comparable": len(comparable),
        "triggered": sum(row["scenarios"][scenario.name]["triggered"] for row in rows),
        "closed": sum(row["scenarios"][scenario.name]["closed"] for row in rows),
        "fallback_actual": fallback_actual,
        "missing_target_history": missing_target_history,
        "actual_total": sum(actual for actual, _ in comparable) if comparable else None,
        "simulated_total": sum(simulated for _, simulated in comparable) if comparable else None,
        "delta": sum(simulated - actual for actual, simulated in comparable) if comparable else None,
        "improved": sum(simulated > actual + 1e-9 for actual, simulated in comparable),
        "worsened": sum(simulated < actual - 1e-9 for actual, simulated in comparable),
        "unchanged": sum(abs(simulated - actual) <= 1e-9 for actual, simulated in comparable),
    }


def print_report(
    rows: list[Dict[str, Any]],
    scenarios: list[HarvestScenario],
    *,
    summary_only: bool = False,
) -> None:
    print("# PB High-Profit Harvest Replay")
    print(
        f"positions={len(rows)} | historicos_encontrados={sum(row['history_found'] for row in rows)} | "
        f"sem_historico={sum(not row['history_found'] for row in rows)}"
    )
    for scenario in scenarios:
        totals = scenario_summary(rows, scenario)
        print(
            f"{scenario.name} | trigger={scenario.trigger_pct:g}% | lock={scenario.lock_pct:g}% | "
            f"gap={scenario.trailing_gap_pct:g}% | acionados={totals['triggered']} | "
            f"fechados_replay={totals['closed']} | comparaveis={totals['comparable']} | "
            f"pnl_atual={fmt_pct(totals['actual_total'])} | "
            f"pnl_simulado={fmt_pct(totals['simulated_total'])} | delta={fmt_pct(totals['delta'])} | "
            f"melhoraram={totals['improved']} | pioraram={totals['worsened']} | "
            f"iguais={totals['unchanged']}"
        )
        if totals["fallback_actual"]:
            print(
                f"  FALLBACK: {totals['fallback_actual']} Position(s) acionaram o modo, "
                "mas foram encerradas primeiro pela protecao atual."
            )
        if totals["missing_target_history"]:
            print(
                f"  AVISO: {totals['missing_target_history']} Position(s) atingiram o gatilho no resumo, "
                "mas nao possuem ticks para replay."
            )

    if summary_only:
        return

    print("\n## Positions que atingiram o gatilho")
    headers = ["TOKEN", "PNL ATUAL", "PNL MAX", *[scenario.name for scenario in scenarios], "CA", "POSITION"]
    rendered = []
    for row in rows:
        if not any(row["scenarios"][scenario.name]["triggered"] for scenario in scenarios):
            continue
        values = [
            str(row["token"]),
            fmt_pct(row["actual_pnl_pct"]),
            fmt_pct(row["actual_max_pnl_pct"]),
        ]
        for scenario in scenarios:
            replay = row["scenarios"][scenario.name]
            values.append(
                fmt_pct(replay["exit_pnl_pct"])
                if replay["closed"]
                else f"ATUAL {fmt_pct(row['actual_pnl_pct'])}"
            )
        values.extend([str(row["token_address"]), str(row["position_id"])])
        rendered.append(values)
    widths = [len(header) for header in headers]
    for values in rendered:
        for index, value in enumerate(values):
            widths[index] = max(widths[index], len(value))
    line = lambda values: " | ".join(value.ljust(widths[index]) for index, value in enumerate(values))
    print(line(headers))
    print("-+-".join("-" * width for width in widths))
    for values in rendered:
        print(line(values))
    if not rendered:
        print("Nenhuma Position com historico atingiu o gatilho.")


def parse_persistences(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def parse_numbers(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reproduz PBs com modo de colheita, sem alterar dados ou configuracao."
    )
    parser.add_argument("--trading-history", type=Path, default=DEFAULT_TRADING_HISTORY)
    parser.add_argument("--position-history-dir", type=Path, default=DEFAULT_POSITION_HISTORY_DIR)
    parser.add_argument("--trigger-pct", type=float, default=20)
    parser.add_argument("--lock-pct", type=float, default=15)
    parser.add_argument(
        "--trigger-sweep",
        help="Lista de gatilhos para comparar, por exemplo 10,15,20,25,30.",
    )
    parser.add_argument(
        "--lock-distance-pct",
        type=float,
        default=5,
        help="No sweep, define lock = trigger - esta distancia.",
    )
    parser.add_argument("--trailing-gap-pct", type=float, default=4)
    parser.add_argument("--persist-seconds", default="0,1,3")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()

    triggers = (
        parse_numbers(args.trigger_sweep)
        if args.trigger_sweep
        else [args.trigger_pct]
    )
    scenarios = [
        HarvestScenario(
            trigger_pct=trigger,
            lock_pct=(
                trigger - args.lock_distance_pct
                if args.trigger_sweep
                else args.lock_pct
            ),
            trailing_gap_pct=args.trailing_gap_pct,
            persistence_seconds=persistence,
        )
        for trigger in triggers
        for persistence in parse_persistences(args.persist_seconds)
    ]
    rows = run_replays(
        load_pullbacks(args.trading_history),
        args.position_history_dir,
        scenarios,
    )
    print_report(rows, scenarios, summary_only=args.summary_only)


if __name__ == "__main__":
    main()
