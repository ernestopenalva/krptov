import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.tools.social_technical_performance_report import (
    build_rows,
    load_alerts,
    load_history,
    summary,
)


TOKEN_A = "FtoDggFJiAZQybpmWZbGFioKqtpL7LRcrJrF9Aw2pump"
TOKEN_B = "SrxbiYw29qnTH9dwg3LyNVg9UMHwsfjC9U9dkCtpump"


class SocialTechnicalPerformanceReportTests(unittest.TestCase):
    def test_crosses_redemption_with_closed_trade_and_excursions(self):
        alert = {
            "timestamp": "2026-07-21T12:00:00+00:00",
            "chain_id": "solana",
            "token_address": TOKEN_A,
            "watchlist_key": f"solana:{TOKEN_A}",
            "alert_rank": 90,
            "telegram_alert_sent": True,
            "monitor_admission_requested": True,
            "monitor_admitted": True,
            "monitor_admission_reason": "social_fifo",
        }
        monitor = {
            "timestamp": "2026-07-21T12:01:00+00:00",
            "event": "monitor_finished",
            "watchlist_key": f"solana:{TOKEN_A}",
            "result": {"outcome": "buy"},
        }
        closed = {
            "timestamp": "2026-07-21T12:10:00+00:00",
            "event": "position_closed",
            "watchlist_key": f"solana:{TOKEN_A}",
            "exit_reason": "TRAILING_STOP",
            "pnl_pct": 8,
            "position": {
                "watchlist_key": f"solana:{TOKEN_A}",
                "chain": "solana",
                "token_address": TOKEN_A,
                "entry_time": "2026-07-21T12:02:00+00:00",
                "entry_price_usd": 1,
                "min_price_usd": .95,
                "highest_price_usd": 1.12,
                "source_signal": {
                    "watchlist_key": f"solana:{TOKEN_A}",
                    "token_symbol": "ALPHA",
                    "entry_reason": "MOMENTUM_CONTINUATION",
                    "admission_source": "social_alert",
                    "rank_bypass": True,
                },
            },
        }
        rows = build_rows([alert], [monitor, closed])
        self.assertEqual(rows[0]["route_class"], "REDIMIDO_SOCIAL")
        self.assertEqual(rows[0]["technical_status"], "TRADE_FECHADO")
        self.assertEqual(rows[0]["entry_latency_minutes"], 2)
        self.assertAlmostEqual(rows[0]["min_pnl_pct"], -5)
        self.assertAlmostEqual(rows[0]["max_pnl_pct"], 12)
        self.assertEqual(summary(rows)["pnl_total"], 8)

    def test_distinguishes_already_technical_and_ignores_event_before_alert(self):
        alert = {
            "timestamp": "2026-07-22T12:00:00+00:00",
            "chain_id": "solana",
            "token_address": TOKEN_B,
            "watchlist_key": f"solana:{TOKEN_B}",
            "monitor_admission_requested": True,
            "monitor_admitted": False,
            "monitor_admission_reason": "already_in_monitor_circuit",
        }
        before = {
            "timestamp": "2026-07-22T11:59:00+00:00",
            "event": "monitor_finished",
            "watchlist_key": f"solana:{TOKEN_B}",
            "result": {"outcome": "discard"},
        }
        after = {
            "timestamp": "2026-07-22T12:01:00+00:00",
            "event": "monitor_finished",
            "watchlist_key": f"solana:{TOKEN_B}",
            "result": {"outcome": "discard"},
        }
        rows = build_rows([alert], [before, after])
        self.assertEqual(rows[0]["route_class"], "JA_NO_TECNICO")
        self.assertEqual(rows[0]["technical_status"], "MONITORADO_SEM_BUY")
        self.assertEqual(rows[0]["monitor_attempts"], 1)

    def test_loads_and_deduplicates_daily_and_aggregate_alerts(self):
        alert = {
            "timestamp": "2026-07-21T12:00:00+00:00",
            "chain_id": "solana",
            "token_address": TOKEN_A,
            "watchlist_key": f"solana:{TOKEN_A}",
            "alert_signature": "rank_90",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "social_alerts_2026-07-21.jsonl").write_text(
                json.dumps(alert) + "\n", encoding="utf-8"
            )
            (root / "social_alerts.json").write_text(json.dumps([alert]), encoding="utf-8")
            history = root / "trading_history.jsonl"
            history.write_text("", encoding="utf-8")
            alerts = load_alerts(root)
            events = load_history(history)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(events, [])

    def test_filters_by_brasilia_calendar_date(self):
        alerts = [{
            "timestamp": "2026-07-22T01:00:00+00:00",
            "chain_id": "solana",
            "token_address": TOKEN_A,
        }]
        included = build_rows(
            alerts,
            [],
            from_date=date(2026, 7, 21),
            to_date=date(2026, 7, 21),
        )
        self.assertEqual(len(included), 1)


if __name__ == "__main__":
    unittest.main()
