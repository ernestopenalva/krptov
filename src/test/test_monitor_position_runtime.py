import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.modules import monitor_watchlist as store
from src.modules.monitor import MonitorConfig, evaluate_entry, evaluate_momentum
from src.market_data.evm_provider import (
    EvmPoolProvider,
    SELECTOR_DECIMALS,
    SELECTOR_GET_RESERVES,
    SELECTOR_LIQUIDITY,
    SELECTOR_SLOT0,
    SELECTOR_TOKEN0,
    SELECTOR_TOKEN1,
)
from src.market_data.types import MarketContext
from src.market_data.types import LiquidityObservation, MarketTick
from src.modules import position as position_module
from src.modules.position import PositionSupervisor
from src.position.engine import PositionEngine, PositionEngineConfig


def tick(at, price):
    return {"observed_at": at.isoformat(), "price_usd": price, "price_quote": price,
            "liquidity": {"model": "test", "status": "ok"}}


def signal():
    return {"watchlist_key": "base:0xabc", "chain": "base", "source": "uniswap_v2",
            "token_address": "0xabc", "quote_token_address": "0xquote",
            "pool_address": "0xpool", "symbol": "ABC", "signal_price_usd": 100}


class PositionEngineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = PositionEngineConfig()
        self.engine = PositionEngine(self.cfg)
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.state = self.engine.open(position_id="p1", signal=signal(), tick=tick(self.start, 100))

    def test_stop_loss_requires_continuous_three_seconds(self):
        self.assertFalse(self.engine.update(self.state, tick(self.start, 94))["closed"])
        self.assertFalse(self.engine.update(self.state, tick(self.start + timedelta(seconds=2), 96))["closed"])
        self.assertFalse(self.engine.update(self.state, tick(self.start + timedelta(seconds=3), 94))["closed"])
        result = self.engine.update(self.state, tick(self.start + timedelta(seconds=6), 94))
        self.assertTrue(result["closed"])
        self.assertEqual(result["exit_reason"], "STOP_LOSS")

    def test_profit_ladder_locks_one_three_five_percent(self):
        self.engine.update(self.state, tick(self.start + timedelta(seconds=1), 105))
        self.assertAlmostEqual(self.state.stop_price, 101)
        self.engine.update(self.state, tick(self.start + timedelta(seconds=2), 106))
        self.assertAlmostEqual(self.state.stop_price, 103)
        self.engine.update(self.state, tick(self.start + timedelta(seconds=3), 110))
        self.assertAlmostEqual(self.state.stop_price, 105)
        result = self.engine.update(self.state, tick(self.start + timedelta(seconds=4), 104.9))
        self.assertEqual(result["exit_reason"], "BREAKEVEN_STOP")

    def test_stop_loss_reason_precedes_breakeven_and_trailing(self):
        self.state.breakeven_activated = True
        self.state.stop_price = 101
        self.state.trailing_stop_price = 102
        result = self.engine.update(self.state, tick(self.start + timedelta(seconds=1), 94))
        self.assertEqual(result["exit_reason"], "STOP_LOSS")

    def test_trailing_uses_abb_and_persistence(self):
        self.engine.update(self.state, tick(self.start + timedelta(seconds=1), 104))
        self.assertAlmostEqual(self.state.trailing_stop_price, 99.84)
        first = self.engine.update(self.state, tick(self.start + timedelta(seconds=2), 97.5))
        self.assertFalse(first["closed"])
        final = self.engine.update(self.state, tick(self.start + timedelta(seconds=5), 97.5))
        self.assertEqual(final["exit_reason"], "TRAILING_STOP")


class FakeRegistry:
    def __init__(self, ticks):
        self.ticks = list(ticks)

    def tick(self, _signal):
        return self.ticks.pop(0) if len(self.ticks) > 1 else self.ticks[0]


def market_tick(status="ok", price=100, reason=None):
    return MarketTick("2026-01-01T00:00:00+00:00", "test", status, reason, "base", "uniswap_v2",
                      "0xpool", None, "0xabc", "0xquote",
                      price if status == "ok" else None, 1 if status == "ok" else None,
                      price if status == "ok" else None,
                      liquidity=LiquidityObservation("test", status))


class PositionSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_limit_applies_only_to_configured_chain(self):
        config = {"position": {"mode": "paper", "entry_tick_wait_seconds": .1,
                               "poll_interval_seconds": .01,
                               "max_active_positions_by_chain": {"solana": 1}}}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            position_module, "TRADING_HISTORY_FILE", Path(directory) / "history.jsonl"
        ), patch.object(position_module, "LIVE_DIR", Path(directory) / "live"), patch.object(
            position_module, "HISTORY_DIR", Path(directory) / "position-history"
        ):
            supervisor = PositionSupervisor(config, registry=FakeRegistry([market_tick(price=100)]))
            solana_signal = {**signal(), "watchlist_key": "solana:mint-1", "chain": "solana"}
            first_position = await supervisor.open_position(solana_signal)
            self.assertIsNotNone(first_position)

            second_signal = {**solana_signal, "watchlist_key": "solana:mint-2"}
            self.assertIsNone(await supervisor.open_position(second_signal))

            evm_position = await supervisor.open_position(signal())
            self.assertIsNotNone(evm_position)
            self.assertEqual(supervisor.status()["active_positions_by_chain"], {"solana": 1, "base": 1})
            self.assertEqual(supervisor.status()["position_capacity_by_chain"], {"solana": 1})
            history = (Path(directory) / "history.jsonl").read_text(encoding="utf-8")
            self.assertIn("position_capacity_reached", history)
            await supervisor.stop_now()

    async def test_waits_for_first_reliable_onchain_tick(self):
        config = {"position": {"mode": "paper", "entry_tick_wait_seconds": .2,
                               "poll_interval_seconds": .01, "max_entry_divergence_pct": 10}}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            position_module, "TRADING_HISTORY_FILE", Path(directory) / "trading.jsonl"
        ), patch.object(position_module, "LIVE_DIR", Path(directory) / "live"), patch.object(
            position_module, "HISTORY_DIR", Path(directory) / "history"
        ):
            supervisor = PositionSupervisor(config, registry=FakeRegistry([
                market_tick("unavailable", reason="rpc warming"), market_tick(price=100),
            ]))
            supervisor.ops_alerter.send = lambda *_args: True
            position_id = await supervisor.open_position(signal())
            self.assertIsNotNone(position_id)
            self.assertEqual(supervisor.states[position_id].entry_price_usd, 100)
            await supervisor.stop_now()

    async def test_rejects_entry_divergence_instead_of_only_auditing_it(self):
        config = {"position": {"mode": "paper", "entry_tick_wait_seconds": .1,
                               "poll_interval_seconds": .01, "max_entry_divergence_pct": 5}}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            position_module, "TRADING_HISTORY_FILE", Path(directory) / "history.jsonl"
        ):
            supervisor = PositionSupervisor(config, registry=FakeRegistry([market_tick(price=110)]))
            position_id = await supervisor.open_position(signal())
            self.assertIsNone(position_id)
            self.assertFalse(supervisor.states)
            history = (Path(directory) / "history.jsonl").read_text(encoding="utf-8")
            self.assertIn("max_entry_divergence_exceeded", history)


