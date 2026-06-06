import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch


requests = sys.modules.get("requests")
if requests is None:
    requests = types.ModuleType("requests")
    sys.modules["requests"] = requests

if not hasattr(requests, "RequestException"):
    class RequestException(Exception):
        pass

    requests.RequestException = RequestException

if not hasattr(requests, "Timeout"):
    class Timeout(requests.RequestException):
        pass

    requests.Timeout = Timeout

if not hasattr(requests, "HTTPError"):
    class HTTPError(requests.RequestException):
        pass

    requests.HTTPError = HTTPError

if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv

from src.modules import market_ranker


class MarketRankerOpsAlertTests(unittest.TestCase):
    def test_classifies_dexscreener_rate_limit(self):
        error = (
            "429 Client Error: Too Many Requests for url: "
            "https://api.dexscreener.com/token-pairs/v1/ethereum/0x1"
        )

        self.assertEqual(market_ranker.classify_error(error), "dexscreener_rate_limit")

    def test_ops_alert_uses_cooldown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "ops_alert_state.json"
            current_time = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)
            summary = {
                "timestamp": "2026-06-06T12:00:00Z",
                "tokens_checked": 10,
                "errors": 1,
            }
            results = [
                {
                    "watchlist_key": "ethereum:0x1111111111111111111111111111111111111111",
                    "error": "429 Client Error: Too Many Requests for url: https://api.dexscreener.com/",
                }
            ]
            send_message = Mock(return_value={"success": True, "message_id": 123, "error": None})

            with patch.object(market_ranker, "OPS_ALERT_STATE_FILE", state_file), patch.object(
                market_ranker.telegram_notifier,
                "load_telegram_env",
                return_value={"bot_token": "token", "chat_id": "chat"},
            ), patch.object(
                market_ranker.telegram_notifier,
                "send_message",
                send_message,
            ):
                first = market_ranker.maybe_send_ops_alert(summary, results, current_time)
                second = market_ranker.maybe_send_ops_alert(summary, results, current_time)

            self.assertTrue(first["success"])
            self.assertEqual(first["message_id"], 123)
            self.assertEqual(second["error"], "cooldown")
            self.assertEqual(send_message.call_count, 1)

    def test_ops_alert_can_be_disabled_by_config(self):
        current_time = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)
        summary = {
            "timestamp": "2026-06-06T12:00:00Z",
            "tokens_checked": 10,
            "errors": 1,
        }
        results = [
            {
                "watchlist_key": "ethereum:0x1111111111111111111111111111111111111111",
                "error": "429 Client Error: Too Many Requests for url: https://api.dexscreener.com/",
            }
        ]
        send_message = Mock(return_value={"success": True, "message_id": 123, "error": None})

        with patch.object(market_ranker.telegram_notifier, "send_message", send_message):
            result = market_ranker.maybe_send_ops_alert(
                summary,
                results,
                current_time,
                config={"ops_alerts": {"enabled": False, "cooldown_seconds": 1800}},
            )

        self.assertIsNone(result)
        self.assertEqual(send_message.call_count, 0)


if __name__ == "__main__":
    unittest.main()
