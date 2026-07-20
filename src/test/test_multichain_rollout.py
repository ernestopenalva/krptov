import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from src.modules import market_ranker, pool_scanner, social_inference
from src.modules.monitor import _select_pair
from src.modules.chain_identity import make_watchlist_key, normalize_token_address
from src.modules.chain_routing import circuit_enabled, load_routing_sections, social_actions
from src.tools.closed_position_report import load_closed_positions


SOL_TOKEN = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"
SOL_QUOTE = "So11111111111111111111111111111111111111112"
SOL_POOL = "9wFFmGphLCQ26G2YGgboT7jHXfNTXLRz7E8QGR7w9a8p"
PUMP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


def base58_encode(payload):
    alphabet = pool_scanner.BASE58_ALPHABET
    number = int.from_bytes(payload, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = alphabet[remainder] + encoded
    zeros = len(payload) - len(payload.lstrip(b"\0"))
    return "1" * zeros + (encoded or "")


class ChainIdentityTests(unittest.TestCase):
    def test_solana_is_case_sensitive_and_evm_is_lowercase(self):
        self.assertEqual(normalize_token_address("solana", SOL_TOKEN), SOL_TOKEN)
        self.assertNotEqual(normalize_token_address("solana", SOL_TOKEN.lower()), SOL_TOKEN)
        self.assertEqual(
            make_watchlist_key("base", "0xABCDEFabcdefABCDEFabcdefABCDEFabcdefABCD"),
            "base:0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
        )


class PumpSwapScannerTests(unittest.TestCase):
    def pumpswap_transaction(self):
        return {
            "transaction": {"message": {"accountKeys": [], "instructions": [{
                "programId": PUMP_PROGRAM,
                "accounts": [SOL_POOL, SOL_TOKEN, SOL_TOKEN, SOL_TOKEN, SOL_QUOTE],
                "data": base58_encode(pool_scanner.PUMPSWAP_CREATE_POOL_DISCRIMINATOR + b"payload"),
            }]}},
            "meta": {"innerInstructions": []},
        }

    def test_pumpswap_create_pool_is_decoded_from_official_account_order(self):
        decoded = pool_scanner.decode_pumpswap_create_pool(self.pumpswap_transaction(), PUMP_PROGRAM)
        self.assertEqual(decoded["pool_address"], SOL_POOL)
        self.assertEqual(decoded["token0"], SOL_TOKEN)
        self.assertEqual(decoded["token1"], SOL_QUOTE)

    def test_pool_sources_enable_only_pumpswap_for_solana(self):
        config = yaml.safe_load((pool_scanner.PROJECT_ROOT / "config" / "pool_sources.yaml").read_text())
        with patch.dict("os.environ", {"ALCHEMY_SOLANA_RPC_URL": "https://example.invalid"}, clear=False):
            config["chains"] = {"solana": config["chains"]["solana"]}
            chain = pool_scanner.build_enabled_chains(config)[0]
        self.assertEqual(chain["family"], "solana")
        self.assertEqual([source["name"] for source in chain["sources"]], ["pumpswap"])

    def test_notification_writes_base58_candidate_to_ranking_buffer(self):
        chain = {"name": "solana", "rpc_url": "https://example.invalid",
                 "quote_tokens": {SOL_QUOTE: "SOL"}}
        source = {"name": "pumpswap", "type": "pumpswap_program", "program_address": PUMP_PROGRAM}
        notification = {"params": {"result": {"context": {"slot": 123}, "value": {
            "signature": "signature", "err": None,
            "logs": ["Program log: Instruction: CreatePool"],
        }}}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(pool_scanner, "DATA_DIR", root), patch.object(
                pool_scanner, "RANKING_BUFFER_FILE", root / "ranking_buffer.json"
            ), patch.object(pool_scanner, "WATCHLIST_LOCK_FILE", root / "watchlist.lock"), patch.object(
                pool_scanner, "fetch_solana_transaction", return_value=self.pumpswap_transaction()
            ), patch.object(pool_scanner, "event_file_path", return_value=root / "events.jsonl"):
                action = pool_scanner.process_solana_notification(chain, source, notification, False)
            buffer = json.loads((root / "ranking_buffer.json").read_text(encoding="utf-8"))
        self.assertEqual(action, "created")
        self.assertIn(f"solana:{SOL_TOKEN}", buffer)
        self.assertEqual(buffer[f"solana:{SOL_TOKEN}"]["pool_address"], SOL_POOL)


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


if __name__ == "__main__":
    unittest.main()
