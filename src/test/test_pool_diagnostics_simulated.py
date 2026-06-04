import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

if "requests" not in sys.modules:
    requests = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    requests.RequestException = RequestException
    sys.modules["requests"] = requests

spec = importlib.util.spec_from_file_location(
    "pool_diagnostics_simulated",
    PROJECT_ROOT / "src" / "tools" / "pool_diagnostics.py",
)
pool_diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pool_diagnostics)


WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
TOKEN = "0x1111111111111111111111111111111111111111"
POOL = "0x2222222222222222222222222222222222222222"
POOL_ID = "0x" + ("ab" * 32)


def make_scanner_event(pool_address=POOL, pool_id=None, source="uniswap_v3"):
    return {
        "received_at_utc": "2026-06-01T12:00:02Z",
        "chain": "ethereum",
        "source": source,
        "source_type": "pool_initialized" if pool_id else "pool_created",
        "pool_manager_address": (
            "0x000000000004444c5dc75cb358380d2e3de08a90" if pool_id else None
        ),
        "decoded_event": {
            "pool_address": pool_address,
            "pool_id": pool_id,
        },
        "candidate": {
            "token_address": TOKEN,
            "quote_token": "WETH",
            "quote_token_address": WETH,
        },
        "raw_log": {
            "blockTimestamp": "0x683da020",
        },
    }


def make_pair(pair_address=POOL, pair_created_at=1748779200000, liquidity=1234.5):
    return {
        "chainId": "ethereum",
        "dexId": "uniswap",
        "pairAddress": pair_address,
        "baseToken": {"address": TOKEN},
        "quoteToken": {"address": WETH},
        "liquidity": {"usd": liquidity},
        "volume": {"m5": 10, "h1": 20, "h24": 30},
        "txns": {
            "m5": {"buys": 1, "sells": 2},
            "h1": {"buys": 3, "sells": 4},
            "h24": {"buys": 5, "sells": 6},
        },
        "pairCreatedAt": pair_created_at,
    }


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.urls = []

    def get(self, url, timeout):
        self.urls.append((url, timeout))
        if self.error:
            raise self.error
        return FakeResponse(self.payload)


