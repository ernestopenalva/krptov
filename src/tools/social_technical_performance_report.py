#!/usr/bin/env python3
"""Cross social alerts with their later technical-circuit outcomes."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_HISTORY_FILE = DEFAULT_DATA_DIR / "trading_history.jsonl"
BRASILIA = ZoneInfo("America/Sao_Paulo")


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
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BRASILIA)
    return parsed.astimezone(BRASILIA)


def identity(chain: Any, address: Any, watchlist_key: Any = None) -> Optional[str]:
    if watchlist_key:
        return str(watchlist_key)
    if not chain or not address:
        return None
    chain_text = str(chain).lower()
    address_text = str(address)
    if chain_text != "solana":
        address_text = address_text.lower()
    return f"{chain_text}:{address_text}"


def alert_identity(alert: Dict[str, Any]) -> Optional[str]:
    return identity(
        alert.get("chain_id") or alert.get("chain"),
        alert.get("token_address"),
        alert.get("watchlist_key"),
    )


def event_identity(event: Dict[str, Any]) -> Optional[str]:
    position = event.get("position") or {}
    signal = position.get("source_signal") or event.get("signal") or {}
    return identity(
        position.get("chain") or signal.get("chain") or signal.get("chain_id"),
        position.get("token_address") or signal.get("token_address"),
        event.get("watchlist_key") or position.get("watchlist_key") or signal.get("watchlist_key"),
    )


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


def load_alerts(data_dir: Path) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    daily_files = sorted(data_dir.glob("social_alerts_????-??-??.jsonl"))
    for path in daily_files:
        rows.extend(load_jsonl(path) or [])

    aggregate = data_dir / "social_alerts.json"
    if aggregate.exists():
        try:
            payload = json.loads(aggregate.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            payload = []
        if isinstance(payload, dict):
            payload = payload.get("alerts") or []
        if isinstance(payload, list):
            rows.extend(row for row in payload if isinstance(row, dict))

    unique: Dict[tuple[Any, ...], Dict[str, Any]] = {}
    for row in rows:
        key = (
            alert_identity(row),
            row.get("timestamp"),
            row.get("alert_signature") or row.get("alert_reason"),
        )
        unique[key] = row
    return list(unique.values())


def load_history(path: Path) -> list[Dict[str, Any]]:
    return list(load_jsonl(path) or [])


def in_date_range(value: Any, from_date: Optional[date], to_date: Optional[date]) -> bool:
    parsed = parse_time(value)
    if parsed is None:
        return from_date is None and to_date is None
    local_date = parsed.date()
    return (from_date is None or local_date >= from_date) and (to_date is None or local_date <= to_date)


def happened_after(event: Dict[str, Any], alert_at: Optional[datetime]) -> bool:
    if alert_at is None:
        return True
    event_at = parse_time(event.get("timestamp"))
    return event_at is None or event_at >= alert_at


def position_metrics(event: Dict[str, Any]) -> Dict[str, Any]:
    position = event.get("position") or {}
    signal = position.get("source_signal") or {}
    tick = event.get("last_tick") or position.get("last_tick") or {}
    entry = safe_float(position.get("entry_price_usd"))
    minimum = safe_float(position.get("min_price_usd"))
    maximum = safe_float(position.get("highest_price_usd"))
    return {
        "entry_time": position.get("entry_time"),
        "exit_time": tick.get("observed_at") or event.get("timestamp"),
        "entry_reason": signal.get("entry_reason") or signal.get("entry_type") or "UNKNOWN",
        "admission_source": signal.get("admission_source") or "unknown",
        "pnl_pct": safe_float(event.get("pnl_pct")),
        "min_pnl_pct": ((minimum / entry) - 1) * 100 if entry and minimum is not None else None,
        "max_pnl_pct": ((maximum / entry) - 1) * 100 if entry and maximum is not None else None,
        "exit_reason": event.get("exit_reason") or "UNKNOWN",
        "symbol": (
            signal.get("token_symbol")
            or signal.get("token_name")
            or signal.get("symbol")
            or position.get("symbol")
            or "-"
        ),
        "token_address": position.get("token_address") or signal.get("token_address") or "-",
        "rank_bypass": signal.get("rank_bypass") is True,
    }


def route_class(alert: Dict[str, Any], later_events: list[Dict[str, Any]]) -> str:
    if alert.get("monitor_admitted") is True:
        return "REDIMIDO_SOCIAL"
    for event in later_events:
        if event.get("event") != "position_closed":
            continue
        metrics = position_metrics(event)
        if metrics["admission_source"] == "social_alert" or metrics["rank_bypass"]:
            return "REDIMIDO_SOCIAL"
    if alert.get("monitor_admission_requested") is False:
        return "NAO_ROTEADO"
    reason = alert.get("monitor_admission_reason")
    if reason == "already_in_monitor_circuit" or later_events:
        return "JA_NO_TECNICO"
    return "NAO_ADMITIDO"


def technical_status(later_events: list[Dict[str, Any]]) -> str:
    closed = [event for event in later_events if event.get("event") == "position_closed"]
    if closed:
        return "TRADE_FECHADO"
    monitor_buys = [
        event for event in later_events
        if event.get("event") == "monitor_finished"
        and (event.get("result") or {}).get("outcome") == "buy"
    ]
    if monitor_buys:
        return "BUY_SEM_FECHAMENTO"
    monitored = [event for event in later_events if event.get("event") == "monitor_finished"]
    if monitored:
        return "MONITORADO_SEM_BUY"
    return "SEM_EVENTO_TECNICO"


def build_rows(
    alerts: Iterable[Dict[str, Any]],
    history: Iterable[Dict[str, Any]],
    *,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    only_sent: bool = False,
) -> list[Dict[str, Any]]:
    indexed: Dict[str, list[Dict[str, Any]]] = defaultdict(list)
    for event in history:
        key = event_identity(event)
        if key:
            indexed[key].append(event)

    rows = []
    for alert in alerts:
        if only_sent and alert.get("telegram_alert_sent") is not True:
            continue
        if not in_date_range(alert.get("timestamp"), from_date, to_date):
            continue
        key = alert_identity(alert)
        alert_at = parse_time(alert.get("timestamp"))
        later = [
            event for event in indexed.get(key or "", [])
            if happened_after(event, alert_at)
        ]
        later.sort(key=lambda event: parse_time(event.get("timestamp")) or datetime.min.replace(tzinfo=BRASILIA))
        closed = [position_metrics(event) for event in later if event.get("event") == "position_closed"]
        pnls = [trade["pnl_pct"] for trade in closed if trade["pnl_pct"] is not None]
        minimums = [trade["min_pnl_pct"] for trade in closed if trade["min_pnl_pct"] is not None]
        maximums = [trade["max_pnl_pct"] for trade in closed if trade["max_pnl_pct"] is not None]
        first_trade = closed[0] if closed else {}
        entry_at = parse_time(first_trade.get("entry_time"))
        latency = (entry_at - alert_at).total_seconds() / 60 if entry_at and alert_at else None
        reasons = alert.get("alert_reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        rows.append({
            "alert_time": alert.get("timestamp"),
            "watchlist_key": key or "-",
            "chain": alert.get("chain_id") or str(key or "unknown").split(":", 1)[0],
            "token_address": alert.get("token_address") or first_trade.get("token_address") or "-",
            "symbol": first_trade.get("symbol") or alert.get("token_symbol") or "-",
            "alert_rank": alert.get("alert_rank"),
            "alert_reasons": reasons,
            "telegram_sent": alert.get("telegram_alert_sent") is True,
            "monitor_requested": alert.get("monitor_admission_requested"),
            "monitor_admitted": alert.get("monitor_admitted") is True,
            "route_class": route_class(alert, later),
            "technical_status": technical_status(later),
            "monitor_attempts": sum(event.get("event") == "monitor_finished" for event in later),
            "trades": len(closed),
            "entry_reason": ",".join(sorted({trade["entry_reason"] for trade in closed})) or "-",
            "entry_latency_minutes": latency,
            "pnl_pct": sum(pnls) if pnls else None,
            "min_pnl_pct": min(minimums) if minimums else None,
            "max_pnl_pct": max(maximums) if maximums else None,
            "exit_reason": ",".join(sorted({trade["exit_reason"] for trade in closed})) or "-",
        })
    rows.sort(
        key=lambda row: parse_time(row["alert_time"]) or datetime.min.replace(tzinfo=BRASILIA),
        reverse=True,
    )
    return rows


def fmt_pct(value: Optional[float]) -> str:
    return f"{value:+.2f}%" if value is not None else "-"


def fmt_number(value: Optional[float]) -> str:
    return f"{value:.1f}" if value is not None else "-"


def fmt_time(value: Any) -> str:
    parsed = parse_time(value)
    return parsed.strftime("%d/%m %H:%M:%S") if parsed else "-"


def summary(rows: list[Dict[str, Any]]) -> Dict[str, Any]:
    closed = [row for row in rows if row["trades"]]
    pnls = [row["pnl_pct"] for row in closed if row["pnl_pct"] is not None]
    return {
        "alerts": len(rows),
        "telegram_sent": sum(row["telegram_sent"] for row in rows),
        "social_redemptions": sum(row["route_class"] == "REDIMIDO_SOCIAL" for row in rows),
        "already_technical": sum(row["route_class"] == "JA_NO_TECNICO" for row in rows),
        "monitored": sum(row["monitor_attempts"] > 0 for row in rows),
        "closed": len(closed),
        "wins": sum((row["pnl_pct"] or 0) > 0 for row in closed),
        "losses": sum((row["pnl_pct"] or 0) <= 0 for row in closed),
        "pnl_total": sum(pnls) if pnls else None,
        "pnl_average": mean(pnls) if pnls else None,
    }


def compact_performance(rows: list[Dict[str, Any]]) -> str:
    totals = summary(rows)
    return (
        f"alertas={totals['alerts']} | trades={totals['closed']} | "
        f"vitorias={totals['wins']} | derrotas={totals['losses']} | "
        f"pnl_total={fmt_pct(totals['pnl_total'])} | pnl_medio={fmt_pct(totals['pnl_average'])}"
    )


def print_table(rows: list[Dict[str, Any]]) -> None:
    headers = [
        "ALERTA", "ROTA", "TECNICO", "RANK", "TENT", "TRADES", "LAT MIN",
        "ESTRATEGIA", "PNL", "PNL MIN", "PNL MAX", "EXIT", "TOKEN", "CHAIN", "CA",
    ]
    rendered = []
    for row in rows:
        rendered.append([
            fmt_time(row["alert_time"]),
            row["route_class"],
            row["technical_status"],
            str(row["alert_rank"] if row["alert_rank"] is not None else "-"),
            str(row["monitor_attempts"]),
            str(row["trades"]),
            fmt_number(row["entry_latency_minutes"]),
            row["entry_reason"],
            fmt_pct(row["pnl_pct"]),
            fmt_pct(row["min_pnl_pct"]),
            fmt_pct(row["max_pnl_pct"]),
            row["exit_reason"],
            str(row["symbol"]),
            str(row["chain"]),
            str(row["token_address"]),
        ])
    widths = [len(header) for header in headers]
    for values in rendered:
        for index, value in enumerate(values):
            widths[index] = max(widths[index], len(value))
    line = lambda values: " | ".join(value.ljust(widths[index]) for index, value in enumerate(values))
    print(line(headers))
    print("-+-".join("-" * width for width in widths))
    for values in rendered:
        print(line(values))


def parse_date(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cruza alertas sociais com monitoramento e Positions posteriores do circuito tecnico."
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY_FILE)
    parser.add_argument("--from-date", help="Data inicial em Brasilia (YYYY-MM-DD).")
    parser.add_argument("--to-date", help="Data final em Brasilia (YYYY-MM-DD).")
    parser.add_argument("--only-sent", action="store_true", help="Considera apenas alertas enviados ao Telegram.")
    parser.add_argument("--limit", type=int, default=0, help="0 mostra todos; positivo mostra os mais recentes.")
    args = parser.parse_args()

    rows = build_rows(
        load_alerts(args.data_dir),
        load_history(args.history),
        from_date=parse_date(args.from_date),
        to_date=parse_date(args.to_date),
        only_sent=args.only_sent,
    )
    totals = summary(rows)
    print("# Social x Technical Performance Report")
    print(f"alertas={totals['alerts']} | telegram_enviados={totals['telegram_sent']}")
    print(
        f"redimidos_social={totals['social_redemptions']} | "
        f"ja_no_tecnico={totals['already_technical']} | monitorados={totals['monitored']}"
    )
    print(
        f"trades_fechados={totals['closed']} | vitorias={totals['wins']} | derrotas={totals['losses']} | "
        f"pnl_total={fmt_pct(totals['pnl_total'])} | pnl_medio={fmt_pct(totals['pnl_average'])}"
    )
    print("\n## Performance por relacao com o alerta")
    for route in ("REDIMIDO_SOCIAL", "JA_NO_TECNICO", "NAO_ROTEADO", "NAO_ADMITIDO"):
        cohort = [row for row in rows if row["route_class"] == route]
        if cohort:
            print(f"{route} | {compact_performance(cohort)}")
    print("\n## Performance por estrategia")
    strategies = sorted({
        strategy
        for row in rows
        for strategy in str(row["entry_reason"]).split(",")
        if strategy != "-"
    })
    for strategy in strategies:
        cohort = [
            row for row in rows
            if strategy in str(row["entry_reason"]).split(",")
        ]
        print(f"{strategy} | {compact_performance(cohort)}")

    selected = rows[:args.limit] if args.limit > 0 else rows
    print(f"\n## Alertas cruzados ({len(selected)} mostrados)")
    if selected:
        print_table(selected)
    else:
        print("Nenhum alerta encontrado no filtro selecionado.")


if __name__ == "__main__":
    main()
