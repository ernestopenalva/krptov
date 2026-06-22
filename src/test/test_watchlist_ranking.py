import argparse
import json
import tempfile
import unittest
from pathlib import Path

from src.tools import watchlist_ranking


def args(**updates):
    defaults = {
        "chain": None,
        "source": None,
        "eligible_only": False,
        "active_only": False,
        "top": 30,
    }
    defaults.update(updates)
    return argparse.Namespace(**defaults)


class WatchlistRankingTests(unittest.TestCase):
    def test_ranking_prioritizes_social_ready_then_score(self):
        watchlist = {
            "ethereum:0x1111111111111111111111111111111111111111": {
                "chain": "ethereum",
                "token_address": "0x1111111111111111111111111111111111111111",
                "token_symbol": "AAA",
                "token_name": "Alpha",
                "quote_token": "WETH",
                "status": "novo",
                "social_eligibility": "eligible",
                "market_score": 10,
                "liquidity_usd": 1200,
                "quote_liquidity_usd": 900,
                "volume_h24": 3400,
                "txns_h24": 12,
                "market_sanity_status": "ok",
                "minimum_token_age_inferred_minutes": 45,
                "minimum_token_age_inferred_source": "oldest_pair",
            },
            "base:0x2222222222222222222222222222222222222222": {
                "chain": "base",
                "token_address": "0x2222222222222222222222222222222222222222",
                "status": "novo",
                "social_eligibility": "eligible",
                "market_score": 90,
                "quote_liquidity_usd": 1000,
                "minimum_token_age_inferred_minutes": 45,
            },
            "ethereum:0x3333333333333333333333333333333333333333": {
                "chain": "ethereum",
                "token_address": "0x3333333333333333333333333333333333333333",
                "status": "novo",
                "social_eligibility": "pending",
                "market_score": 100,
            },
        }

        ranked = watchlist_ranking.ranked_entries(watchlist, args())

        self.assertEqual(ranked[0]["chain"], "base")
        self.assertEqual(ranked[0]["market_score"], 90)
        self.assertEqual(ranked[1]["market_score"], 10)
        self.assertEqual(ranked[2]["social_eligibility"], "pending")
        self.assertEqual(watchlist_ranking.display_name(ranked[1]), "AAA/WETH")
        rows = watchlist_ranking.table_rows([ranked[1]], {}, top=1)
        self.assertEqual(rows[0]["liq"], "$1.2K")
        self.assertEqual(rows[0]["quote_liq"], "$900")
        self.assertEqual(rows[0]["vol"], "$3.4K")
        self.assertEqual(rows[0]["txns"], "12")
        self.assertEqual(rows[0]["ca"], "0x1111111111111111111111111111111111111111")
        self.assertEqual(rows[0]["minimum_age"], "45m")
        self.assertEqual(rows[0]["window"], "fila")
        self.assertIn(("ca", "CA", 42), watchlist_ranking.table_columns(width=160))
        self.assertIn(("window", "Jan", 5), watchlist_ranking.table_columns(width=160))
        self.assertNotIn(("sanity", "San", 3), watchlist_ranking.table_columns(width=160))
        self.assertNotIn(("done", "Reas", 6), watchlist_ranking.table_columns(width=160))
        self.assertNotIn(("ca", "CA", 42), watchlist_ranking.table_columns(width=80))
        self.assertNotIn(("done", "Reas", 5), watchlist_ranking.table_columns(width=80))

    def test_eligible_only_filters_social_candidates(self):
        watchlist = {
            "ethereum:0x1111111111111111111111111111111111111111": {
                "chain": "ethereum",
                "token_address": "0x1111111111111111111111111111111111111111",
                "status": "novo",
                "social_eligibility": "eligible",
                "market_score": 10,
                "quote_liquidity_usd": 1000,
                "minimum_token_age_inferred_minutes": 45,
            },
            "ethereum:0x2222222222222222222222222222222222222222": {
                "chain": "ethereum",
                "token_address": "0x2222222222222222222222222222222222222222",
                "status": "novo",
                "social_eligibility": "eligible",
            },
        }

        ranked = watchlist_ranking.ranked_entries(watchlist, args(eligible_only=True))

        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["market_score"], 10)

    def test_social_victor_prioritizes_active_and_shows_remaining_window(self):
        current = watchlist_ranking.datetime(
            2026, 6, 15, 12, 0, tzinfo=watchlist_ranking.BRASILIA_TZ
        )
        watchlist = {
            "ethereum:0x1111111111111111111111111111111111111111": {
                "chain": "ethereum",
                "token_address": "0x1111111111111111111111111111111111111111",
                "status": "ativo",
                "social_status": "ativo",
                "social_eligibility": "blocked_old_market",
                "quote_liquidity_usd": 1000,
                "social_monitoring_expires_at": "2026-06-15T13:30:00-03:00",
            },
            "ethereum:0x2222222222222222222222222222222222222222": {
                "chain": "ethereum",
                "token_address": "0x2222222222222222222222222222222222222222",
                "status": "novo",
                "social_status": "pendente",
                "social_eligibility": "eligible",
                "market_score": 100,
                "quote_liquidity_usd": 1000,
            },
        }

        ranked = watchlist_ranking.ranked_entries(watchlist, args())
        rows = watchlist_ranking.table_rows(ranked, {}, top=2, current_time=current)

        self.assertEqual(ranked[0]["social_status"], "ativo")
        self.assertEqual(rows[0]["window"], "1.5h")
        self.assertEqual(rows[1]["window"], "fila")

    def test_movement_marker_reports_position_change(self):
        previous = {"a": 3, "b": 1}

        self.assertEqual(watchlist_ranking.movement_marker("a", 1, previous), "up 2")
        self.assertEqual(watchlist_ranking.movement_marker("b", 2, previous), "down 1")
        self.assertEqual(watchlist_ranking.movement_marker("c", 4, previous), "new")

    def test_social_completion_summary_counts_today_in_brasilia(self):
        entries = [
            {
                "social_completed_reason": "alert_sent",
                "social_monitoring_completed_at": "2026-06-15T03:30:00Z",
            },
            {
                "social_completed_reason": "alert_sent",
                "social_monitoring_completed_at": "2026-06-14T23:30:00Z",
            },
            {
                "social_completed_reason": "max_social_checks",
                "social_monitoring_completed_at": "2026-06-15T12:00:00-03:00",
            },
        ]

        total, today = watchlist_ranking.social_completion_summary(
            entries,
            current_date=watchlist_ranking.datetime(2026, 6, 15).date(),
        )

        self.assertEqual(total["alert_sent"], 2)
        self.assertEqual(today["alert_sent"], 1)
        self.assertEqual(today["max_social_checks"], 1)
        self.assertEqual(
            watchlist_ranking.format_social_completion_summary(total, today),
            "alert 1/2 | maxchk legado 1/1",
        )

    def test_next_bucket_uses_current_brasilia_time(self):
        current = watchlist_ranking.datetime(
            2026,
            6,
            15,
            18,
            33,
            tzinfo=watchlist_ranking.BRASILIA_TZ,
        )

        next_bucket = watchlist_ranking.next_bucket_time(
            current,
            bucket_minutes=32,
            usage_date="2026-06-15",
        )

        self.assertEqual(next_bucket.strftime("%H:%M"), "19:04")

    def test_last_social_cycle_summary_counts_latest_timestamp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            data_dir.mkdir()
            history = data_dir / "social_inference_2026-06-15.jsonl"
            rows = [
                {"timestamp": "2026-06-15T10:00:00", "status_before": "novo"},
                {"timestamp": "2026-06-15T10:00:00", "status_before": "ativo"},
                {"timestamp": "2026-06-15T10:03:00", "status_before": "novo"},
                {"timestamp": "2026-06-15T10:03:00", "status_before": "novo"},
                {"timestamp": "2026-06-15T10:03:00", "status_before": "ativo"},
            ]
            history.write_text(
                "\n".join(json.dumps(row) for row in rows),
                encoding="utf-8",
            )

            original_root = watchlist_ranking.PROJECT_ROOT
            watchlist_ranking.PROJECT_ROOT = root
            try:
                summary = watchlist_ranking.load_last_social_cycle_summary(
                    "2026-06-15",
                    new_limit=10,
                    active_limit=40,
                )
            finally:
                watchlist_ranking.PROJECT_ROOT = original_root

        self.assertEqual(summary["new_count"], 2)
        self.assertEqual(summary["active_count"], 1)
        self.assertEqual(summary["total_count"], 3)


if __name__ == "__main__":
    unittest.main()
