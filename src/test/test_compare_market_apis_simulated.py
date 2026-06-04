import importlib.util
import sys
import types
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

if "requests" not in sys.modules:
    requests = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    requests.RequestException = RequestException
    sys.modules["requests"] = requests

spec = importlib.util.spec_from_file_location(
    "compare_market_apis_simulated",
    PROJECT_ROOT / "src" / "tools" / "compare_market_apis.py",
)
compare_market_apis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare_market_apis)


TOKEN = "0x1111111111111111111111111111111111111111"
QUOTE = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
POOL = "0x2222222222222222222222222222222222222222"
POOL_ID = "0x" + ("ab" * 32)


def make_case(pool_address=POOL, pool_id=None, source="uniswap_v2"):
    return {
        "case_id": f"ethereum:{pool_address or pool_id}",
        "chain": "ethereum",
        "source": source,
        "source_type": "pool_created",
        "token_address": TOKEN,
        "quote_token": "WETH",
        "quote_token_address": QUOTE,
        "pool_address": pool_address,
        "pool_id": pool_id,
        "pool_created_at_utc": "2026-06-01T12:00:00Z",
        "scanner_received_at_utc": "2026-06-01T12:00:02Z",
    }


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        for marker, payload in self.responses.items():
            if marker in url:
                return FakeResponse(payload)
        return FakeResponse({})


def dexscreener_pair():
    return {
        "dexId": "uniswap",
        "pairAddress": POOL,
        "pairCreatedAt": 1748779200000,
        "liquidity": {"usd": 1234.5},
        "volume": {"h24": 4567.8},
        "txns": {"h24": {"buys": 3, "sells": 4}},
        "url": "https://dexscreener.com/ethereum/example",
        "baseToken": {"address": TOKEN},
        "quoteToken": {"address": QUOTE},
    }


def gecko_pair():
    return {
        "id": f"eth_{POOL}",
        "attributes": {
            "pool_created_at": "2026-06-01T12:00:01Z",
            "reserve_in_usd": "2222.2",
            "volume_usd": {"h24": "3333.3"},
            "transactions": {"h24": {"buys": 5, "sells": 6}},
        },
        "relationships": {
            "dex": {"data": {"id": "uniswap_v2"}},
            "base_token": {"data": {"id": f"eth_{TOKEN}"}},
            "quote_token": {"data": {"id": f"eth_{QUOTE}"}},
        },
    }


def paprika_pool():
    return {
        "id": POOL,
        "dex_id": "uniswap_v2",
        "dex_name": "Uniswap V2",
        "created_at": "2026-06-01T12:00:01Z",
        "liquidity_usd": 4444.4,
        "24h": {"volume_usd": 5555.5, "txns": 12},
        "tokens": [{"id": TOKEN}, {"id": QUOTE}],
    }


class CompareMarketApisSimulatedTests(unittest.TestCase):
    def test_queries_normalize_three_apis_for_exact_pool(self):
        session = FakeSession(
            {
                "dexscreener": {"pairs": [dexscreener_pair()]},
                "geckoterminal": {"data": gecko_pair()},
                "dexpaprika": paprika_pool(),
            }
        )
        case = make_case()

        dex = compare_market_apis.query_dexscreener(case, session=session)
        gecko = compare_market_apis.query_geckoterminal(case, session=session)
        paprika = compare_market_apis.query_dexpaprika(case, session=session)

        self.assertEqual(dex["liquidity_usd"], 1234.5)
        self.assertEqual(dex["txns_h24"], 7)
        self.assertEqual(gecko["liquidity_usd"], 2222.2)
        self.assertEqual(gecko["txns_h24"], 11)
        self.assertEqual(paprika["liquidity_usd"], 4444.4)
        self.assertEqual(paprika["txns_h24"], 12)

    def test_token_level_query_selects_matching_pair(self):
        case = make_case(pool_address=None, pool_id=POOL_ID, source="uniswap_v4")
        session = FakeSession(
            {
                "token-pairs": [dexscreener_pair()],
                "tokens": {"data": [gecko_pair()]},
                "dexpaprika": {"pools": [paprika_pool()]},
            }
        )

        dex = compare_market_apis.query_dexscreener(case, session=session)
        gecko = compare_market_apis.query_geckoterminal(case, session=session)
        paprika = compare_market_apis.query_dexpaprika(case, session=session)

        self.assertTrue(dex["found"])
        self.assertTrue(gecko["found"])
        self.assertTrue(paprika["found"])
        self.assertEqual(dex["query_mode"], "token_pools")
        self.assertEqual(gecko["query_mode"], "token_pools")
        self.assertEqual(paprika["query_mode"], "token_pools")

    def test_summary_reports_complementary_coverage(self):
        results = [
            {
                "case": make_case(),
                "apis": {
                    "dexscreener": {"found": False, "error": None},
                    "geckoterminal": {"found": True, "liquidity_usd": 1, "volume_h24_usd": 2, "txns_h24": 3, "error": None},
                },
            },
            {
                "case": make_case(pool_address="0x3333333333333333333333333333333333333333"),
                "apis": {
                    "dexscreener": {"found": True, "liquidity_usd": 4, "volume_h24_usd": 5, "txns_h24": 6, "error": None},
                    "geckoterminal": {"found": True, "liquidity_usd": 7, "volume_h24_usd": 8, "txns_h24": 9, "error": None},
                },
            },
        ]

        summary = compare_market_apis.summarize_results(results, ["dexscreener", "geckoterminal"])

        self.assertEqual(summary["by_api"]["dexscreener"]["found"], 1)
        self.assertEqual(summary["by_api"]["geckoterminal"]["found"], 2)
        self.assertEqual(summary["overlap"]["dexscreener_not_found__geckoterminal_found"], 1)


if __name__ == "__main__":
    unittest.main()
