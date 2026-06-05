import json
import sys
import tempfile
import types
import unittest
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

import requests

from src.modules import social_inference, telegram_notifier


class FakeTelegramResponse:
    def __init__(self, payload=None, status_code=200, http_error=False):
        self._payload = payload if payload is not None else {"ok": True, "result": {"message_id": 123}}
        self.status_code = status_code
        self.http_error = http_error

    def raise_for_status(self):
        if self.http_error:
            error = requests.HTTPError("http error")
            error.response = self
            raise error

    def json(self):
        return self._payload


class FakeTelegramSession:
    def __init__(self, response=None, error=None):
        self.response = response or FakeTelegramResponse()
        self.error = error
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.error:
            raise self.error
        return self.response


def telegram_config(**updates):
    config = {
        "enabled": True,
        "dry_run": False,
        "parse_mode": "HTML",
        "timeout_seconds": 20,
    }
    config.update(updates)
    return config


def fake_analysis(rank=80, signature="author_followers_high"):
    origin = social_inference.empty_origin_summary()
    origin.update(
        {
            "origin_type": "author",
            "author_username": "alice",
            "author_followers": 25000,
            "author_verified": True,
            "author_verified_type": "business",
        }
    )
    return {
        "tweets": [{"id": "1"}],
        "posts_found": 1,
        "users_found": 1,
        "best_post_score": 0,
        "best_author_followers": 25000,
        "author_badge_found": True,
        "affiliation_found": False,
        "bio_patterns_found": [],
        "origin_summary": origin,
        "selected_origin_summary": origin,
        "best_followers_author_summary": None,
        "best_affiliation_author_summary": None,
        "trigger_posts": [
            {
                "tweet_id": "1",
                "author_username": "alice",
                "url": "https://x.com/alice/status/1",
                "text": "hello",
                "public_metrics": {},
            }
        ],
        "alert_rank": rank,
        "alert_reasons": [signature],
        "alert_signature": signature,
    }


