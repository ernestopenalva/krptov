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

    def test_trading_channel_uses_trading_destination(self):
        session = FakeTelegramSession(FakeTelegramResponse({"ok": True, "result": {"message_id": 456}}))

        result = telegram_notifier.send_message(
            "trading",
            "teste",
            config=telegram_config(),
            env={
                "bot_token": "token",
                "trading_chat_id": "trading-chat",
                "trading_thread_id": "10",
                "system_chat_id": "system-chat",
                "system_thread_id": "20",
            },
            session=session,
        )

        self.assertTrue(result["success"])
        self.assertEqual(session.calls[0]["json"]["chat_id"], "trading-chat")
        self.assertEqual(session.calls[0]["json"]["message_thread_id"], "10")

    def test_system_channel_uses_system_destination(self):
        session = FakeTelegramSession(FakeTelegramResponse({"ok": True, "result": {"message_id": 456}}))

        result = telegram_notifier.send_message(
            "system",
            "teste",
            config=telegram_config(),
            env={
                "bot_token": "token",
                "trading_chat_id": "trading-chat",
                "trading_thread_id": "10",
                "system_chat_id": "system-chat",
                "system_thread_id": "20",
            },
            session=session,
        )

        self.assertTrue(result["success"])
        self.assertEqual(session.calls[0]["json"]["chat_id"], "system-chat")
        self.assertEqual(session.calls[0]["json"]["message_thread_id"], "20")

    def test_legacy_chat_id_still_works(self):
        session = FakeTelegramSession(FakeTelegramResponse({"ok": True, "result": {"message_id": 456}}))

        result = telegram_notifier.send_message(
            "system",
            "teste",
            config=telegram_config(),
            env={"bot_token": "token", "chat_id": "legacy-chat", "thread_id": "30"},
            session=session,
        )

        self.assertTrue(result["success"])
        self.assertEqual(session.calls[0]["json"]["chat_id"], "legacy-chat")
        self.assertEqual(session.calls[0]["json"]["message_thread_id"], "30")

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

        self.assertIn("A&lt;B/ETH", message)
        self.assertIn("alice&lt;admin&gt;", message)
        self.assertIn("name &lt;bad&gt;&amp;reason", message)
        self.assertNotIn("A&B <Coin>", message)

    def test_alert_message_uses_human_reasons_and_gmgn_link(self):
        token_address = "0xe24659c4567af33a332fe4dfea2b38f40e9487c5"
        alert = {
            "alert_rank": 20,
            "chain_id": "ethereum",
            "token_address": token_address,
            "alert_reasons": ["author_followers_medium>=7528"],
            "author_username": "BlackhatEmpire",
            "author_followers": 7528,
            "trigger_posts": [{"url": "https://x.com/BlackhatEmpire/status/2062941605099667783"}],
        }

        message = telegram_notifier.build_alert_message(alert, {})

        self.assertNotIn("<b>Endereco:</b>", message)
        self.assertIn(
            'autor com boa audiência <a href="https://x.com/BlackhatEmpire">@BlackhatEmpire</a> '
            "(7.528 seguidores)",
            message,
        )
        self.assertIn(
            '<b>Autor:</b> <a href="https://x.com/BlackhatEmpire">@BlackhatEmpire</a>',
            message,
        )
        self.assertNotIn("<b>Seguidores:</b>", message)
        self.assertNotIn("Maior audiência no resultado", message)
        self.assertIn(
            f"<b>GMGN:</b> https://gmgn.ai/eth/token/{token_address}",
            message,
        )
        self.assertIn(f"<b>Endereco completo:</b> <code>{token_address}</code>", message)

    def test_alert_message_uses_readable_affiliation_from_best_author(self):
        alert = {
            "alert_rank": 100,
            "chain_id": "base",
            "token_address": "0xb8c02cc0682832c86a21e26f1d1de80b51255ba3",
            "alert_reasons": ["author_affiliation_found", "author_followers_high>=25887"],
            "author_username": "meligamble",
            "author_followers": 7161,
            "author_affiliation_found": True,
            "author_affiliation_id": ["1329466321919217665"],
            "best_followers_author_summary": {
                "username": "NeoCallss",
                "followers": 25887,
            },
            "best_affiliation_author_summary": {
                "username": "NeoCallss",
                "followers": 25887,
                "affiliation_found": True,
                "affiliation_name": "Sigma",
                "affiliation_username": "SigmaTrading",
            },
            "trigger_posts": [{"url": "https://x.com/meligamble/status/1", "author_username": "meligamble"}],
        }

        message = telegram_notifier.build_alert_message(alert, {"token_symbol": "Bertie", "quote_token": "WETH"})

        self.assertIn(
            'autor com grande audiência <a href="https://x.com/NeoCallss">@NeoCallss</a> '
            "(25.887 seguidores)",
            message,
        )
        self.assertIn(
            '<b>Afiliação:</b> Sigma (<a href="https://x.com/SigmaTrading">@SigmaTrading</a>)',
            message,
        )
        self.assertNotIn("id=", message)
        self.assertNotIn("<b>Seguidores:</b>", message)
        self.assertNotIn("Maior audiência no resultado", message)


    def test_alert_message_extracts_affiliation_username_from_raw_url(self):
        alert = {
            "alert_rank": 100,
            "chain_id": "base",
            "token_address": "0xc3ebe0574abf86adb818deec5b3bb7435d490ba3",
            "alert_reasons": ["author_affiliation_found"],
            "author_username": "panzonhl",
            "author_followers": 638,
            "author_affiliation_found": True,
            "author_affiliation_raw": {
                "description": "Based",
                "url": "https://twitter.com/BasedBot",
                "user_id": ["1800483217327157248"],
                "badge_url": "https://pbs.twimg.com/profile_images/based.jpg",
            },
            "trigger_posts": [{"url": "https://x.com/panzonhl/status/1", "author_username": "panzonhl"}],
        }

        message = telegram_notifier.build_alert_message(alert, {"token_symbol": "FABLEBOY", "quote_token": "USDC"})

        self.assertIn(
            'autor afiliado a Based (<a href="https://x.com/BasedBot">@BasedBot</a>)',
            message,
        )
        self.assertIn(
            '<b>Afiliação:</b> Based (<a href="https://x.com/BasedBot">@BasedBot</a>)',
            message,
        )
        self.assertNotIn("<b>Afiliação:</b> presente", message)


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
                            "social_eligibility": "eligible",
                            "market_score": 80,
                            "minimum_token_age_inferred_minutes": 30,
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

    def test_rank_upgrade_is_not_sent_after_social_completion(self):
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

            self.assertEqual(success_mock.call_count, 1)
            self.assertEqual(snapshot["alerts_generated"], 0)
            self.assertEqual(entry["best_alert_rank"], 80)
            self.assertEqual(entry["telegram_alert_signature"], "rank_80")
            self.assertEqual(entry["social_status"], "concluido")

    def test_automated_author_does_not_rank_as_affiliated(self):
        payload = {
            "data": [
                {
                    "id": "1",
                    "author_id": "u1",
                    "text": "CA 0xabc",
                    "created_at": "2026-06-10T12:00:00Z",
                    "public_metrics": {},
                }
            ],
            "includes": {
                "users": [
                    {
                        "id": "u1",
                        "username": "BaseAlphaOnly",
                        "name": "Base Alpha Only",
                        "automated_by": {"username": "Pixel_eth"},
                        "affiliation": {"type": "automation"},
                        "public_metrics": {"followers_count": 30000},
                    }
                ]
            },
        }

        analysis = social_inference.build_social_analysis(payload, social_inference.DEFAULT_CONFIG)

        self.assertEqual(analysis["alert_rank"], 0)
        self.assertFalse(analysis["affiliation_found"])
        self.assertEqual(analysis["alert_reasons"], [])

    def test_qualified_affiliation_still_ranks(self):
        payload = {
            "data": [
                {
                    "id": "1",
                    "author_id": "u1",
                    "text": "CA 0xabc",
                    "created_at": "2026-06-10T12:00:00Z",
                    "public_metrics": {},
                }
            ],
            "includes": {
                "users": [
                    {
                        "id": "u1",
                        "username": "gabbens",
                        "name": "Gabbens",
                        "affiliation": {"name": "Sigma", "username": "SigmaTrading"},
                        "public_metrics": {"followers_count": 319},
                    }
                ]
            },
        }

        analysis = social_inference.build_social_analysis(payload, social_inference.DEFAULT_CONFIG)

        self.assertEqual(analysis["alert_rank"], 100)
        self.assertTrue(analysis["affiliation_found"])
        self.assertIn("author_affiliation_found", analysis["alert_reasons"])
        self.assertEqual(analysis["origin_summary"]["author_affiliation_name"], "Sigma")
        self.assertEqual(analysis["origin_summary"]["author_affiliation_username"], "SigmaTrading")


if __name__ == "__main__":
    unittest.main()
