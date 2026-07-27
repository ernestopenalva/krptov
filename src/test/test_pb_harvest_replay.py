import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.tools.pb_harvest_replay import (
    HarvestScenario,
    load_pullbacks,
    load_ticks,
    replay_harvest,
    run_replays,
    scenario_summary,
)


def tick(start, seconds, price):
    observed_at = (start + timedelta(seconds=seconds)).isoformat()
    return {
        "timestamp": observed_at,
        "closed": False,
        "tick": {"observed_at": observed_at, "price_usd": price},
    }


class PbHarvestReplayTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 7, 21, tzinfo=timezone.utc)
        self.ticks = [
            {"observed_at": self.start, "observed_at_raw": self.start.isoformat(), "price_usd": 120},
            {
                "observed_at": self.start + timedelta(seconds=1),
                "observed_at_raw": (self.start + timedelta(seconds=1)).isoformat(),
                "price_usd": 130,
            },
            {
                "observed_at": self.start + timedelta(seconds=2),
                "observed_at_raw": (self.start + timedelta(seconds=2)).isoformat(),
                "price_usd": 124,
            },
            {
                "observed_at": self.start + timedelta(seconds=3),
                "observed_at_raw": (self.start + timedelta(seconds=3)).isoformat(),
                "price_usd": 123,
            },
            {
                "observed_at": self.start + timedelta(seconds=5),
                "observed_at_raw": (self.start + timedelta(seconds=5)).isoformat(),
                "price_usd": 122,
            },
        ]

    def scenario(self, persistence):
        return HarvestScenario(20, 15, 4, persistence)

    def test_zero_persistence_closes_on_first_tick_below_raw_trailing(self):
        result = replay_harvest(100, self.ticks, self.scenario(0))
        self.assertTrue(result["closed"])
        self.assertAlmostEqual(result["exit_pnl_pct"], 24)

    def test_one_and_three_second_persistence_use_observed_prices(self):
        one = replay_harvest(100, self.ticks, self.scenario(1))
        three = replay_harvest(100, self.ticks, self.scenario(3))
        self.assertAlmostEqual(one["exit_pnl_pct"], 23)
        self.assertAlmostEqual(three["exit_pnl_pct"], 22)

    def test_condition_resets_when_price_recovers_above_threshold(self):
        ticks = [
            self.ticks[0],
            self.ticks[1],
            self.ticks[2],
            {
                "observed_at": self.start + timedelta(seconds=3),
                "observed_at_raw": (self.start + timedelta(seconds=3)).isoformat(),
                "price_usd": 126,
            },
            {
                "observed_at": self.start + timedelta(seconds=6),
                "observed_at_raw": (self.start + timedelta(seconds=6)).isoformat(),
                "price_usd": 124,
            },
        ]
        result = replay_harvest(100, ticks, self.scenario(3))
        self.assertFalse(result["closed"])

    def test_loads_pb_and_runs_history_file(self):
        position_id = "pos-test"
        event = {
            "event": "position_closed",
            "position_id": position_id,
            "pnl_pct": 10,
            "exit_reason": "TRAILING_STOP",
            "position": {
                "position_id": position_id,
                "entry_time": self.start.isoformat(),
                "entry_price_usd": 100,
                "highest_price_usd": 130,
                "token_address": "token",
                "source_signal": {
                    "entry_reason": "PULLBACK_RECOVERY",
                    "token_symbol": "TEST",
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trading = root / "trading.jsonl"
            history_dir = root / "position"
            history_dir.mkdir()
            trading.write_text(json.dumps(event) + "\n", encoding="utf-8")
            history_path = history_dir / f"{position_id}.jsonl"
            history_path.write_text(
                "\n".join(json.dumps(tick(self.start, seconds, price)) for seconds, price in [
                    (0, 120), (1, 130), (2, 124), (3, 123),
                ]) + "\n",
                encoding="utf-8",
            )
            positions = load_pullbacks(trading)
            loaded_ticks = load_ticks(history_path)
            rows = run_replays(positions, history_dir, [self.scenario(1)])
        self.assertEqual(len(positions), 1)
        self.assertEqual(len(loaded_ticks), 4)
        self.assertAlmostEqual(rows[0]["scenarios"]["harvest_p1s"]["exit_pnl_pct"], 23)
        totals = scenario_summary(rows, self.scenario(1))
        self.assertAlmostEqual(totals["actual_total"], 10)
        self.assertAlmostEqual(totals["simulated_total"], 23)
        self.assertAlmostEqual(totals["delta"], 13)

    def test_summary_excludes_position_without_tick_history(self):
        scenario = self.scenario(1)
        rows = [{
            "actual_pnl_pct": 10,
            "actual_max_pnl_pct": 30,
            "history_found": False,
            "ticks": 0,
            "scenarios": {
                scenario.name: {
                    "triggered": False,
                    "closed": False,
                    "exit_pnl_pct": None,
                },
            },
        }]
        totals = scenario_summary(rows, scenario)
        self.assertEqual(totals["comparable"], 0)
        self.assertEqual(totals["missing_target_history"], 1)
        self.assertIsNone(totals["simulated_total"])


if __name__ == "__main__":
    unittest.main()
