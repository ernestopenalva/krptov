import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

if "websockets" not in sys.modules:
    sys.modules["websockets"] = types.ModuleType("websockets")
if "yaml" not in sys.modules:
    yaml = types.ModuleType("yaml")
    yaml.safe_load = lambda _file: {}
    sys.modules["yaml"] = yaml
if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *_args, **_kwargs: None
    sys.modules["dotenv"] = dotenv

spec = importlib.util.spec_from_file_location(
    "pool_scanner_simulated",
    PROJECT_ROOT / "src" / "modules" / "pool_scanner.py",
)
pool_scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pool_scanner)


WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
ETH = "0x0000000000000000000000000000000000000000"
TOKEN_A = "0x1111111111111111111111111111111111111111"
TOKEN_B = "0x2222222222222222222222222222222222222222"
POOL = "0x3333333333333333333333333333333333333333"
HOOKS = "0x4444444444444444444444444444444444444444"
POOL_MANAGER = "0x000000000004444c5dc75cb358380d2e3de08a90"
POOL_ID = "0x" + ("ab" * 32)
TX_HASH = "0x" + ("cd" * 32)


def topic_address(address):
    return "0x" + ("0" * 24) + address[2:]


def uint_word(value):
    return f"{value:064x}"


def int_word(value):
    if value < 0:
        value += 1 << 256
    return uint_word(value)


def make_v4_log(currency0, currency1):
    return {
        "topics": [
            pool_scanner.INITIALIZE_TOPIC,
            POOL_ID,
            topic_address(currency0),
            topic_address(currency1),
        ],
        "data": "0x" + "".join(
            (
                uint_word(500),
                int_word(-10),
                uint_word(int(HOOKS, 16)),
                uint_word(2**96),
                int_word(-123),
            )
        ),
        "blockNumber": "0x123",
        "transactionHash": TX_HASH,
    }


def make_v4_source():
    return {
        "name": "uniswap_v4",
        "type": "uniswap_v4_pool_manager",
        "factory_address": None,
        "pool_manager_address": POOL_MANAGER,
        "subscription_address": POOL_MANAGER,
    }


