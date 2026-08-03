import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src.tools import closed_position_report as report


class ClosedPositionReportSinceTests(unittest.TestCase):
    def test_since_filters_by_exit_time_and_supports_brasilia_date(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            events = [
                {"event": "position_closed", "timestamp": "2026-08-01T02:00:00Z", "pnl_pct": 1,
                 "position": {"entry_time": "2026-08-01T01:00:00Z", "entry_price_usd": 1,
                               "min_price_usd": 1, "highest_price_usd": 1, "token_address": "old"},
                 "last_tick": {"observed_at": "2026-08-01T02:00:00Z", "price_usd": 1}},
                {"event": "position_closed", "timestamp": "2026-08-02T02:00:00Z", "pnl_pct": 2,
                 "position": {"entry_time": "2026-08-02T01:00:00Z", "entry_price_usd": 1,
                               "min_price_usd": 1, "highest_price_usd": 1, "token_address": "new"},
                 "last_tick": {"observed_at": "2026-08-02T02:00:00Z", "price_usd": 1}},
            ]
            path.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
            rows = [row for row in report.load_closed_positions(path)
                    if report.in_period(row, report.parse_boundary("2026-08-01"), None)]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["token_address"], "new")

    def test_boundary_accepts_iso_and_short_day_month(self):
        self.assertEqual(report.parse_boundary("2026-08-01T00:00:00Z").utcoffset().total_seconds(), -10800)
        self.assertEqual(report.parse_boundary("02/08 12:30").hour, 12)

    def test_invalid_boundary_exits_with_guidance(self):
        with self.assertRaises(SystemExit):
            report.parse_boundary("not-a-date")


if __name__ == "__main__":
    unittest.main()
