import json
import tempfile
import unittest
from pathlib import Path

from src.modules import market_ranker, social_inference
from src.modules.monitor import _select_pair
from src.modules.chain_identity import make_watchlist_key, normalize_token_address
from src.modules.chain_routing import circuit_enabled, load_routing_sections, social_actions
from src.tools.closed_position_report import fmt_price, fmt_time, load_closed_positions


SOL_TOKEN = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"
SOL_QUOTE = "So11111111111111111111111111111111111111112"
SOL_POOL = "9wFFmGphLCQ26G2YGgboT7jHXfNTXLRz7E8QGR7w9a8p"


class ChainIdentityTests(unittest.TestCase):
    def test_solana_is_case_sensitive_and_evm_is_lowercase(self):
        self.assertEqual(normalize_token_address("solana", SOL_TOKEN), SOL_TOKEN)
        self.assertNotEqual(normalize_token_address("solana", SOL_TOKEN.lower()), SOL_TOKEN)
        self.assertEqual(
            make_watchlist_key("base", "0xABCDEFabcdefABCDEFabcdefABCDEFabcdefABCD"),
            "base:0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
        )


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.config = load_routing_sections(market_ranker.PROJECT_ROOT / "config" / "config.yaml")

    def test_initial_circuit_rollout(self):
        self.assertTrue(circuit_enabled(self.config, "ethereum", "inference"))
        self.assertFalse(circuit_enabled(self.config, "ethereum", "monitor"))
        self.assertTrue(circuit_enabled(self.config, "solana", "inference"))
        self.assertTrue(circuit_enabled(self.config, "solana", "monitor"))
        ranked = {
            "ethereum:0x1111111111111111111111111111111111111111": {"chain": "ethereum"},
            f"solana:{SOL_TOKEN}": {"chain": "solana"},
        }
        inference = market_ranker.entries_for_circuit(ranked, self.config, "inference")
        monitor = market_ranker.entries_for_circuit(ranked, self.config, "monitor")
        self.assertEqual(set(inference), set(ranked))
        self.assertEqual(set(monitor), {f"solana:{SOL_TOKEN}"})

    def test_solana_authors_go_to_monitor_without_telegram(self):
        self.assertEqual(
            social_actions(self.config, "solana", {"authors"}),
            {"telegram": False, "monitor": True},
        )
        self.assertEqual(
            social_actions(self.config, "solana", {"government_badge"}),
            {"telegram": True, "monitor": True},
        )

    def test_blue_badge_is_not_a_routing_category(self):
        analysis = {
            "alert_reasons": [],
            "best_post_author_followers": 0,
            "min_author_followers_for_alert": 1000,
        }
        self.assertEqual(social_inference.social_signal_categories(analysis), set())


class RankerSolanaTests(unittest.TestCase):
    def test_dexscreener_batch_preserves_base58_address(self):
        class Response:
            def raise_for_status(self): pass
            def json(self): return []

        class Session:
            def __init__(self): self.url = None
            def get(self, url, timeout): self.url = url; return Response()

        session = Session()
        market_ranker.fetch_token_pairs_batch("solana", [SOL_TOKEN], session=session)
        self.assertIn(SOL_TOKEN, session.url)

    def test_monitor_selects_exact_pumpswap_pool_without_changing_case(self):
        other = {"pairAddress": SOL_TOKEN, "liquidity": {"usd": 999}}
        exact = {"pairAddress": SOL_POOL, "liquidity": {"usd": 1}}
        selected = _select_pair({"chain": "solana", "pool_address": SOL_POOL}, [other, exact])
        self.assertIs(selected, exact)


class ClosedPositionReportTests(unittest.TestCase):
    def test_reads_canonical_position_closed_event(self):
        event = {
            "timestamp": "2026-07-19T12:01:00+00:00", "event": "position_closed",
            "exit_reason": "TRAILING_STOP", "pnl_pct": 5,
            "position": {
                "chain": "solana", "token_address": SOL_TOKEN, "symbol": "PUMP",
                "entry_time": "2026-07-19T12:00:00+00:00", "entry_price_usd": 1,
                "min_price_usd": .95, "highest_price_usd": 1.1,
                "source_signal": {"quote_token": "SOL", "admission_source": "social_alert",
                                  "entry_reason": "MOMENTUM_CONTINUATION"},
            },
            "last_tick": {"observed_at": "2026-07-19T12:01:00+00:00", "price_usd": 1.05},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            path.write_text(json.dumps(event) + "\n", encoding="utf-8")
            rows = list(load_closed_positions(path))
        self.assertEqual(rows[0]["token"], "PUMP/SOL")
        self.assertEqual(rows[0]["chain"], "solana")
        self.assertTrue(rows[0]["social"])
        self.assertAlmostEqual(rows[0]["min_pnl_pct"], -5)

    def test_prefers_real_signal_symbol_over_position_address_fallback(self):
        event = {
            "event": "position_closed",
            "position": {
                "chain": "solana",
                "token_address": SOL_TOKEN,
                "symbol": SOL_TOKEN[:8],
                "source_signal": {"token_symbol": "GMEBULL", "quote_token": "SOL"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            path.write_text(json.dumps(event) + "\n", encoding="utf-8")
            rows = list(load_closed_positions(path))
        self.assertEqual(rows[0]["token"], "GMEBULL/SOL")

    def test_formats_utc_time_in_brasilia(self):
        self.assertEqual(fmt_time("2026-07-21T00:00:18+00:00"), "20/07 21:00:18")

    def test_formats_small_prices_without_scientific_notation(self):
        self.assertEqual(fmt_price(5.442159079e-05), "US$0.0₄5442")
        self.assertEqual(fmt_price(0.0006587303791), "US$0.0006587")


if __name__ == "__main__":
    unittest.main()
