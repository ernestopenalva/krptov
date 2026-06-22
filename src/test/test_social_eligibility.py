import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

requests = sys.modules.get("requests")
if requests is None:
    requests = types.ModuleType("requests")
    sys.modules["requests"] = requests

if not hasattr(requests, "RequestException"):
    class RequestException(Exception):
        pass

    requests.RequestException = RequestException

if not hasattr(requests, "HTTPError"):
    class HTTPError(requests.RequestException):
        pass

    requests.HTTPError = HTTPError

if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv

from src.modules import market_ranker, social_inference


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeDexscreenerSession:
    def __init__(self, pairs):
        self.pairs = pairs
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        return FakeResponse(self.pairs)


class SocialEligibilityTests(unittest.TestCase):
    def test_social_victor_has_no_minimum_age_gate_by_default(self):
        current_time = datetime(2026, 6, 12, 12, 0, 0)
        entry = {
            "status": "novo",
            "social_status": "pendente",
            "social_eligibility": "eligible",
            "market_score": 90,
            "quote_liquidity_usd": 1000,
            "minimum_token_age_inferred_minutes": 0,
        }

        self.assertEqual(social_inference.DEFAULT_CONFIG["min_social_age_minutes"], 0)
        self.assertIsNone(
            social_inference.social_query_skip_reason(
                entry,
                social_inference.DEFAULT_CONFIG,
                current_time=current_time,
            )
        )

    def test_social_inference_waits_for_minimum_age_before_starting_new_token(self):
        current_time = datetime(2026, 6, 12, 12, 0, 0)
        config = {
            **social_inference.DEFAULT_CONFIG,
            "min_social_age_minutes": 30,
        }

        young_entry = {
            "status": "novo",
            "social_status": "pendente",
            "social_eligibility": "eligible",
            "market_score": 90,
            "quote_liquidity_usd": 1000,
            "minimum_token_age_inferred_minutes": 10,
        }
        mature_entry = {
            "status": "novo",
            "social_status": "pendente",
            "social_eligibility": "eligible",
            "market_score": 90,
            "quote_liquidity_usd": 1000,
            "minimum_token_age_inferred_minutes": 30,
        }
        active_entry = {
            "status": "ativo",
            "social_status": "ativo",
            "social_eligibility": "eligible",
            "quote_liquidity_usd": 1000,
            "minimum_token_age_inferred_minutes": 10,
        }

        self.assertEqual(
            social_inference.social_query_skip_reason(young_entry, config, current_time=current_time),
            "social_age_too_young",
        )
        self.assertIsNone(
            social_inference.social_query_skip_reason(mature_entry, config, current_time=current_time)
        )
        self.assertIsNone(
            social_inference.social_query_skip_reason(active_entry, config, current_time=current_time)
        )

    def test_social_inference_blocks_missing_or_zero_quote_liquidity(self):
        current_time = datetime(2026, 6, 12, 12, 0, 0)
        config = {
            **social_inference.DEFAULT_CONFIG,
            "min_quote_liquidity_usd": 1,
        }
        base_entry = {
            "status": "novo",
            "social_status": "pendente",
            "social_eligibility": "eligible",
            "market_score": 90,
            "minimum_token_age_inferred_minutes": 30,
        }

        self.assertEqual(
            social_inference.social_query_skip_reason(base_entry, config, current_time=current_time),
            "low_quote_liquidity",
        )

        base_entry["quote_liquidity_usd"] = 0
        self.assertEqual(
            social_inference.social_query_skip_reason(base_entry, config, current_time=current_time),
            "low_quote_liquidity",
        )

        base_entry["quote_liquidity_usd"] = 1
        self.assertIsNone(
            social_inference.social_query_skip_reason(base_entry, config, current_time=current_time)
        )

    def test_social_usage_counts_unique_posts_for_budget(self):
        current_time = datetime(2026, 6, 12, 12, 0, 0)
        usage = {
            "date": "2026-06-12",
            "posts_returned": 0,
            "posts_returned_raw": 0,
            "seen_tweet_ids": [],
            "checks": 0,
        }
        payload = {"data": [{"id": "100"}, {"id": "101"}]}

        social_inference.register_social_check_usage("0x1", current_time, usage, payload)
        social_inference.register_social_check_usage("0x1", current_time, usage, payload)
        social_inference.register_social_check_usage("0x1", current_time, usage, {"data": [{"id": "101"}, {"id": "102"}]})

        self.assertEqual(usage["posts_returned"], 3)
        self.assertEqual(usage["posts_returned_raw"], 6)
        self.assertEqual(usage["seen_tweet_ids"], ["100", "101", "102"])

    def test_social_usage_deduplicates_posts_seen_in_previous_social_day(self):
        current_time = datetime(2026, 6, 12, 12, 0, 0)
        usage = {
            "date": "2026-06-12",
            "posts_returned": 0,
            "posts_returned_raw": 0,
            "seen_tweet_ids": [],
            "checks": 0,
        }
        recent_posts = {"100": "2026-06-11T13:00:00"}

        result = social_inference.register_social_check_usage(
            "0x1",
            current_time,
            usage,
            {"data": [{"id": "100"}, {"id": "101"}]},
            recent_posts=recent_posts,
        )

        self.assertEqual(result["posts_returned"], 1)
        self.assertEqual(result["posts_returned_raw"], 2)
        self.assertEqual(usage["posts_returned"], 1)
        self.assertEqual(recent_posts["101"], "2026-06-12T12:00:00")

    def test_social_inference_reserves_cycle_slots_for_new_and_active_tokens(self):
        watchlist = {
            "base:0x1111111111111111111111111111111111111111": {
                "chain": "base",
                "chain_id": "base",
                "token_address": "0x1111111111111111111111111111111111111111",
                "status": "ativo",
                "social_status": "ativo",
                "market_score": 60,
                "social_monitoring_started_at": "2026-06-15T10:00:00",
                "social_monitoring_expires_at": "2026-06-16T10:00:00",
            },
            "base:0x3333333333333333333333333333333333333333": {
                "chain": "base",
                "chain_id": "base",
                "token_address": "0x3333333333333333333333333333333333333333",
                "status": "ativo",
                "social_status": "ativo",
                "market_score": 50,
                "social_monitoring_started_at": "2026-06-15T10:00:00",
                "social_monitoring_expires_at": "2026-06-16T10:00:00",
            },
            "base:0x2222222222222222222222222222222222222222": {
                "chain": "base",
                "chain_id": "base",
                "token_address": "0x2222222222222222222222222222222222222222",
                "status": "novo",
                "social_status": "pendente",
                "market_score": 90,
            },
            "base:0x4444444444444444444444444444444444444444": {
                "chain": "base",
                "chain_id": "base",
                "token_address": "0x4444444444444444444444444444444444444444",
                "status": "novo",
                "social_status": "pendente",
                "market_score": 80,
            },
            "base:0x5555555555555555555555555555555555555555": {
                "chain": "base",
                "chain_id": "base",
                "token_address": "0x5555555555555555555555555555555555555555",
                "status": "novo",
                "social_status": "pendente",
                "market_score": 70,
            },
        }
        config = {
            **social_inference.DEFAULT_CONFIG,
            "max_tokens_per_cycle": 0,
            "max_new_tokens_per_cycle": 2,
            "max_active_tokens_per_cycle": 0,
        }

        candidates = social_inference.build_social_candidates(watchlist, config)

        self.assertEqual(
            [candidate["token_address"] for candidate in candidates],
            [
                "0x1111111111111111111111111111111111111111",
                "0x3333333333333333333333333333333333333333",
                "0x2222222222222222222222222222222222222222",
                "0x4444444444444444444444444444444444444444",
            ],
        )

    def test_social_victor_ignores_check_count_and_exits_by_time(self):
        entry = {
            "status": "ativo",
            "social_status": "ativo",
            "social_checks_count": 999,
            "social_monitoring_started_at": "2026-06-12T10:00:00",
            "social_monitoring_expires_at": "2026-06-12T12:00:00",
        }
        config = {**social_inference.DEFAULT_CONFIG, "max_social_checks_per_token": 0}

        self.assertFalse(social_inference.reached_max_social_checks(entry, config))
        social_inference.expire_social_monitoring(entry, datetime(2026, 6, 12, 12, 0, 0))

        self.assertEqual(entry["social_status"], "concluido")
        self.assertEqual(entry["status"], "descarte")
        self.assertEqual(entry["social_completed_reason"], "social_timeout")

    def test_ranker_blocks_social_when_oldest_pair_is_old(self):
        current_time = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        old_pair_created_at = int(datetime(2026, 6, 3, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        fresh_pair_created_at = int(datetime(2026, 6, 5, 11, 55, 0, tzinfo=timezone.utc).timestamp() * 1000)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watchlist_file = root / "watchlist.json"
            state_file = root / "state.json"
            market_dir = root / "market_ranker"
            lock_file = root / "watchlist.lock"
            token_address = "0x1111111111111111111111111111111111111111"
            pool_address = "0x2222222222222222222222222222222222222222"
            watchlist_key = f"ethereum:{token_address}"
            watchlist_file.write_text(
                json.dumps(
                    {
                        watchlist_key: {
                            "watchlist_key": watchlist_key,
                            "chain": "ethereum",
                            "chain_id": "ethereum",
                            "token_address": token_address,
                            "pool_address": pool_address,
                            "status": "novo",
                            "social_status": "pendente",
                            "monitor_status": "pendente",
                        }
                    }
                ),
                encoding="utf-8",
            )
            pairs = [
                {
                    "chainId": "ethereum",
                    "dexId": "uniswap",
                    "pairAddress": "0x3333333333333333333333333333333333333333",
                    "baseToken": {"address": token_address, "symbol": "TEST"},
                    "quoteToken": {"address": "0x0000000000000000000000000000000000000000", "symbol": "ETH"},
                    "pairCreatedAt": old_pair_created_at,
                    "liquidity": {"usd": 100},
                    "volume": {"h24": 0},
                    "txns": {"h24": {"buys": 0, "sells": 0}},
                },
                {
                    "chainId": "ethereum",
                    "dexId": "uniswap",
                    "pairAddress": pool_address,
                    "baseToken": {"address": token_address, "symbol": "TEST"},
                    "quoteToken": {"address": "0x0000000000000000000000000000000000000000", "symbol": "ETH"},
                    "pairCreatedAt": fresh_pair_created_at,
                    "liquidity": {"usd": 5000},
                    "volume": {"h24": 1000},
                    "txns": {"h24": {"buys": 5, "sells": 5}},
                },
            ]

            with patch.object(market_ranker, "WATCHLIST_FILE", watchlist_file), patch.object(
                market_ranker, "WATCHLIST_LOCK_FILE", lock_file
            ), patch.object(market_ranker, "STATE_FILE", state_file), patch.object(
                market_ranker, "MARKET_RANKER_DATA_DIR", market_dir
            ), patch.object(
                market_ranker, "DATA_DIR", root
            ), patch.object(
                market_ranker, "utc_now", return_value=current_time
            ):
                market_ranker.run_cycle(
                    dry_run=False,
                    session=FakeDexscreenerSession(pairs),
                )

            updated = json.loads(watchlist_file.read_text(encoding="utf-8"))
            entry = updated[watchlist_key]
            self.assertEqual(entry["social_eligibility"], "blocked_old_market")
            self.assertEqual(entry["social_eligibility_reason"], "old_market")
            self.assertEqual(entry["oldest_pair_created_at_utc"], "2026-06-03T10:00:00Z")
            self.assertEqual(entry["selected_pair_created_at_utc"], "2026-06-05T11:55:00Z")
            self.assertEqual(entry["minimum_token_age_inferred_source"], "oldest_pair")
            self.assertIsNotNone(entry.get("market_score"))

    def test_ranker_marks_fresh_market_eligible_without_contract_age(self):
        current_time = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        fresh_pair_created_at = int(datetime(2026, 6, 5, 11, 55, 0, tzinfo=timezone.utc).timestamp() * 1000)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watchlist_file = root / "watchlist.json"
            state_file = root / "state.json"
            market_dir = root / "market_ranker"
            lock_file = root / "watchlist.lock"
            token_address = "0x1111111111111111111111111111111111111111"
            pool_address = "0x2222222222222222222222222222222222222222"
            watchlist_key = f"ethereum:{token_address}"
            watchlist_file.write_text(
                json.dumps(
                    {
                        watchlist_key: {
                            "watchlist_key": watchlist_key,
                            "chain": "ethereum",
                            "chain_id": "ethereum",
                            "token_address": token_address,
                            "pool_address": pool_address,
                            "status": "novo",
                            "social_status": "pendente",
                            "monitor_status": "pendente",
                        }
                    }
                ),
                encoding="utf-8",
            )
            pairs = [
                {
                    "chainId": "ethereum",
                    "dexId": "uniswap",
                    "pairAddress": pool_address,
                    "baseToken": {"address": token_address, "symbol": "TEST"},
                    "quoteToken": {"address": "0x0000000000000000000000000000000000000000", "symbol": "ETH"},
                    "pairCreatedAt": fresh_pair_created_at,
                    "liquidity": {"usd": 5000},
                    "volume": {"h24": 1000},
                    "txns": {"h24": {"buys": 5, "sells": 5}},
                },
            ]

            with patch.object(market_ranker, "WATCHLIST_FILE", watchlist_file), patch.object(
                market_ranker, "WATCHLIST_LOCK_FILE", lock_file
            ), patch.object(market_ranker, "STATE_FILE", state_file), patch.object(
                market_ranker, "MARKET_RANKER_DATA_DIR", market_dir
            ), patch.object(
                market_ranker, "DATA_DIR", root
            ), patch.object(
                market_ranker, "utc_now", return_value=current_time
            ):
                market_ranker.run_cycle(
                    dry_run=False,
                    session=FakeDexscreenerSession(pairs),
                )

            updated = json.loads(watchlist_file.read_text(encoding="utf-8"))
            entry = updated[watchlist_key]
            self.assertEqual(entry["social_eligibility"], "eligible")
            self.assertEqual(entry["social_eligibility_reason"], "fresh_market")
            self.assertEqual(entry["minimum_token_age_inferred_source"], "oldest_pair")
            self.assertEqual(entry["minimum_token_age_inferred_minutes"], 5)

    def test_social_inference_skips_blocked_old_market_without_querying_x(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watchlist_file = root / "watchlist.json"
            lock_file = root / "watchlist.lock"
            latest_file = root / "social_inference_latest.json"
            alerts_file = root / "social_alerts.json"
            posts_dir = root / "social_posts"
            alert_posts_dir = root / "social_alert_posts"
            logs_dir = root / "logs"
            token_address = "0x1111111111111111111111111111111111111111"
            watchlist_key = f"ethereum:{token_address}"
            watchlist_file.write_text(
                json.dumps(
                    {
                        watchlist_key: {
                            "watchlist_key": watchlist_key,
                            "chain": "ethereum",
                            "chain_id": "ethereum",
                            "token_address": token_address,
                            "status": "novo",
                            "social_status": "pendente",
                            "monitor_status": "pendente",
                            "social_eligibility": "blocked_old_market",
                            "social_eligibility_reason": "old_market",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(social_inference, "DATA_DIR", root), patch.object(
                social_inference, "LOGS_DIR", logs_dir
            ), patch.object(social_inference, "WATCHLIST_FILE", watchlist_file), patch.object(
                social_inference, "WATCHLIST_LOCK_FILE", lock_file
            ), patch.object(social_inference, "LATEST_SNAPSHOT_FILE", latest_file), patch.object(
                social_inference, "ALERTS_FILE", alerts_file
            ), patch.object(social_inference, "POSTS_DIR", posts_dir), patch.object(
                social_inference, "ALERT_POSTS_DIR", alert_posts_dir
            ), patch.object(
                social_inference, "load_bearer_token", return_value="fake-token"
            ), patch.object(
                social_inference,
                "search_token_mentions",
                side_effect=AssertionError("X should not be queried"),
            ):
                snapshot = social_inference.run_cycle()

            self.assertEqual(snapshot["tokens_checked"], 0)
            self.assertEqual(snapshot["tokens_blocked_by_social_eligibility"], 1)
            updated = json.loads(watchlist_file.read_text(encoding="utf-8"))
            entry = updated[watchlist_key]
            self.assertEqual(entry["social_skip_reason"], "social_eligibility_blocked_old_market")
            self.assertIn("social_last_skipped_at", entry)

    def test_social_inference_continues_active_token_after_eligibility_expires(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watchlist_file = root / "watchlist.json"
            lock_file = root / "watchlist.lock"
            latest_file = root / "social_inference_latest.json"
            alerts_file = root / "social_alerts.json"
            posts_dir = root / "social_posts"
            alert_posts_dir = root / "social_alert_posts"
            logs_dir = root / "logs"
            token_address = "0x1111111111111111111111111111111111111111"
            watchlist_key = f"ethereum:{token_address}"
            watchlist_file.write_text(
                json.dumps(
                    {
                        watchlist_key: {
                            "watchlist_key": watchlist_key,
                            "chain": "ethereum",
                            "chain_id": "ethereum",
                            "token_address": token_address,
                            "status": "ativo",
                            "social_status": "ativo",
                            "monitor_status": "pendente",
                            "market_score": 75,
                            "quote_liquidity_usd": 1000,
                            "social_eligibility": "blocked_old_market",
                            "social_eligibility_reason": "old_market",
                            "social_monitoring_started_at": "2026-06-05T12:00:00",
                            "social_monitoring_expires_at": "2099-06-05T12:00:00",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(social_inference, "DATA_DIR", root), patch.object(
                social_inference, "LOGS_DIR", logs_dir
            ), patch.object(social_inference, "WATCHLIST_FILE", watchlist_file), patch.object(
                social_inference, "WATCHLIST_LOCK_FILE", lock_file
            ), patch.object(social_inference, "LATEST_SNAPSHOT_FILE", latest_file), patch.object(
                social_inference, "ALERTS_FILE", alerts_file
            ), patch.object(social_inference, "POSTS_DIR", posts_dir), patch.object(
                social_inference, "ALERT_POSTS_DIR", alert_posts_dir
            ), patch.object(
                social_inference, "load_bearer_token", return_value="fake-token"
            ), patch.object(
                social_inference,
                "search_token_mentions",
                return_value={"data": []},
            ):
                snapshot = social_inference.run_cycle()

            self.assertEqual(snapshot["tokens_checked"], 1)
            self.assertEqual(snapshot["tokens_blocked_by_social_eligibility"], 0)
            updated = json.loads(watchlist_file.read_text(encoding="utf-8"))
            self.assertEqual(updated[watchlist_key]["social_status"], "ativo")
            self.assertNotEqual(updated[watchlist_key].get("social_skip_reason"), "social_eligibility_blocked_old_market")

    def test_social_inference_requires_eligible_and_numeric_market_score(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watchlist_file = root / "watchlist.json"
            lock_file = root / "watchlist.lock"
            latest_file = root / "social_inference_latest.json"
            alerts_file = root / "social_alerts.json"
            posts_dir = root / "social_posts"
            alert_posts_dir = root / "social_alert_posts"
            logs_dir = root / "logs"
            missing_token = "0x1111111111111111111111111111111111111111"
            pending_token = "0x2222222222222222222222222222222222222222"
            no_score_token = "0x3333333333333333333333333333333333333333"
            watchlist_file.write_text(
                json.dumps(
                    {
                        f"ethereum:{missing_token}": {
                            "watchlist_key": f"ethereum:{missing_token}",
                            "chain": "ethereum",
                            "chain_id": "ethereum",
                            "token_address": missing_token,
                            "status": "novo",
                            "social_status": "pendente",
                            "monitor_status": "pendente",
                        },
                        f"ethereum:{pending_token}": {
                            "watchlist_key": f"ethereum:{pending_token}",
                            "chain": "ethereum",
                            "chain_id": "ethereum",
                            "token_address": pending_token,
                            "status": "novo",
                            "social_status": "pendente",
                            "monitor_status": "pendente",
                            "social_eligibility": "pending",
                        },
                        f"ethereum:{no_score_token}": {
                            "watchlist_key": f"ethereum:{no_score_token}",
                            "chain": "ethereum",
                            "chain_id": "ethereum",
                            "token_address": no_score_token,
                            "status": "novo",
                            "social_status": "pendente",
                            "monitor_status": "pendente",
                            "social_eligibility": "eligible",
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(social_inference, "DATA_DIR", root), patch.object(
                social_inference, "LOGS_DIR", logs_dir
            ), patch.object(social_inference, "WATCHLIST_FILE", watchlist_file), patch.object(
                social_inference, "WATCHLIST_LOCK_FILE", lock_file
            ), patch.object(social_inference, "LATEST_SNAPSHOT_FILE", latest_file), patch.object(
                social_inference, "ALERTS_FILE", alerts_file
            ), patch.object(social_inference, "POSTS_DIR", posts_dir), patch.object(
                social_inference, "ALERT_POSTS_DIR", alert_posts_dir
            ), patch.object(
                social_inference, "load_bearer_token", return_value="fake-token"
            ), patch.object(
                social_inference,
                "search_token_mentions",
                side_effect=AssertionError("X should not be queried"),
            ):
                snapshot = social_inference.run_cycle()

            self.assertEqual(snapshot["tokens_checked"], 0)
            self.assertEqual(snapshot["tokens_blocked_by_social_eligibility"], 2)
            self.assertEqual(snapshot["tokens_blocked_by_market_score"], 1)
            updated = json.loads(watchlist_file.read_text(encoding="utf-8"))
            self.assertEqual(
                updated[f"ethereum:{missing_token}"]["social_skip_reason"],
                "social_eligibility_not_eligible",
            )
            self.assertEqual(
                updated[f"ethereum:{pending_token}"]["social_skip_reason"],
                "social_eligibility_not_eligible",
            )
            self.assertEqual(
                updated[f"ethereum:{no_score_token}"]["social_skip_reason"],
                "missing_numeric_market_score",
            )


if __name__ == "__main__":
    unittest.main()