class MonitorStrategyTests(unittest.TestCase):
    @staticmethod
    def history(prices):
        return [{"price_usd": price, "volume_m5": 10000, "liquidity_usd": 20000,
                 "buy_pressure": .60, "buys_m5": 60, "sells_m5": 40} for price in prices]

    def test_momentum_at_four_percent_can_enter(self):
        result = evaluate_momentum(self.history([100, 102, 104]), MonitorConfig())
        self.assertTrue(result["entry"])
        self.assertEqual(result["entry_reason"], "MOMENTUM_CONTINUATION")

    def test_extended_momentum_is_blocked_with_official_reason(self):
        cfg = MonitorConfig(momentum_min_ticks=3, momentum_min_pct=4, momentum_max_runup_pct=12)
        history = []
        for price in (1.0, 1.08, 1.13):
            history.append({"price_usd": price, "volume_m5": 100, "liquidity_usd": 1000,
                            "buy_pressure": .7, "buys_m5": 7, "sells_m5": 3})
        result = evaluate_momentum(history, cfg)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["block_reason"], "MC_RUNUP_TOO_EXTENDED")

    def test_pullback_path_is_not_blocked_by_momentum_cap(self):
        prices = [100, 104, 108, 110, 107, 106, 105, 104, 105, 106, 106, 107]
        result = evaluate_entry(self.history(prices), MonitorConfig())
        self.assertTrue(result["entry"])
        self.assertEqual(result["entry_reason"], "PULLBACK_RECOVERY")

    def test_campaign_peak_from_previous_attempt_blocks_fresh_local_momentum(self):
        history = self.history([100, 102, 105])
        result = evaluate_momentum(
            history,
            MonitorConfig(),
            campaign_first_price=100,
            campaign_peak_price=150,
        )
        self.assertFalse(result["entry"])
        self.assertEqual(result["reason"], "momentum longe do topo")
        self.assertEqual(result["metrics"]["campaign_peak_price_usd"], 150)


class FakeEvmRpc:
    def __init__(self, calls):
        self.calls = calls

    def eth_call(self, address, selector):
        return self.calls[(address.lower(), selector)]

    def block_number(self):
        return 123


def encoded(*words):
    return "0x" + "".join(f"{word:064x}" for word in words)


class EvmProviderTests(unittest.TestCase):
    token0 = "0x0000000000000000000000000000000000000001"
    token1 = "0x0000000000000000000000000000000000000002"
    pool = "0x0000000000000000000000000000000000000003"

    def context(self):
        return MarketContext("base", "uniswap_v2", self.token0, self.token1, self.pool,
                             symbol="TKN", quote_symbol="USDC")

    def common_calls(self):
        return {
            (self.pool, SELECTOR_TOKEN0): encoded(int(self.token0, 16)),
            (self.pool, SELECTOR_TOKEN1): encoded(int(self.token1, 16)),
            (self.token0, SELECTOR_DECIMALS): encoded(18),
            (self.token1, SELECTOR_DECIMALS): encoded(6),
        }

    def test_v2_records_quote_reserve_as_required_liquidity(self):
        calls = self.common_calls()
        calls[(self.pool, SELECTOR_GET_RESERVES)] = encoded(2 * 10**18, 10 * 10**6, 0)
        provider = EvmPoolProvider("unused", lambda _context: {"value": 1, "source": "test", "age_seconds": 0}, model="v2")
        provider.rpc = FakeEvmRpc(calls)
        tick = provider.get_tick(self.context())
        self.assertEqual(tick.status, "ok")
        self.assertEqual(tick.price_usd, 5)
        self.assertEqual(tick.liquidity.quote_reserve, 10)
        self.assertEqual(tick.liquidity.model, "constant_product_reserves")

    def test_v3_does_not_mislabel_active_liquidity_as_reserve(self):
        calls = self.common_calls()
        # sqrt ratio chosen so the decimal-adjusted token1/token0 price is 1.
        sqrt_price = int((2**96) / (10**6))
        calls[(self.pool, SELECTOR_SLOT0)] = encoded(sqrt_price, 0, 0, 0, 0, 0, 0)
        calls[(self.pool, SELECTOR_LIQUIDITY)] = encoded(999)
        provider = EvmPoolProvider("unused", lambda _context: {"value": 1, "source": "test", "age_seconds": 0}, model="v3")
        provider.rpc = FakeEvmRpc(calls)
        tick = provider.get_tick(self.context())
        self.assertEqual(tick.status, "ok")
        self.assertIsNone(tick.liquidity.quote_reserve)
        self.assertEqual(tick.liquidity.active_liquidity, 999)

    def test_aerodrome_stable_uses_curve_marginal_price(self):
        calls = self.common_calls()
        # 2 tokens against 10 quote units: reserve ratio would be 5, but the
        # stable-curve marginal price is intentionally different.
        calls[(self.pool, SELECTOR_GET_RESERVES)] = encoded(2 * 10**18, 10 * 10**6, 0)
        provider = EvmPoolProvider("unused", lambda _context: {"value": 1, "source": "test", "age_seconds": 0},
                                   model="aerodrome_stable")
        provider.rpc = FakeEvmRpc(calls)
        result = provider.get_tick(self.context())
        expected = (3 * 2 * 2 * 10 + 10**3) / (2**3 + 3 * 10 * 10 * 2)
        self.assertAlmostEqual(result.price_quote, expected)
        self.assertNotEqual(result.price_quote, 5)
        self.assertEqual(result.liquidity.model, "aerodrome_stable_curve_reserves")


class MonitorWatchlistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.path_patch = patch.object(store, "MONITOR_WATCHLIST_FILE", root / "monitor_watchlist.json")
        self.lock_patch = patch.object(store, "MONITOR_WATCHLIST_LOCK_FILE", root / "monitor_watchlist.lock")
        self.data_patch = patch.object(store, "DATA_DIR", root)
        self.path_patch.start(); self.lock_patch.start(); self.data_patch.start()

    def tearDown(self):
        self.data_patch.stop(); self.lock_patch.stop(); self.path_patch.stop(); self.temp.cleanup()

    def test_rank_refresh_preserves_runtime_and_social_bypass(self):
        store.sync_ranked_watchlist([("base:a", {"watchlist_key": "base:a", "market_score": 10})])
        store.admit_social_alert("base:a", {"watchlist_key": "base:a"}, {"posts_found": 2})
        store.mutate_entry("base:a", {"monitor_attempts": 1, "monitor_status": "cooldown"})
        store.sync_ranked_watchlist([("base:a", {"watchlist_key": "base:a", "market_score": 99})])
        entry = store.load_monitor_watchlist()["base:a"]
        self.assertEqual(entry["market_score"], 99)
        self.assertEqual(entry["monitor_attempts"], 1)
        self.assertTrue(entry["rank_bypass"])

    def test_monitor_projection_never_carries_social_status(self):
        source = {
            "watchlist_key": "solana:a",
            "status": "ativo",
            "social_status": "ativo",
            "social_monitoring_started_at": "2026-01-01T00:00:00+00:00",
            "social_monitoring_expires_at": "2026-01-01T02:00:00+00:00",
            "market_score": 90,
        }
        store.sync_ranked_watchlist([("solana:a", source)])
        entry = store.load_monitor_watchlist()["solana:a"]
        self.assertNotIn("status", entry)
        self.assertNotIn("social_status", entry)
        self.assertNotIn("social_monitoring_started_at", entry)
        self.assertNotIn("social_monitoring_expires_at", entry)

        store.mutate_entry("solana:a", {
            "status": "ativo",
            "monitor_status": "cooldown",
            "rank_bypass": True,
            "social_ready_at_utc": "2026-01-01T00:00:00+00:00",
            "social_alert_snapshot": {"posts_found": 2},
        })
        store.sync_ranked_watchlist([])
        carried = store.load_monitor_watchlist()["solana:a"]
        self.assertNotIn("status", carried)
        self.assertEqual(carried["monitor_status"], "cooldown")
        self.assertEqual(carried["social_ready_at_utc"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(carried["social_alert_snapshot"], {"posts_found": 2})

    def test_campaign_prices_survive_cooldown_and_rank_refresh(self):
        row = {"watchlist_key": "base:campaign", "market_score": 50}
        store.sync_ranked_watchlist([("base:campaign", row)])
        store.mutate_entry("base:campaign", {
            "campaign_first_price_usd": 100,
            "campaign_first_price_at_utc": "2026-01-01T00:00:00+00:00",
            "campaign_peak_price_usd": 150,
            "campaign_peak_price_at_utc": "2026-01-01T00:01:00+00:00",
        })
        store.finish_monitor_attempt(
            "base:campaign",
            {"outcome": "no_buy", "reason": "monitor_timeout"},
            max_attempts=3,
            cooldown_minutes=15,
            now=datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
        )
        store.sync_ranked_watchlist([("base:campaign", {**row, "market_score": 60})])
        entry = store.load_monitor_watchlist()["base:campaign"]
        self.assertEqual(entry["campaign_first_price_usd"], 100)
        self.assertEqual(entry["campaign_peak_price_usd"], 150)
        reserved = store.reserve_next_candidate(
            active_keys=set(), active_social=0, max_social=2, max_attempts=3,
            now=datetime(2026, 1, 1, 0, 17, tzinfo=timezone.utc),
        )
        self.assertEqual(reserved[1]["campaign_first_price_usd"], 100)
        self.assertEqual(reserved[1]["campaign_peak_price_usd"], 150)

    def test_social_alert_redeems_technical_exhaustion_with_audit_fields(self):
        row = {
            "watchlist_key": "solana:blocked",
            "chain": "solana",
            "technical_eligibility": "blocked_exhaustion",
            "technical_eligibility_reason": "price_change_m5_above_max",
        }
        admitted = store.admit_social_alert(
            "solana:blocked", row, {"alert_reasons": ["authors"]},
            "2026-01-01T00:00:00+00:00",
        )
        self.assertTrue(admitted)
        entry = store.load_monitor_watchlist()["solana:blocked"]
        self.assertTrue(entry["rank_bypass"])
        self.assertEqual(entry["admission_source"], "social_alert")
        self.assertEqual(entry["technical_admission_override"], "social_alert")
        self.assertEqual(entry["technical_redemption_original_eligibility"], "blocked_exhaustion")
        self.assertEqual(entry["technical_redemption_original_reason"], "price_change_m5_above_max")

    def test_social_fifo_precedes_technical_and_respects_cap(self):
        store.sync_ranked_watchlist([("base:t", {"watchlist_key": "base:t", "market_score": 99})])
        store.admit_social_alert("base:s", {"watchlist_key": "base:s", "market_score": 1}, {"posts_found": 1},
                                 "2026-01-01T00:00:00+00:00")
        selected = store.reserve_next_candidate(active_keys=set(), active_social=0, max_social=2, max_attempts=3)
        self.assertEqual(selected[0], "base:s")
        store.mutate_entry("base:s", {"monitor_status": "monitoring"})
        selected = store.reserve_next_candidate(active_keys={"base:s"}, active_social=2, max_social=2, max_attempts=3)
        self.assertEqual(selected[0], "base:t")

    def test_social_vacancy_does_not_make_technical_retry_miss_its_rank_call(self):
        store.sync_ranked_watchlist([
            ("base:t1", {"watchlist_key": "base:t1", "market_score": 99}),
            ("base:t2", {"watchlist_key": "base:t2", "market_score": 90}),
        ])
        past = "2026-01-01T00:00:00+00:00"
        for key in ("base:t1", "base:t2"):
            store.mutate_entry(key, {"monitor_status": "cooldown", "monitor_attempts": 1,
                                     "monitor_cooldown_until_utc": past})
        store.admit_social_alert("base:s", {"watchlist_key": "base:s"}, {"posts_found": 1}, past)
        social = store.reserve_next_candidate(active_keys=set(), active_social=0, max_social=2, max_attempts=3)
        self.assertEqual(social[0], "base:s")
        after_social = store.load_monitor_watchlist()
        self.assertEqual(after_social["base:t2"]["monitor_status"], "eligible")
        technical = store.reserve_next_candidate(active_keys={"base:s"}, active_social=1, max_social=2, max_attempts=3)
        self.assertEqual(technical[0], "base:t1")
        completed = dict(store.pop_completed_entries())
        self.assertEqual(completed["base:t2"]["monitor_last_reason"], "missed_live_rank_reentry")

    def test_completed_campaign_cannot_be_recreated_by_next_rank_cycle(self):
        row = {"watchlist_key": "base:done", "market_score": 50}
        store.sync_ranked_watchlist([("base:done", row)])
        store.mutate_entry("base:done", {"monitor_status": "completed", "monitor_attempts": 3,
                                         "monitor_last_reason": "monitor_timeout"})
        store.pop_completed_entries()
        store.sync_ranked_watchlist([("base:done", {**row, "market_score": 100})])
        self.assertNotIn("base:done", store.load_monitor_watchlist())
        self.assertFalse(store.admit_social_alert("base:done", row, {"posts_found": 5}))


if __name__ == "__main__":
    unittest.main()