class PoolDiagnosticsSimulatedTests(unittest.TestCase):
    def setUp(self):
        self.saved_observations = []
        self.original_save_observation = pool_diagnostics.save_observation
        pool_diagnostics.save_observation = self.saved_observations.append

    def tearDown(self):
        pool_diagnostics.save_observation = self.original_save_observation

    def test_build_task_uses_alchemy_block_timestamp_and_exact_pool(self):
        task = pool_diagnostics.build_task(make_scanner_event())

        self.assertEqual(task["task_id"], f"ethereum:{POOL}")
        self.assertEqual(task["pool_created_at_source"], "alchemy_block_timestamp")
        self.assertEqual(task["lookup_mode"], "exact_pool_address")
        self.assertEqual(task["association_precision"], "exact_pool")

    def test_fetch_exact_pool_uses_pair_endpoint(self):
        task = pool_diagnostics.build_task(make_scanner_event())
        session = FakeSession({"pairs": [make_pair()]})

        pair = pool_diagnostics.fetch_dexscreener_pair(task, session=session)

        self.assertIn(f"/pairs/ethereum/{POOL}", session.urls[0][0])
        self.assertEqual(pair["pair_address"], POOL)
        self.assertEqual(pair["liquidity_usd"], 1234.5)
        self.assertEqual(pair["buys_h24"] + pair["sells_h24"], 11)

    def test_v4_uses_token_endpoint_and_marks_lower_precision(self):
        task = pool_diagnostics.build_task(
            make_scanner_event(pool_address=None, pool_id=POOL_ID, source="uniswap_v4")
        )
        session = FakeSession([make_pair(pair_address="0x" + ("33" * 20))])

        pair = pool_diagnostics.fetch_dexscreener_pair(task, session=session)

        self.assertEqual(task["lookup_mode"], "token_address_fallback")
        self.assertEqual(task["association_precision"], "token_level")
        self.assertIn(f"/token-pairs/v1/ethereum/{TOKEN}", session.urls[0][0])
        self.assertIsNotNone(pair)

    def test_poll_records_first_seen_and_due_snapshot(self):
        task = pool_diagnostics.build_task(make_scanner_event())
        task["pool_created_at_utc"] = "2026-06-01T12:00:00Z"
        now = datetime(2026, 6, 1, 12, 5, 3, tzinfo=timezone.utc)

        pool_diagnostics.poll_task(
            task,
            snapshot_minutes=[5, 15, 30],
            session=FakeSession({"pairs": [make_pair()]}),
            now=now,
        )

        self.assertEqual(task["first_seen_delay_seconds"], 303)
        self.assertIn("5", task["snapshots"])
        self.assertEqual(
            [item["observation_type"] for item in self.saved_observations],
            ["first_seen", "snapshot"],
        )

    def test_network_error_does_not_consume_due_snapshot(self):
        task = pool_diagnostics.build_task(make_scanner_event())
        task["pool_created_at_utc"] = "2026-06-01T12:00:00Z"
        error = pool_diagnostics.requests.RequestException("offline")

        pool_diagnostics.poll_task(
            task,
            snapshot_minutes=[5, 15, 30],
            session=FakeSession(error=error),
            now=datetime(2026, 6, 1, 12, 5, 3, tzinfo=timezone.utc),
        )

        self.assertNotIn("5", task["snapshots"])
        self.assertEqual(self.saved_observations[0]["observation_type"], "poll_error")

    def test_ingest_streams_reads_appended_events_incrementally(self):
        original_data_dir = pool_diagnostics.POOL_SCANNER_DATA_DIR
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                pool_diagnostics.POOL_SCANNER_DATA_DIR = Path(temporary_directory)
                stream_file = pool_diagnostics.POOL_SCANNER_DATA_DIR / "events_2026-06-01.jsonl"
                first_event = make_scanner_event()
                second_event = make_scanner_event(
                    pool_address="0x3333333333333333333333333333333333333333"
                )
                stream_file.write_text(json.dumps(first_event) + "\n", encoding="utf-8")
                state = pool_diagnostics.create_state([5, 15, 30], 45)

                pool_diagnostics.ingest_streams(
                    state,
                    fresh_state=True,
                    lookback_seconds=10**9,
                )
                with stream_file.open("a", encoding="utf-8") as file:
                    file.write(json.dumps(second_event) + "\n")
                pool_diagnostics.ingest_streams(state)

                self.assertEqual(len(state["tasks"]), 2)
        finally:
            pool_diagnostics.POOL_SCANNER_DATA_DIR = original_data_dir

    def test_build_state_from_observations_recovers_empty_state_report(self):
        original_data_dir = pool_diagnostics.DIAGNOSTICS_DATA_DIR
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                pool_diagnostics.DIAGNOSTICS_DATA_DIR = Path(temporary_directory)
                observation_file = (
                    pool_diagnostics.DIAGNOSTICS_DATA_DIR
                    / "observations_2026-06-01.jsonl"
                )
                first_seen = {
                    "observed_at_utc": "2026-06-01T12:01:02Z",
                    "observation_type": "first_seen",
                    "target_age_minutes": None,
                    "task_id": f"ethereum:{POOL}",
                    "chain": "ethereum",
                    "source": "uniswap_v3",
                    "token_address": TOKEN,
                    "quote_token": "WETH",
                    "quote_token_address": WETH,
                    "pool_address": POOL,
                    "pool_id": None,
                    "lookup_mode": "exact_pool_address",
                    "association_precision": "exact_pool",
                    "pool_created_at_utc": "2026-06-01T12:00:00Z",
                    "age_seconds": 62,
                    "found_on_dexscreener": True,
                    "error": None,
                    "pair": make_pair(),
                }
                snapshot = dict(first_seen)
                snapshot.update(
                    {
                        "observed_at_utc": "2026-06-01T12:05:03Z",
                        "observation_type": "snapshot",
                        "target_age_minutes": 5,
                        "age_seconds": 303,
                    }
                )
                observation_file.write_text(
                    json.dumps(first_seen) + "\n" + json.dumps(snapshot) + "\n",
                    encoding="utf-8",
                )

                state = pool_diagnostics.build_state_from_observations([5, 15, 30], 45)

                self.assertEqual(state["report_source"], "observations_recovered")
                self.assertEqual(len(state["tasks"]), 1)
                task = state["tasks"][f"ethereum:{POOL}"]
                self.assertEqual(task["first_seen_delay_seconds"], 62)
                self.assertIn("5", task["snapshots"])
        finally:
            pool_diagnostics.DIAGNOSTICS_DATA_DIR = original_data_dir


if __name__ == "__main__":
    unittest.main()
