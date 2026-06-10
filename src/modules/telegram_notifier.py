import html
import os
from datetime import datetime

import requests
from dotenv import load_dotenv


TELEGRAM_SEND_MESSAGE_URL = "https://api.telegram.org/bot{bot_token}/sendMessage"
CHANNEL_TRADING = "trading"
CHANNEL_SYSTEM = "system"
SUPPORTED_CHANNELS = {CHANNEL_TRADING, CHANNEL_SYSTEM}


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
        "trading_chat_id": os.getenv("TELEGRAM_TRADING_CHAT_ID"),
        "trading_thread_id": os.getenv("TELEGRAM_TRADING_THREAD_ID"),
        "system_chat_id": os.getenv("TELEGRAM_SYSTEM_CHAT_ID"),
        "system_thread_id": os.getenv("TELEGRAM_SYSTEM_THREAD_ID"),
    }


def normalize_channel(channel):
    if channel in SUPPORTED_CHANNELS:
        return channel
    return CHANNEL_TRADING


def channel_destination(env, channel):
    channel = normalize_channel(channel)
    chat_id = env.get(f"{channel}_chat_id") or env.get("chat_id")
    thread_id = env.get(f"{channel}_thread_id") or env.get("thread_id")
    return chat_id, thread_id


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


def dexscreener_chain_slug(chain_id):
    if chain_id == "ethereum":
        return "ethereum"
    if chain_id == "base":
        return "base"
    return chain_id


def build_dexscreener_url(chain_id, token_address):
    if not chain_id or not token_address:
        return None

    return f"https://dexscreener.com/{dexscreener_chain_slug(chain_id)}/{token_address}"


def gmgn_chain_slug(chain_id):
    if chain_id == "ethereum":
        return "eth"
    if chain_id == "base":
        return "base"
    return chain_id


def build_gmgn_url(chain_id, token_address):
    if not chain_id or not token_address:
        return None

    return f"https://gmgn.ai/{gmgn_chain_slug(chain_id)}/token/{token_address}"


def default_quote_for_chain(chain_id):
    if chain_id == "base":
        return "BASE"
    if chain_id == "ethereum":
        return "ETH"
    return None


def token_pair_label(entry, chain_id):
    selected_pair = entry.get("selected_pair") or {}
    base_token = selected_pair.get("baseToken") or {}
    quote_token = selected_pair.get("quoteToken") or {}
    token_symbol = entry.get("token_symbol") or base_token.get("symbol") or "TOKEN"
    quote_symbol = (
        entry.get("quote_token")
        or quote_token.get("symbol")
        or default_quote_for_chain(chain_id)
        or "QUOTE"
    )
    return f"{token_symbol}/{quote_symbol}"


def affiliation_label(alert, prefix="author"):
    found = alert.get(f"{prefix}_affiliation_found")
    if not found:
        return None

    name = alert.get(f"{prefix}_affiliation_name")
    username = alert.get(f"{prefix}_affiliation_username")
    affiliation_id = alert.get(f"{prefix}_affiliation_id")
    raw = alert.get(f"{prefix}_affiliation_raw")

    if isinstance(raw, dict):
        name = name or raw.get("name") or raw.get("label")
        username = username or raw.get("username") or raw.get("screen_name") or raw.get("handle")
        affiliation_id = affiliation_id or raw.get("id") or raw.get("user_id")
    elif isinstance(raw, str):
        name = name or raw

    if username:
        username = str(username).lstrip("@")

    if name and username:
        return f"{name} (@{username})"
    if name:
        return str(name)
    if username:
        return f"@{username}"
    if affiliation_id:
        return f"id={affiliation_id}"
    return "presente"


def format_alert_reason(reason):
    reason = str(reason or "").strip()
    if not reason:
        return None

    if reason.startswith("author_followers_") and ">=" in reason:
        level, followers = reason.split(">=", 1)
        level = level.replace("author_followers_", "")
        level_text = {
            "medium": "boa audiência",
            "high": "grande audiência",
            "critical": "audiência muito alta",
        }.get(level, "audiência relevante")
        return f"autor com {level_text} ({format_number(followers)} seguidores)"

    reason_labels = {
        "author_verified_business": "autor com verificação empresarial",
        "author_verified_government": "autor com verificação governamental",
        "author_affiliation": "autor afiliado a uma organização",
        "author_affiliation_found": "autor afiliado a uma organização",
        "automated_operator_detected": "post indica operador ou automação por trás do token",
    }
    if reason in reason_labels:
        return reason_labels[reason]

    if reason.startswith("author_followers_") and ">=" in reason:
        level, followers = reason.split(">=", 1)
        level = level.replace("author_followers_", "")
        level_text = {
            "medium": "boa audiência",
            "high": "grande audiência",
            "critical": "audiência muito alta",
        }.get(level, "audiência relevante")
        return f"Autor com {level_text} ({format_number(followers)} seguidores)"

    if reason == "author_verified_business":
        return "Autor com verificação empresarial"
    if reason == "author_verified_government":
        return "Autor com verificação governamental"
    if reason == "author_affiliation":
        return "Autor vinculado a uma organização"
    if reason == "automated_operator_detected":
        return "Post indica operador ou automação por trás do token"

    return reason.replace("_", " ")


