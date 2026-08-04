#!/usr/bin/env python3
"""Counterfactual single-filter study for catastrophic closed positions."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Callable, Dict, Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.tools.closed_position_report import (
    DEFAULT_HISTORY_FILE,
    in_period,
    load_closed_positions,
    parse_boundary,
)


@dataclass(frozen=True)
class FilterCase:
    name: str
    field: str
    predicate: Callable[[float], bool]


def filter_cases() -> list[FilterCase]:
    cases: list[FilterCase] = []
    for value in (1, 100, 1_000, 5_000, 10_000, 25_000, 50_000):
        cases.append(FilterCase(f"QLiq >= ${value:g}", "quote_liquidity_usd", lambda x, v=value: x >= v))
    for value in (1_000, 5_000, 10_000, 25_000, 50_000, 100_000):
        cases.append(FilterCase(f"LiqDS >= ${value:g}", "liquidity_usd", lambda x, v=value: x >= v))
    for value in (5, 10, 15, 30, 60):
        cases.append(FilterCase(f"MinAg >= {value}m", "minimum_age_minutes", lambda x, v=value: x >= v))
    for value in (50, 60, 70, 80, 90):
        cases.append(FilterCase(f"Score >= {value}", "market_score", lambda x, v=value: x >= v))
    for value in (1_000, 10_000, 50_000, 100_000, 250_000):
        cases.append(FilterCase(f"Vol24h >= ${value:g}", "volume_h24", lambda x, v=value: x >= v))
    for value in (50, 100, 500, 1_000, 5_000):
        cases.append(FilterCase(f"Tx24h >= {value}", "txns_h24", lambda x, v=value: x >= v))
    for value in (20, 50, 100, 200):
        cases.append(FilterCase(f"abs(ChangeM5) <= {value}%", "signal_price_change_m5", lambda x, v=value: abs(x) <= v))
    return cases


def evaluate_case(rows: list[Dict], case: FilterCase, catastrophe_pnl: float) -> Dict:
    known = [row for row in rows if row.get(case.field) is not None]
    kept = [row for row in rows if row.get(case.field) is not None and case.predicate(float(row[case.field]))]
    kept_ids = {id(row) for row in kept}
    removed = [row for row in rows if id(row) not in kept_ids]
    catastrophes = [row for row in rows if row.get("pnl_pct") is not None and row["pnl_pct"] <= catastrophe_pnl]
    catastrophe_ids = {id(row) for row in catastrophes}
    removed_catastrophes = sum(id(row) in catastrophe_ids for row in removed)
    removed_noncatastrophes = len(removed) - removed_catastrophes
    kept_pnls = [float(row["pnl_pct"]) for row in kept if row.get("pnl_pct") is not None]
    return {
        "filter": case.name,
        "known": len(known),
        "kept": len(kept),
        "removed": len(removed),
        "cat_removed": removed_catastrophes,
        "cat_total": len(catastrophes),
        "noncat_removed": removed_noncatastrophes,
        "kept_pnl_total": sum(kept_pnls) if kept_pnls else None,
        "kept_pnl_avg": mean(kept_pnls) if kept_pnls else None,
    }


def evaluate_filters(rows: Iterable[Dict], catastrophe_pnl: float = -90) -> list[Dict]:
    materialized = list(rows)
    return [evaluate_case(materialized, case, catastrophe_pnl) for case in filter_cases()]


def fmt_pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:+.1f}%"


def print_results(rows: list[Dict], results: list[Dict], catastrophe_pnl: float) -> None:
    catastrophes = sum(
        row.get("pnl_pct") is not None and row["pnl_pct"] <= catastrophe_pnl
        for row in rows
    )
    print("# Stop Loss Filter Study")
    print(f"trades={len(rows)} | catastrofes_pnl<={catastrophe_pnl:g}%={catastrophes}")
    print("missing=falha no filtro (criterio conservador)")
    print()
    headers = ("FILTRO", "DADOS", "MANTEM", "REMOVE", "TIRA CATAST", "TIRA OUTROS", "PNL MANTIDO", "MEDIA")
    rendered = []
    for result in results:
        rendered.append((
            result["filter"],
            f"{result['known']}/{len(rows)}",
            str(result["kept"]),
            str(result["removed"]),
            f"{result['cat_removed']}/{result['cat_total']}",
            str(result["noncat_removed"]),
            fmt_pct(result["kept_pnl_total"]),
            fmt_pct(result["kept_pnl_avg"]),
        ))
    widths = [len(value) for value in headers]
    for row in rendered:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    line = lambda values: " | ".join(value.ljust(widths[index]) for index, value in enumerate(values))
    print(line(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rendered:
        print(line(row))


def main() -> None:
    parser = argparse.ArgumentParser(description="Compara filtros contra stop losses catastroficos.")
    parser.add_argument("--file", type=Path, default=DEFAULT_HISTORY_FILE)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--chain")
    parser.add_argument("--catastrophe-pnl", type=float, default=-90)
    args = parser.parse_args()
    since = parse_boundary(args.since)
    until = parse_boundary(args.until, end_of_day=True)
    rows = [row for row in (load_closed_positions(args.file) or []) if in_period(row, since, until)]
    if args.chain:
        rows = [row for row in rows if row.get("chain") == args.chain.lower()]
    print_results(rows, evaluate_filters(rows, args.catastrophe_pnl), args.catastrophe_pnl)


if __name__ == "__main__":
    main()
