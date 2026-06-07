import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


requests = sys.modules.get("requests")
if requests is None:
    requests = types.ModuleType("requests")
    sys.modules["requests"] = requests

if not hasattr(requests, "RequestException"):
    class RequestException(Exception):
        pass

    requests.RequestException = RequestException

if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv

from src.modules import token_age_resolver


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(self.payload)


class TokenAgeResolverTests(unittest.TestCase):
    def test_resolves_contract_creation_from_etherscan_v2(self):
        current_time = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)
        token_address = "0x1111111111111111111111111111111111111111"
        created_at = int(datetime(2026, 6, 7, 11, 30, 0, tzinfo=timezone.utc).timestamp())
        session = FakeSession(
            {
                "status": "1",
                "message": "OK",
                "result": [
                    {
                        "contractAddress": token_address,
                        "contractCreator": "0x2222222222222222222222222222222222222222",
                        "txHash": "0x" + ("ab" * 32),
                        "blockNumber": "123",
                        "timestamp": str(created_at),
                    }
                ],
            }
        )
        tokens = [
            {
                "watchlist_key": f"ethereum:{token_address}",
                "chain": "ethereum",
                "token_address": token_address,
                "entry": {"status": "novo"},
            }
        ]

        with TemporaryDirectory() as temp_dir, patch.object(
            token_age_resolver, "TOKEN_AGE_DATA_DIR", Path(temp_dir)
        ), patch.object(
            token_age_resolver, "load_api_key", return_value="key"
        ):
            updates, summary = token_age_resolver.resolve_token_ages(
                tokens,
                config={"enabled": True, "sleep_seconds": 0},
                current_time=current_time,
                session=session,
            )

        update = updates[f"ethereum:{token_address}"]
        self.assertEqual(summary["checked"], 1)
        self.assertEqual(summary["resolved"], 1)
        self.assertEqual(update["token_age_status"], "resolved")
        self.assertEqual(update["token_age_minutes"], 30)
        self.assertEqual(update["token_created_at_utc"], "2026-06-07T11:30:00Z")
        self.assertEqual(session.calls[0]["params"]["chainid"], "1")
        self.assertEqual(session.calls[0]["params"]["action"], "getcontractcreation")

    def test_missing_api_key_marks_error_without_request(self):
        current_time = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)
        token_address = "0x1111111111111111111111111111111111111111"
        session = FakeSession({})
        tokens = [
            {
                "watchlist_key": f"base:{token_address}",
                "chain": "base",
                "token_address": token_address,
                "entry": {"status": "novo"},
            }
        ]

        with TemporaryDirectory() as temp_dir, patch.object(
            token_age_resolver, "TOKEN_AGE_DATA_DIR", Path(temp_dir)
        ), patch.object(
            token_age_resolver, "load_api_key", return_value=None
        ):
            updates, summary = token_age_resolver.resolve_token_ages(
                tokens,
                config={"enabled": True},
                current_time=current_time,
                session=session,
            )

        update = updates[f"base:{token_address}"]
        self.assertEqual(summary["checked"], 1)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(update["token_age_status"], "error")
        self.assertEqual(update["token_age_source"], "etherscan_contract_creation")
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
