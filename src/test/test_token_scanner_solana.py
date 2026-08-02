import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

from src.modules import pool_scanner, token_scanner_solana as scanner


TOKEN = "5wAr8gMm8KpgkGEiAhiBBdKBXFQsyChuLdw4KHuVpump"
WSOL = "So11111111111111111111111111111111111111112"
POOL = "5AUgLVM64n9GnmijM8BXNnGshKNBmzCtaiHybte4Tzce"


def pair(dex="pumpswap", quote=WSOL, liquidity=1, price_change_m5=-90, price_change_h1=0):
    return {
        "chainId": "solana",
        "dexId": dex,
        "pairAddress": POOL,
        "baseToken": {"address": TOKEN, "symbol": "MEOW", "name": "Meowpin"},
        "quoteToken": {"address": quote, "symbol": "SOL", "name": "Wrapped SOL"},
        "pairCreatedAt": 1784516400000,
        "liquidity": {"usd": liquidity},
        "volume": {"h24": 2},
        "txns": {"h24": {"buys": 1, "sells": 0}},
        "priceChange": {"m5": price_change_m5, "h1": price_change_h1},
    }


class FakeProvider:
    def __init__(self, pairs):
        self.pairs = pairs
        self.pair_calls = 0

    def latest_profiles(self):
        return [{"chainId": "solana", "tokenAddress": TOKEN, "url": "profile"}]

    def pairs_for_tokens(self, chain, addresses):
        self.pair_calls += 1
        return list(self.pairs)


def config(root):
    return {
        "enabled": True,
        "chain_id": "solana",
        "allowed_dex_id": "pumpswap",
        "quote_token": "SOL",
        "quote_token_address": WSOL,
        "require_pump_mint_suffix": True,
        "discovery_provider": "dexscreener",
        "emitted_retention_hours": 24,
        "technical_entry_filters": {
            "max_price_change_m5": 20,
            "max_price_change_h1": 200,
        },
        "state_file": root / "scanner" / "state.json",
        "audit_dir": root / "scanner",
        "request_timeout_seconds": 1,
        "jupiter": {
            "quote_url": "https://jupiter.invalid/quote",
            "token_search_url": "https://jupiter.invalid/search",
            "buy_amount_lamports": 10_000_000,
            "sell_amount_raw": 1_000_000,
            "slippage_bps": 100,
        },
    }


def unavailable_jupiter(observed_at):
    return {
        "summary": {
            "observed_at_utc": observed_at,
            "available": False,
            "jupiter_buy_quote_ok": False,
            "jupiter_sell_quote_ok": False,
            "mint_authority_ok": None,
            "freeze_authority_ok": None,
            "approved_by_jupiter": False,
            "holder_count": None,
            "top_holders_percentage": None,
            "organic_score": None,
            "organic_score_label": None,
            "num_traders_1h": None,
            "buy_price_impact_pct": None,
            "sell_price_impact_pct": None,
        },
        "raw": {"error": "unavailable"},
    }