class PoolScannerSimulatedTests(unittest.TestCase):
    def test_v4_config_uses_pool_manager_as_subscription_address(self):
        previous_rpc_url = os.environ.get("TEST_ETH_WSS_URL")
        os.environ["TEST_ETH_WSS_URL"] = "wss://example.invalid"
        try:
            chains = pool_scanner.build_enabled_chains(
                {
                    "chains": {
                        "ethereum": {
                            "enabled": True,
                            "rpc_env": "TEST_ETH_WSS_URL",
                            "quote_tokens": {"WETH": WETH},
                            "sources": [
                                {
                                    "name": "uniswap_v4",
                                    "enabled": True,
                                    "type": "uniswap_v4_pool_manager",
                                    "pool_manager_address": POOL_MANAGER,
                                    "event": "Initialize",
                                }
                            ],
                        }
                    }
                }
            )
        finally:
            if previous_rpc_url is None:
                os.environ.pop("TEST_ETH_WSS_URL", None)
            else:
                os.environ["TEST_ETH_WSS_URL"] = previous_rpc_url

        source = chains[0]["sources"][0]
        self.assertEqual(source["subscription_address"], POOL_MANAGER)
        self.assertEqual(source["pool_manager_address"], POOL_MANAGER)
        self.assertIsNone(source["factory_address"])

    def test_v4_weth_currency0_identifies_currency1(self):
        decoded = pool_scanner.decode_uniswap_v4_initialize(make_v4_log(WETH, TOKEN_A))
        candidate, ignored_reason = pool_scanner.identify_new_token(
            decoded,
            {WETH: "WETH"},
            no_quote_reason="ignored_no_known_quote_token",
        )

        self.assertIsNone(ignored_reason)
        self.assertEqual(candidate["token_address"], TOKEN_A)
        self.assertEqual(decoded["pool_id"], POOL_ID)
        self.assertIsNone(decoded["pool_address"])
        self.assertEqual(decoded["fee"], 500)
        self.assertEqual(decoded["tick_spacing"], -10)
        self.assertEqual(decoded["hooks"], HOOKS)
        self.assertEqual(decoded["sqrt_price_x96"], 2**96)
        self.assertEqual(decoded["tick"], -123)

    def test_v4_weth_currency1_identifies_currency0(self):
        decoded = pool_scanner.decode_uniswap_v4_initialize(make_v4_log(TOKEN_A, WETH))
        candidate, ignored_reason = pool_scanner.identify_new_token(
            decoded,
            {WETH: "WETH"},
            no_quote_reason="ignored_no_known_quote_token",
        )

        self.assertIsNone(ignored_reason)
        self.assertEqual(candidate["token_address"], TOKEN_A)

    def test_v4_native_eth_currency0_identifies_currency1(self):
        decoded = pool_scanner.decode_uniswap_v4_initialize(make_v4_log(ETH, TOKEN_A))
        candidate, ignored_reason = pool_scanner.identify_new_token(
            decoded,
            {WETH: "WETH", ETH: "ETH"},
            no_quote_reason="ignored_no_known_quote_token",
            allow_native_eth_quote=True,
        )

        self.assertIsNone(ignored_reason)
        self.assertEqual(candidate["token_address"], TOKEN_A)
        self.assertEqual(candidate["quote_token"], "ETH")
        self.assertEqual(candidate["quote_token_address"], ETH)

    def test_v4_native_eth_currency1_identifies_currency0(self):
        decoded = pool_scanner.decode_uniswap_v4_initialize(make_v4_log(TOKEN_A, ETH))
        candidate, ignored_reason = pool_scanner.identify_new_token(
            decoded,
            {WETH: "WETH", ETH: "ETH"},
            no_quote_reason="ignored_no_known_quote_token",
            allow_native_eth_quote=True,
        )

        self.assertIsNone(ignored_reason)
        self.assertEqual(candidate["token_address"], TOKEN_A)
        self.assertEqual(candidate["quote_token"], "ETH")
        self.assertEqual(candidate["quote_token_address"], ETH)

    def test_v2_v3_native_eth_quote_stays_ignored(self):
        decoded = {"token0": ETH, "token1": TOKEN_A, "pool_address": POOL, "fee": None}
        candidate, ignored_reason = pool_scanner.identify_new_token(
            decoded,
            {WETH: "WETH", ETH: "ETH"},
            no_quote_reason="pool_without_known_quote_token",
            allow_native_eth_quote=False,
        )

        self.assertIsNone(candidate)
        self.assertEqual(ignored_reason, "pool_without_known_quote_token")

    def test_v4_without_known_quote_is_ignored(self):
        decoded = pool_scanner.decode_uniswap_v4_initialize(make_v4_log(TOKEN_A, TOKEN_B))
        candidate, ignored_reason = pool_scanner.identify_new_token(
            decoded,
            {WETH: "WETH"},
            no_quote_reason="ignored_no_known_quote_token",
        )

        self.assertIsNone(candidate)
        self.assertEqual(ignored_reason, "ignored_no_known_quote_token")

    def test_v4_normalized_event_and_watchlist_entry_keep_pool_id(self):
        raw_log = make_v4_log(WETH, TOKEN_A)
        source = make_v4_source()
        decoded = pool_scanner.decode_uniswap_v4_initialize(raw_log)
        candidate, _ = pool_scanner.identify_new_token(decoded, {WETH: "WETH"})
        record = pool_scanner.build_raw_event_record(
            "ethereum",
            source,
            raw_log,
            decoded,
            candidate,
            None,
            "2026-06-01T12:00:00Z",
        )
        entry = pool_scanner.build_watchlist_entry(
            "ethereum",
            source,
            decoded,
            candidate,
            "2026-06-01T12:00:00Z",
            raw_log,
        )

        self.assertEqual(record["normalized_event"]["source_type"], "pool_initialized")
        self.assertEqual(record["normalized_event"]["pool_id"], POOL_ID)
        self.assertIsNone(record["normalized_event"]["pool_address"])
        self.assertEqual(entry["pool_id"], POOL_ID)
        self.assertEqual(entry["pool_manager_address"], POOL_MANAGER)
        self.assertIsNone(entry["pool_address"])

    def test_v2_and_v3_decoders_remain_compatible(self):
        v2 = pool_scanner.decode_uniswap_v2_pair_created(
            {
                "topics": [pool_scanner.PAIR_CREATED_TOPIC, topic_address(TOKEN_A), topic_address(WETH)],
                "data": "0x" + uint_word(int(POOL, 16)) + uint_word(1),
            }
        )
        v3 = pool_scanner.decode_uniswap_v3_pool_created(
            {
                "topics": [
                    pool_scanner.POOL_CREATED_TOPIC,
                    topic_address(TOKEN_A),
                    topic_address(WETH),
                    "0x" + uint_word(3000),
                ],
                "data": "0x" + int_word(60) + uint_word(int(POOL, 16)),
            }
        )

        self.assertEqual(v2, {"token0": TOKEN_A, "token1": WETH, "pool_address": POOL, "fee": None})
        self.assertEqual(v3, {"token0": TOKEN_A, "token1": WETH, "pool_address": POOL, "fee": 3000})


if __name__ == "__main__":
    unittest.main()
