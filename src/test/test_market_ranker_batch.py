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

from src.modules import market_ranker


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeBatchSession:
    def __init__(self, pairs):
        self.pairs = pairs
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        return FakeResponse(self.pairs)


def token_entry(token_address, watchlist_key):
    return {
        "watchlist_key": watchlist_key,
        "chain": "ethereum",
        "chain_id": "ethereum",
        "token_address": token_address,
        "status": "novo",
        "social_status": "pendente",
        "monitor_status": "pendente",
        "created_at_utc": "2026-06-06T11:55:00Z",
    }


def pair_for(token_address, pair_address, liquidity=5000):
    return {
        "chainId": "ethereum",
        "dexId": "uniswap",
        "pairAddress": pair_address,
        "baseToken": {"address": token_address, "symbol": "TEST", "name": "Test Token"},
        "quoteToken": {"address": "0x0000000000000000000000000000000000000000", "symbol": "ETH"},
        "pairCreatedAt": int(datetime(2026, 6, 6, 11, 56, 0, tzinfo=timezone.utc).timestamp() * 1000),
        "liquidity": {"usd": liquidity},
        "volume": {"h24": 1000},
        "txns": {"h24": {"buys": 10, "sells": 10}},
    }


class MarketRankerBatchTests(unittest.TestCase):
    def test_ranker_uses_batch_endpoint_for_same_chain_tokens(self):
        current_time = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watchlist_file = root / "watchlist.json"
            state_file = root / "state.json"
            market_dir = root / "market_ranker"
            lock_file = root / "watchlist.lock"
            token_a = "0x1111111111111111111111111111111111111111"
            token_b = "0x2222222222222222222222222222222222222222"
            key_a = f"ethereum:{token_a}"
            key_b = f"ethereum:{token_b}"
            watchlist_file.write_text(
                json.dumps(
                    {
                        key_a: token_entry(token_a, key_a),
                        key_b: token_entry(token_b, key_b),
                    }
                ),
                encoding="utf-8",
            )
            session = FakeBatchSession(
                [
                    pair_for(token_a, "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                    pair_for(token_b, "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
                ]
            )

            with patch.object(market_ranker, "WATCHLIST_FILE", watchlist_file), patch.object(
                market_ranker, "WATCHLIST_LOCK_FILE", lock_file
            ), patch.object(market_ranker, "STATE_FILE", state_file), patch.object(
                market_ranker, "MARKET_RANKER_DATA_DIR", market_dir
            ), patch.object(market_ranker, "DATA_DIR", root), patch.object(
                market_ranker, "utc_now", return_value=current_time
            ), patch.object(
                market_ranker, "maybe_send_ops_alert", return_value=None
            ):
                summary = market_ranker.run_cycle(dry_run=False, session=session)

            self.assertEqual(summary["tokens_checked"], 2)
            self.assertEqual(summary["dex_found"], 2)
            self.assertEqual(summary["dex_batch_calls"], 1)
            self.assertEqual(len(session.urls), 1)
            self.assertIn("/tokens/v1/ethereum/", session.urls[0])
            self.assertIn(f"{token_a},{token_b}", session.urls[0])
            updated = json.loads(watchlist_file.read_text(encoding="utf-8"))
            self.assertEqual(updated[key_a]["token_symbol"], "TEST")
            self.assertEqual(updated[key_a]["token_name"], "Test Token")
            self.assertEqual(updated[key_a]["liquidity_usd"], 5000)
            self.assertEqual(updated[key_a]["volume_h24"], 1000)
            self.assertEqual(updated[key_a]["txns_h24"], 20)
            self.assertEqual(updated[key_a]["minimum_token_age_inferred_minutes"], 4)
            self.assertEqual(updated[key_a]["minimum_token_age_inferred_source"], "oldest_pair")

    def test_market_score_uses_quote_liquidity_and_marks_misleading_liquidity(self):
        current_time = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)
        token_address = "0x1111111111111111111111111111111111111111"
        entry = token_entry(token_address, f"ethereum:{token_address}")
        pair = {
            "chainId": "ethereum",
            "dexId": "uniswap",
            "pairAddress": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "baseToken": {"address": token_address, "symbol": "ZEC", "name": "Zcash"},
            "quoteToken": {
                "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "symbol": "WETH",
                "name": "Wrapped Ether",
            },
            "priceNative": "0.2085",
            "priceUsd": "324.032",
            "pairCreatedAt": int(datetime(2026, 6, 6, 11, 56, 0, tzinfo=timezone.utc).timestamp() * 1000),
            "liquidity": {"usd": 3_402_339_417.01, "base": 10_499_999, "quote": 0.0004143},
            "volume": {"h24": 730.27},
            "txns": {"h24": {"buys": 6, "sells": 3}},
        }

        score, components, metrics = market_ranker.calculate_market_score(
            pair,
            entry,
            current_time,
            weights={"quote_liquidity": 5, "volume_h24": 3, "txns_h24": 4, "minimum_token_age_inferred": 5},
            inferred_age={
                "minimum_token_age_inferred_minutes": 4,
                "minimum_token_age_inferred_source": "oldest_pair",
            },
        )

        self.assertEqual(metrics["market_sanity_status"], "misleading_liquidity")
        self.assertEqual(metrics["quote_liquidity_symbol"], "WETH")
        self.assertLess(metrics["quote_liquidity_usd"], 1)
        self.assertEqual(components["quote_liquidity"], 10)
        self.assertLess(score, 15)

    def test_watchlist_retention_applies_blind_cap_without_removing_protected(self):
        current_time = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watchlist_file = root / "watchlist.json"
            lock_file = root / "watchlist.lock"
            archive_file = root / "archive.jsonl"
            protected_token = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            low_token = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            high_token = "0xcccccccccccccccccccccccccccccccccccccccc"
            watchlist_file.write_text(
                json.dumps(
                    {
                        f"ethereum:{protected_token}": {
                            **token_entry(protected_token, f"ethereum:{protected_token}"),
                            "status": "ativo",
                            "social_eligibility": "eligible",
                            "market_score": 1,
                        },
                        f"ethereum:{low_token}": {
                            **token_entry(low_token, f"ethereum:{low_token}"),
                            "social_eligibility": "pending",
                            "market_score": 5,
                        },
                        f"ethereum:{high_token}": {
                            **token_entry(high_token, f"ethereum:{high_token}"),
                            "social_eligibility": "eligible",
                            "market_score": 90,
                        },
                    }
                ),
                encoding="utf-8",
            )
            config = {
                "market_ranker": {
                    "watchlist_retention": {
                        "enabled": True,
                        "max_entries": 2,
                        "archive_removed": True,
                        "archive_file": str(archive_file),
                        "pending_retention_hours": 999,
                    }
                }
            }

            with patch.object(market_ranker, "WATCHLIST_FILE", watchlist_file), patch.object(
                market_ranker, "WATCHLIST_LOCK_FILE", lock_file
            ):
                summary = market_ranker.apply_watchlist_retention(config, current_time)

            updated = json.loads(watchlist_file.read_text(encoding="utf-8"))
            self.assertEqual(summary["removed"], 1)
            self.assertIn(f"ethereum:{protected_token}", updated)
            self.assertIn(f"ethereum:{high_token}", updated)
            self.assertNotIn(f"ethereum:{low_token}", updated)
            self.assertTrue(archive_file.exists())
            self.assertIn("retention_blind_cap", archive_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