def format_alert_reasons(reasons):
    formatted = [format_alert_reason(reason) for reason in (reasons or [])]
    formatted = [reason for reason in formatted if reason]
    return "; ".join(formatted) if formatted else "nenhum"


def first_trigger_post(alert):
    posts = alert.get("trigger_posts") or []
    if not posts:
        return {}

    first = posts[0]
    return first if isinstance(first, dict) else {}


def build_alert_message(alert, entry=None):
    entry = entry or {}
    trigger_post = first_trigger_post(alert)
    chain_id = alert.get("chain_id") or entry.get("chain_id") or entry.get("chain") or "unknown"
    token_address = alert.get("token_address") or entry.get("token_address")
    pair_label = token_pair_label(entry, chain_id)
    reasons = alert.get("alert_reasons") or []
    reason_text = format_alert_reasons(reasons)
    author = alert.get("author_username") or trigger_post.get("author_username") or "indisponivel"
    followers = alert.get("author_followers") or alert.get("best_author_followers")
    gmgn_url = build_gmgn_url(chain_id, token_address)
    post_url = trigger_post.get("url")
    affiliation = affiliation_label(alert, "author")
    best_followers = alert.get("best_followers_author_summary") or {}

    lines = [
        "<b>KRPTO-V | Alerta social</b>",
        f"<b>Rank:</b> {escape_html(alert.get('alert_rank', 'indisponivel'))}",
        f"<b>Token:</b> {escape_html(pair_label)}",
        f"<b>Chain:</b> {escape_html(chain_id)}",
        f"<b>Motivos:</b> {escape_html(reason_text)}",
        f"<b>Autor:</b> @{escape_html(author)}",
        f"<b>Seguidores:</b> {escape_html(format_number(followers))}",
    ]

    if affiliation:
        lines.append(f"<b>Afiliação:</b> {escape_html(affiliation)}")
    if (
        best_followers.get("username")
        and best_followers.get("username") != author
        and best_followers.get("followers") is not None
    ):
        lines.append(
            "<b>Maior audiência no resultado:</b> "
            f"@{escape_html(best_followers.get('username'))} "
            f"({escape_html(format_number(best_followers.get('followers')))} seguidores)"
        )
    if alert.get("automated_operator_username"):
        lines.append(f"<b>Operador:</b> @{escape_html(alert.get('automated_operator_username'))}")
    if post_url:
        lines.append(f"<b>Post:</b> {escape_html(post_url)}")
    if gmgn_url:
        lines.append(f"<b>GMGN:</b> {escape_html(gmgn_url)}")
    if token_address:
        lines.append(f"<b>Endereco completo:</b> <code>{escape_html(token_address)}</code>")

    return "\n".join(lines)


def send_message(
    channel_or_text,
    text=None,
    config=None,
    env=None,
    session=requests,
):
    if text is None:
        channel = CHANNEL_TRADING
        text = channel_or_text
    else:
        channel = normalize_channel(channel_or_text)

    config = config or {}
    env = env or load_telegram_env()
    dry_run = bool(config.get("dry_run", False))
    parse_mode = config.get("parse_mode") or "HTML"
    timeout_seconds = int(config.get("timeout_seconds") or 20)
    bot_token = env.get("bot_token")
    chat_id, thread_id = channel_destination(env, channel)

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
        return {
            "success": False,
            "message_id": None,
            "error": f"TELEGRAM_{channel.upper()}_CHAT_ID ausente",
        }

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


def send_alert(alert, entry=None, config=None, env=None, session=requests, channel=CHANNEL_TRADING):
    message = build_alert_message(alert, entry=entry)
    return send_message(channel, message, config=config, env=env, session=session)
