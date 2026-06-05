import html
import os
from datetime import datetime

import requests
from dotenv import load_dotenv


TELEGRAM_SEND_MESSAGE_URL = "https://api.telegram.org/bot{bot_token}/sendMessage"


def escape_html(value):
    if value is None:
        return ""

    return html.escape(str(value), quote=True)


def load_telegram_env(env_file=None):
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()

    return {
        "bot_token": os.getenv("TELEGRAM_BOT_TOKEN"),
        "chat_id": os.getenv("TELEGRAM_CHAT_ID"),
        "thread_id": os.getenv("TELEGRAM_THREAD_ID"),
    }


def short_address(value):
    if not value:
        return "indisponivel"

    value = str(value)
    if len(value) <= 14:
        return value

    return f"{value[:6]}...{value[-4:]}"


def format_number(value):
    if value is None:
        return "indisponivel"

    try:
        return f"{int(round(float(value))):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(value)


def first_trigger_post(alert):
    posts = alert.get("trigger_posts") or []
    if not posts:
        return {}

    first = posts[0]
    return first if isinstance(first, dict) else {}


def build_alert_message(alert, entry=None):
    entry = entry or {}
    trigger_post = first_trigger_post(alert)
    token_symbol = (
        entry.get("token_symbol")
        or (entry.get("selected_pair") or {}).get("baseToken", {}).get("symbol")
        or "indisponivel"
    )
    token_name = (
        entry.get("token_name")
        or (entry.get("selected_pair") or {}).get("baseToken", {}).get("name")
        or "indisponivel"
    )
    chain_id = alert.get("chain_id") or entry.get("chain_id") or entry.get("chain") or "unknown"
    token_address = alert.get("token_address") or entry.get("token_address")
    reasons = alert.get("alert_reasons") or []
    reason_text = ", ".join(str(reason) for reason in reasons) if reasons else "nenhum"
    author = alert.get("author_username") or trigger_post.get("author_username") or "indisponivel"
    followers = alert.get("author_followers") or alert.get("best_author_followers")
    dex_url = (entry.get("selected_pair") or {}).get("url") or entry.get("dexscreener_url")
    post_url = trigger_post.get("url")

    lines = [
        "<b>KRPTO-V | Alerta social</b>",
        f"<b>Rank:</b> {escape_html(alert.get('alert_rank', 'indisponivel'))}",
        f"<b>Token:</b> {escape_html(token_name)} / {escape_html(token_symbol)}",
        f"<b>Chain:</b> {escape_html(chain_id)}",
        f"<b>Endereco:</b> <code>{escape_html(short_address(token_address))}</code>",
        f"<b>Motivos:</b> {escape_html(reason_text)}",
        f"<b>Autor:</b> @{escape_html(author)}",
        f"<b>Seguidores:</b> {escape_html(format_number(followers))}",
    ]

    if alert.get("author_verified_type"):
        lines.append(f"<b>Verified type:</b> {escape_html(alert.get('author_verified_type'))}")
    if alert.get("automated_operator_username"):
        lines.append(f"<b>Operador:</b> @{escape_html(alert.get('automated_operator_username'))}")
    if post_url:
        lines.append(f"<b>Post:</b> {escape_html(post_url)}")
    if dex_url:
        lines.append(f"<b>Dexscreener:</b> {escape_html(dex_url)}")
    if token_address:
        lines.append(f"<b>Endereco completo:</b> <code>{escape_html(token_address)}</code>")

    return "\n".join(lines)


def send_message(
    text,
    config=None,
    env=None,
    session=requests,
):
    config = config or {}
    env = env or load_telegram_env()
    dry_run = bool(config.get("dry_run", False))
    parse_mode = config.get("parse_mode") or "HTML"
    timeout_seconds = int(config.get("timeout_seconds") or 20)
    bot_token = env.get("bot_token")
    chat_id = env.get("chat_id")
    thread_id = env.get("thread_id")

    if dry_run:
        return {
            "success": True,
            "message_id": None,
            "error": None,
            "dry_run": True,
            "sent_at": datetime.now().replace(microsecond=0).isoformat(),
        }

    if not bot_token:
        return {"success": False, "message_id": None, "error": "TELEGRAM_BOT_TOKEN ausente"}
    if not chat_id:
        return {"success": False, "message_id": None, "error": "TELEGRAM_CHAT_ID ausente"}

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if thread_id:
        payload["message_thread_id"] = thread_id

    try:
        response = session.post(
            TELEGRAM_SEND_MESSAGE_URL.format(bot_token=bot_token),
            json=payload,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout:
        return {"success": False, "message_id": None, "error": "timeout"}
    except requests.HTTPError as exc:
        response = exc.response
        status = getattr(response, "status_code", "unknown")
        return {"success": False, "message_id": None, "error": f"HTTP {status}"}
    except requests.RequestException as exc:
        return {"success": False, "message_id": None, "error": str(exc)}
    except ValueError as exc:
        return {"success": False, "message_id": None, "error": f"JSON invalido: {exc}"}

    if not isinstance(payload, dict) or not payload.get("ok"):
        description = payload.get("description") if isinstance(payload, dict) else payload
        return {
            "success": False,
            "message_id": None,
            "error": f"Telegram API retornou falha: {description}",
        }

    result = payload.get("result") or {}
    return {
        "success": True,
        "message_id": result.get("message_id"),
        "error": None,
        "dry_run": False,
    }


def send_alert(alert, entry=None, config=None, env=None, session=requests):
    message = build_alert_message(alert, entry=entry)
    return send_message(message, config=config, env=env, session=session)
