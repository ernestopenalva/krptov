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
            self.assertIsNotNone(entry.get("market_score"))

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
