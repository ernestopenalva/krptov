#!/usr/bin/env python3
"""Readable multichain report from KRPTO-V's canonical trading history."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HISTORY_FILE = PROJECT_ROOT / "data" / "trading_history.jsonl"


def safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def load_closed_positions(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict) or event.get("event") != "position_closed":
                continue
            position = event.get("position") or {}
            signal = position.get("source_signal") or {}
            tick = event.get("last_tick") or position.get("last_tick") or {}
            entry = safe_float(position.get("entry_price_usd"))
            minimum = safe_float(position.get("min_price_usd"))
            maximum = safe_float(position.get("highest_price_usd"))
            chain = str(position.get("chain") or signal.get("chain") or signal.get("chain_id") or "unknown").lower()
            symbol = position.get("symbol") or signal.get("token_symbol") or signal.get("symbol") or "-"
            quote = signal.get("quote_token") or tick.get("raw", {}).get("quote_symbol") or "-"
            yield {
                "entry_time": position.get("entry_time"),
                "entry_price_usd": entry,
                "exit_time": tick.get("observed_at") or event.get("timestamp"),
                "exit_price_usd": safe_float(tick.get("price_usd")),
                "pnl_pct": safe_float(event.get("pnl_pct")),
                "min_pnl_pct": ((minimum / entry) - 1) * 100 if entry and minimum is not None else None,
                "max_pnl_pct": ((maximum / entry) - 1) * 100 if entry and maximum is not None else None,
                "exit_reason": event.get("exit_reason") or "UNKNOWN",
                "entry_type": signal.get("entry_type") or signal.get("entry_reason") or "UNKNOWN",
                "token": f"{symbol}/{quote}",
                "token_address": position.get("token_address") or signal.get("token_address") or "-",
                "chain": chain,
                "social": signal.get("admission_source") == "social_alert" or signal.get("rank_bypass") is True,
            }


def parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_time(value: Any) -> str:
    parsed = parse_time(value)
    return parsed.strftime("%d/%m %H:%M:%S") if parsed else "-"


def fmt_pct(value: Optional[float]) -> str:
    return f"{value:+.2f}%" if value is not None else "-"


def fmt_price(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if value == 0 or abs(value) >= 1:
        return f"US${value:,.2f}"
    return f"US${value:.10g}"


def summary(rows):
    pnls = [row["pnl_pct"] for row in rows if row["pnl_pct"] is not None]
    return (
        f"quantidade={len(rows)} | pnl_total={fmt_pct(sum(pnls) if pnls else None)}"
        f" | pnl_medio={fmt_pct(mean(pnls) if pnls else None)}"
    )


def print_table(rows):
    headers = ["ENTRADA", "PRECO ENTRADA (ONCHAIN)", "SAIDA", "PRECO SAIDA (ONCHAIN)",
               "PNL", "PNL MIN", "PNL MAX", "EXIT", "SOCIAL", "TOKEN", "CHAIN", "CA"]
    rendered = [[fmt_time(row["entry_time"]), fmt_price(row["entry_price_usd"]),
                 fmt_time(row["exit_time"]), fmt_price(row["exit_price_usd"]),
                 fmt_pct(row["pnl_pct"]), fmt_pct(row["min_pnl_pct"]), fmt_pct(row["max_pnl_pct"]),
                 str(row["exit_reason"]), "SIM" if row["social"] else "NAO", row["token"],
                 row["chain"], str(row["token_address"])] for row in rows]
    widths = [len(header) for header in headers]
    for values in rendered:
        for index, value in enumerate(values):
            widths[index] = max(widths[index], len(value))
    line = lambda values: " | ".join(value.ljust(widths[index]) for index, value in enumerate(values))
    print(line(headers))
    print("-+-".join("-" * width for width in widths))
    for values in rendered:
        print(line(values))


def main():
    parser = argparse.ArgumentParser(description="Lista Positions fechadas do KRPTO-V.")
    parser.add_argument("--file", type=Path, default=DEFAULT_HISTORY_FILE)
    parser.add_argument("--chain", help="Exibe somente uma chain (ethereum, base, bsc, robinhood ou solana).")
    parser.add_argument("--limit", type=int, default=0, help="0 mostra todos; valor positivo mostra os mais recentes.")
    args = parser.parse_args()
    rows = list(load_closed_positions(args.file) or [])
    if args.chain:
        rows = [row for row in rows if row["chain"] == args.chain.lower()]
    rows.sort(
        key=lambda row: (parse_time(row["exit_time"]).timestamp() if parse_time(row["exit_time"]) else 0),
        reverse=True,
    )
    momentum = [row for row in rows if row["entry_type"] == "MOMENTUM_CONTINUATION"]
    pullback = [row for row in rows if row["entry_type"] == "PULLBACK_RECOVERY"]
    print("# Closed Position Report")
    print(f"fonte={args.file}")
    print(f"geral | {summary(rows)}")
    print(f"MC | {summary(momentum)}")
    print(f"Pullback | {summary(pullback)}")
    counts = Counter(row["chain"] for row in rows)
    for chain in sorted(counts):
        print(f"chain {chain} | {summary([row for row in rows if row['chain'] == chain])}")
    selected = rows[:args.limit] if args.limit > 0 else rows
    print(f"\n## Trades fechados ({len(selected)} mostrados)")
    if selected:
        print_table(selected)
    else:
        print("Nenhum trade fechado no filtro selecionado.")


if __name__ == "__main__":
    main()