class TelegramNotifierTests(unittest.TestCase):
    def test_dry_run_does_not_call_telegram(self):
        session = FakeTelegramSession()

        result = telegram_notifier.send_message(
            "teste",
            config=telegram_config(dry_run=True),
            env={"bot_token": "token", "chat_id": "chat"},
            session=session,
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(session.calls, [])

    def test_success_returns_message_id(self):
        session = FakeTelegramSession(FakeTelegramResponse({"ok": True, "result": {"message_id": 456}}))

        result = telegram_notifier.send_message(
            "teste",
            config=telegram_config(),
            env={"bot_token": "token", "chat_id": "chat"},
            session=session,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["message_id"], 456)
        self.assertEqual(session.calls[0]["json"]["parse_mode"], "HTML")

    def test_api_failure_returns_error(self):
        session = FakeTelegramSession(FakeTelegramResponse({"ok": False, "description": "chat not found"}))

        result = telegram_notifier.send_message(
            "teste",
            config=telegram_config(),
            env={"bot_token": "token", "chat_id": "chat"},
            session=session,
        )

        self.assertFalse(result["success"])
        self.assertIn("chat not found", result["error"])

    def test_timeout_returns_error(self):
        session = FakeTelegramSession(error=requests.Timeout("slow"))

        result = telegram_notifier.send_message(
            "teste",
            config=telegram_config(),
            env={"bot_token": "token", "chat_id": "chat"},
            session=session,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "timeout")

    def test_dynamic_fields_are_html_escaped(self):
        alert = {
            "alert_rank": 80,
            "chain_id": "ethereum",
            "token_address": "0x1111111111111111111111111111111111111111",
            "alert_reasons": ["name_<bad>&reason"],
            "author_username": "alice<admin>",
            "author_followers": 1000,
            "trigger_posts": [{"url": "https://x.com/a?x=1&y=2"}],
        }
        entry = {"token_name": "A&B <Coin>", "token_symbol": "A<B"}

        message = telegram_notifier.build_alert_message(alert, entry)

        self.assertIn("A&amp;B &lt;Coin&gt;", message)
        self.assertIn("A&lt;B", message)
        self.assertIn("alice&lt;admin&gt;", message)
        self.assertIn("name_&lt;bad&gt;&amp;reason", message)
        self.assertNotIn("A&B <Coin>", message)


class SocialInferenceTelegramTests(unittest.TestCase):
    def run_social_cycle(self, root, send_alert_mock, analysis=None):
        watchlist_file = root / "watchlist.json"
        lock_file = root / "watchlist.lock"
        latest_file = root / "social_inference_latest.json"
        alerts_file = root / "social_alerts.json"
        posts_dir = root / "social_posts"
        alert_posts_dir = root / "social_alert_posts"
        logs_dir = root / "logs"
        token_address = "0x1111111111111111111111111111111111111111"
        watchlist_key = f"ethereum:{token_address}"

        if not watchlist_file.exists():
            watchlist_file.write_text(
                json.dumps(
                    {
                        watchlist_key: {
                            "watchlist_key": watchlist_key,
                            "chain": "ethereum",
                            "chain_id": "ethereum",
                            "token_address": token_address,
                            "token_name": "Test Token",
                            "token_symbol": "TEST",
                            "status": "novo",
                            "social_status": "pendente",
                            "monitor_status": "pendente",
                            "telegram_alert_sent": False,
                        }
                    }
                ),
                encoding="utf-8",
            )

        config = social_inference.merge_dict(
            social_inference.DEFAULT_CONFIG,
            {
                "max_posts_per_token": 8,
                "telegram_alerts": telegram_config(),
            },
        )
        analysis = analysis or fake_analysis()
        response_payload = {
            "data": [
                {
                    "id": "1",
                    "author_id": "u1",
                    "text": "hello",
                    "created_at": "2026-06-05T12:00:00Z",
                    "public_metrics": {},
                }
            ],
            "includes": {"users": []},
        }

        with patch.object(social_inference, "DATA_DIR", root), patch.object(
            social_inference, "LOGS_DIR", logs_dir
        ), patch.object(social_inference, "WATCHLIST_FILE", watchlist_file), patch.object(
            social_inference, "WATCHLIST_LOCK_FILE", lock_file
        ), patch.object(social_inference, "LATEST_SNAPSHOT_FILE", latest_file), patch.object(
            social_inference, "ALERTS_FILE", alerts_file
        ), patch.object(social_inference, "POSTS_DIR", posts_dir), patch.object(
            social_inference, "ALERT_POSTS_DIR", alert_posts_dir
        ), patch.object(social_inference, "load_config", return_value=config), patch.object(
            social_inference, "load_bearer_token", return_value="fake-token"
        ), patch.object(
            social_inference.telegram_notifier,
            "load_telegram_env",
            return_value={"bot_token": "token", "chat_id": "chat"},
        ), patch.object(
            social_inference,
            "search_token_mentions",
            return_value=response_payload,
        ), patch.object(
            social_inference,
            "build_social_analysis",
            return_value=analysis,
        ), patch.object(
            social_inference.telegram_notifier,
            "send_alert",
            send_alert_mock,
        ):
            snapshot = social_inference.run_cycle()

        return snapshot, json.loads(watchlist_file.read_text(encoding="utf-8"))[watchlist_key]

    def test_social_marks_telegram_sent_only_after_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            success_mock = Mock(
                return_value={"success": True, "message_id": 999, "error": None}
            )
            snapshot, entry = self.run_social_cycle(root, success_mock)

            self.assertEqual(snapshot["alerts_generated"], 1)
            self.assertTrue(entry["telegram_alert_sent"])
            self.assertEqual(entry["telegram_message_id"], 999)
            self.assertEqual(entry["telegram_alert_signature"], "author_followers_high")

    def test_social_keeps_telegram_unsent_after_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            failure_mock = Mock(
                return_value={"success": False, "message_id": None, "error": "HTTP 400"}
            )
            snapshot, entry = self.run_social_cycle(root, failure_mock)

            self.assertEqual(snapshot["alerts_generated"], 1)
            self.assertFalse(entry["telegram_alert_sent"])
            self.assertEqual(entry["telegram_alert_error"], "HTTP 400")
            self.assertIn("telegram_alert_attempted_at", entry)

    def test_duplicate_alert_is_not_sent_again_after_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            success_mock = Mock(
                return_value={"success": True, "message_id": 999, "error": None}
            )
            self.run_social_cycle(root, success_mock)
            snapshot, entry = self.run_social_cycle(root, success_mock)

            self.assertEqual(success_mock.call_count, 1)
            self.assertEqual(snapshot["alerts_generated"], 0)
            self.assertTrue(entry["telegram_alert_sent"])

    def test_rank_upgrade_sends_new_alert(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            success_mock = Mock(
                return_value={"success": True, "message_id": 999, "error": None}
            )
            self.run_social_cycle(root, success_mock, analysis=fake_analysis(rank=80, signature="rank_80"))
            snapshot, entry = self.run_social_cycle(
                root,
                success_mock,
                analysis=fake_analysis(rank=90, signature="rank_90"),
            )

            self.assertEqual(success_mock.call_count, 2)
            self.assertEqual(snapshot["alerts_generated"], 1)
            self.assertEqual(entry["best_alert_rank"], 90)
            self.assertEqual(entry["telegram_alert_signature"], "rank_90")


if __name__ == "__main__":
    unittest.main()