class TokenScannerSolanaTests(unittest.TestCase):
    def runtime_patches(self, root):
        return patch.multiple(
            scanner,
            DATA_DIR=root,
            RANKING_BUFFER_FILE=root / "ranking_buffer.json",
            WATCHLIST_FILE=root / "watchlist.json",
            MONITOR_WATCHLIST_FILE=root / "monitor_watchlist.json",
            WATCHLIST_LOCK_FILE=root / "watchlist.lock",
        )

    def test_low_metrics_and_unavailable_jupiter_do_not_block_buffer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeProvider([pair(dex="raydium", liquidity=999999), pair(liquidity=1)])
            observed_at = "2026-07-20T12:00:00Z"
            with self.runtime_patches(root), patch.object(
                scanner, "observe_jupiter", return_value=unavailable_jupiter(observed_at)
            ):
                result = scanner.run_scanner_cycle(
                    config(root), provider=provider,
                    current_time=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
                )
            buffer = json.loads((root / "ranking_buffer.json").read_text(encoding="utf-8"))
            entry = buffer[f"solana:{TOKEN}"]
            self.assertEqual(result["emitted"], 1)
            self.assertEqual(entry["pool_address"], POOL)
            self.assertEqual(entry["discovery_market_snapshot"]["liquidity_usd"], 1)
            self.assertFalse(entry["jupiter_observation"]["available"])
            self.assertFalse((root / "watchlist.json").exists())
            self.assertFalse((root / "monitor_watchlist.json").exists())

    def test_emitted_token_is_not_enriched_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = FakeProvider([pair()])
            with self.runtime_patches(root), patch.object(
                scanner, "observe_jupiter", return_value=unavailable_jupiter("2026-07-20T12:00:00Z")
            ):
                scanner.run_scanner_cycle(config(root), provider=first)
                second = FakeProvider([pair()])
                result = scanner.run_scanner_cycle(config(root), provider=second)
            self.assertEqual(result["new_profiles"], 0)
            self.assertEqual(second.pair_calls, 0)

    def test_exhausted_token_is_emitted_as_social_only_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeProvider([pair(price_change_m5=41.23, price_change_h1=170)])
            with self.runtime_patches(root), patch.object(
                scanner, "observe_jupiter", return_value=unavailable_jupiter("2026-07-20T12:00:00Z")
            ):
                result = scanner.run_scanner_cycle(config(root), provider=provider)
            buffer = json.loads((root / "ranking_buffer.json").read_text(encoding="utf-8"))
            entry = buffer[f"solana:{TOKEN}"]
            self.assertEqual(result["emitted"], 1)
            self.assertEqual(result["technical_social_only"], 1)
            self.assertEqual(entry["technical_eligibility"], "blocked_exhaustion")
            self.assertEqual(entry["technical_eligibility_reason"], "price_change_m5_above_max")
            self.assertEqual(entry["technical_filter_snapshot"]["price_change_m5"], 41.23)

    def test_h1_exhaustion_is_independently_blocked(self):
        eligibility = scanner.technical_eligibility(
            pair(price_change_m5=10, price_change_h1=201),
            config(Path("unused")),
            "2026-07-20T12:00:00Z",
        )
        self.assertEqual(eligibility["technical_eligibility"], "blocked_exhaustion")
        self.assertEqual(eligibility["technical_eligibility_reason"], "price_change_h1_above_max")

    def test_token_without_pumpswap_can_be_reconsidered_next_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.runtime_patches(root), patch.object(
                scanner, "observe_jupiter", return_value=unavailable_jupiter("2026-07-20T12:00:00Z")
            ):
                waiting = scanner.run_scanner_cycle(config(root), provider=FakeProvider([pair("pumpfun")]))
                emitted = scanner.run_scanner_cycle(config(root), provider=FakeProvider([pair()]))
            self.assertEqual(waiting["waiting_pumpswap"], 1)
            self.assertEqual(emitted["emitted"], 1)

    def test_dexscreener_observer_never_writes_ranking_buffer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.runtime_patches(root):
                result = scanner.run_dexscreener_observer_cycle(
                    config(root), provider=FakeProvider([pair()]),
                    current_time=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
                )
            audit = (root / "scanner" / "dexscreener_observations_2026-07-20.jsonl")
            self.assertEqual(result["would_capture"], 1)
            self.assertTrue(audit.exists())
            self.assertFalse((root / "ranking_buffer.json").exists())
            self.assertFalse((root / "watchlist.json").exists())

    def test_pumpportal_migration_admits_without_dex_market_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event = {"mint": TOKEN, "pool": POOL, "signature": "sig", "symbol": "MEOW"}
            with self.runtime_patches(root), patch.object(
                scanner, "observe_jupiter", return_value=unavailable_jupiter("2026-07-20T12:00:00Z")
            ):
                result = scanner.process_pumpportal_migration(
                    event, config(root), current_time=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
                )
            buffer = json.loads((root / "ranking_buffer.json").read_text(encoding="utf-8"))
            entry = buffer[f"solana:{TOKEN}"]
            self.assertEqual(result["action"], "created")
            self.assertEqual(entry["discovery_provider"], "pumpportal")
            self.assertEqual(entry["technical_eligibility"], "not_evaluated_early_source")
            self.assertEqual(entry["discovery_event_signature"], "sig")

    def test_pumpportal_raydium_migration_is_not_admitted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event = {"mint": TOKEN, "pool": "raydium"}
            with self.runtime_patches(root):
                result = scanner.process_pumpportal_migration(event, config(root))
            self.assertEqual(result["action"], "ignored_non_pumpswap_migration")
            self.assertFalse((root / "ranking_buffer.json").exists())

    def test_pool_scanner_configuration_contains_only_evm_chains(self):
        raw = yaml.safe_load((pool_scanner.PROJECT_ROOT / "config" / "pool_sources.yaml").read_text())
        self.assertNotIn("solana", raw["chains"])
        self.assertTrue(all(item.get("family", "evm") == "evm" for item in raw["chains"].values()))


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class JupiterSession:
    def get(self, url, params=None, headers=None, timeout=None):
        if url.endswith("/search"):
            return Response([{
                "id": TOKEN, "symbol": "MEOW", "holderCount": 42,
                "mintAuthority": None, "freezeAuthority": None,
                "audit": {"topHoldersPercentage": 73.5},
                "organicScore": 7, "organicScoreLabel": "low",
                "stats1h": {"numTraders": 3},
            }])
        impact = "0.12" if params["inputMint"] == WSOL else "0.34"
        return Response({"routePlan": [{"swapInfo": {}}], "priceImpactPct": impact})


class JupiterObservationTests(unittest.TestCase):
    def test_preserves_k3_metrics_without_turning_them_into_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = scanner.observe_jupiter(
                TOKEN, config(Path(directory)), JupiterSession(), "2026-07-20T12:00:00Z"
            )["summary"]
        self.assertEqual(summary["holder_count"], 42)
        self.assertEqual(summary["top_holders_percentage"], 73.5)
        self.assertEqual(summary["organic_score"], 7)
        self.assertEqual(summary["num_traders_1h"], 3)
        self.assertEqual(summary["buy_price_impact_pct"], 0.12)
        self.assertEqual(summary["sell_price_impact_pct"], 0.34)
        self.assertTrue(summary["approved_by_jupiter"])


if __name__ == "__main__":
    unittest.main()
