import html
import os
from datetime import datetime
from urllib.parse import urlparse

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


def x_profile_url(username):
    username = str(username or "").strip().lstrip("@")
    if not username or username == "indisponivel":
        return None
    return f"https://x.com/{username}"


def format_x_user_link(username):
    username = str(username or "").strip().lstrip("@")
    if not username or username == "indisponivel":
        return "@indisponivel"
    return f'<a href="{escape_html(x_profile_url(username))}">@{escape_html(username)}</a>'


def first_list_value(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def username_from_x_url(value):
    if not value:
        return None

    value = str(value).strip()
    parsed = urlparse(value)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    if host not in {"x.com", "twitter.com"}:
        return None

    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts:
        return None

    username = path_parts[0].strip().lstrip("@")
    if username in {"i", "intent", "share", "hashtag", "search"}:
        return None
    return username or None


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
    raw = alert.get(f"{prefix}_affiliation_raw")
    url = alert.get(f"{prefix}_affiliation_url")

    if isinstance(raw, dict):
        name = name or raw.get("description") or raw.get("name") or raw.get("label")
        username = username or raw.get("username") or raw.get("screen_name") or raw.get("handle")
        url = url or raw.get("url")
    elif isinstance(raw, str):
        name = name or raw

    if username:
        username = str(first_list_value(username)).lstrip("@")

    username = username or username_from_x_url(first_list_value(url))

    if name and username:
        return f"{name} (@{username})"
    if name:
        return str(name)
    if username:
        return f"@{username}"
    return "presente"


def summary_affiliation_label(summary):
    if not isinstance(summary, dict) or not summary.get("affiliation_found"):
        return None

    name = summary.get("affiliation_name")
    username = summary.get("affiliation_username")
    raw = summary.get("affiliation_raw")
    url = summary.get("affiliation_url")

    if isinstance(raw, dict):
        name = name or raw.get("description") or raw.get("name") or raw.get("label")
        username = username or raw.get("username") or raw.get("screen_name") or raw.get("handle")
        url = url or raw.get("url")
    elif isinstance(raw, str):
        name = name or raw

    if username:
        username = str(first_list_value(username)).lstrip("@")

    username = username or username_from_x_url(first_list_value(url))

    if name and username:
        return f"{name} (@{username})"
    if name:
        return str(name)
    if username:
        return f"@{username}"
    return None


def best_affiliation_label(alert):
    label = affiliation_label(alert, "author")
    if label and label != "presente":
        return label

    label = summary_affiliation_label(alert.get("best_affiliation_author_summary") or {})
    if label:
        return label

    return "presente" if affiliation_label(alert, "author") else None


def format_affiliation_html(label):
    label = str(label or "").strip()
    if not label:
        return ""

    match = None
    if "(@" in label and label.endswith(")"):
        prefix, username_part = label.rsplit("(@", 1)
        username = username_part[:-1]
        match = prefix.strip(), username.strip()

    if match:
        name, username = match
        return f"{escape_html(name)} ({format_x_user_link(username)})"

    if label.startswith("@"):
        return format_x_user_link(label)

    return escape_html(label)


def reason_followers_value(reason):
    if not str(reason or "").startswith("author_followers_") or ">=" not in str(reason):
        return None
    try:
        return int(float(str(reason).split(">=", 1)[1]))
    except (TypeError, ValueError):
        return None


def follower_reason_level(reason):
    reason = str(reason or "")
    if not reason.startswith("author_followers_") or ">=" not in reason:
        return None
    return reason.split(">=", 1)[0].replace("author_followers_", "")


def follower_level_text(level):
    return {
        "medium": "boa audiência",
        "high": "grande audiência",
        "critical": "audiência muito alta",
    }.get(level, "audiência relevante")


def follower_reason_username(alert, followers):
    if followers is None:
        return None

    candidates = [
        {
            "username": alert.get("author_username"),
            "followers": alert.get("author_followers"),
        },
        alert.get("best_followers_author_summary") or {},
        alert.get("selected_origin_summary") or {},
        alert.get("best_affiliation_author_summary") or {},
    ]
    for candidate in candidates:
        try:
            candidate_followers = int(round(float(candidate.get("followers"))))
        except (TypeError, ValueError, AttributeError):
            continue
        if candidate_followers == followers:
            return candidate.get("username")
    return None


def follower_reason_lines(alert, limit=3):
    alert = alert or {}
    lines = []
    seen_usernames = set()

    for summary in alert.get("top_followers_author_summaries") or []:
        if not isinstance(summary, dict):
            continue
        username = summary.get("username")
        followers = summary.get("followers")
        reasons = summary.get("reasons") or []
        levels = [
            follower_reason_level(reason)
            for reason in reasons
            if follower_reason_level(reason)
        ]
        level = "critical" if "critical" in levels else "high" if "high" in levels else "medium" if "medium" in levels else None
        if not username or followers is None or not level:
            continue
        normalized = str(username).strip().lower().lstrip("@")
        if normalized in seen_usernames:
            continue
        seen_usernames.add(normalized)
        lines.append(
            f"autor com {follower_level_text(level)} {format_x_user_link(username)} "
            f"({escape_html(format_number(followers))} seguidores)"
        )
        if len(lines) >= limit:
            return lines
    if lines:
        return lines[:limit]

    follower_reasons = [
        reason
        for reason in (alert.get("alert_reasons") or [])
        if str(reason).startswith("author_followers_")
    ]
    if not follower_reasons:
        return lines

    best_reason = max(
        follower_reasons,
        key=lambda reason: reason_followers_value(reason) or 0,
    )
    followers = reason_followers_value(best_reason)
    level = follower_reason_level(best_reason)
    username = follower_reason_username(alert, followers)
    if username:
        lines.append(
            f"autor com {follower_level_text(level)} {format_x_user_link(username)} "
            f"({escape_html(format_number(followers))} seguidores)"
        )
    elif followers is not None:
        lines.append(
            f"autor com {follower_level_text(level)} "
            f"({escape_html(format_number(followers))} seguidores)"
        )
    return lines[:limit]


def format_alert_reason(reason, alert=None):
    alert = alert or {}
    reason = str(reason or "").strip()
    if not reason:
        return None

    if reason.startswith("author_followers_") and ">=" in reason:
        level, followers = reason.split(">=", 1)
        level = level.replace("author_followers_", "")
        followers_value = reason_followers_value(reason)
        level_text = {
            "medium": "boa audiência",
            "high": "grande audiência",
            "critical": "audiência muito alta",
        }.get(level, "audiência relevante")
        username = follower_reason_username(alert, followers_value)
        if username:
            return f"autor com {level_text} {format_x_user_link(username)} ({escape_html(format_number(followers))} seguidores)"
        return f"autor com {level_text} ({escape_html(format_number(followers))} seguidores)"

    if reason in {"author_affiliation", "author_affiliation_found"}:
        affiliation = best_affiliation_label(alert)
        if affiliation and affiliation != "presente":
            return f"autor afiliado a {format_affiliation_html(affiliation)}"
        return "autor afiliado a uma organização"

    reason_labels = {
        "post_found": "post mencionando o token encontrado no X",
        "author_verified_business": "conta verificada como organização",
        "verified_type_business": "conta verificada como organização",
        "author_verified_government": "conta verificada como governo",
        "verified_type_government": "conta verificada como governo",
        "automated_operator_detected": "post indica operador ou automação por trás do token",
    }
    if reason in reason_labels:
        return reason_labels[reason]

    return escape_html(reason.replace("_", " "))


def format_alert_reasons(reasons, alert=None):
    follower_lines = follower_reason_lines(alert)
    formatted = []
    for reason in (reasons or []):
        if str(reason).startswith("author_followers_") and follower_lines:
            continue
        formatted.append(format_alert_reason(reason, alert=alert))
    deduped = []
    seen = set()
    for reason in follower_lines + formatted:
        if not reason:
            continue
        normalized = str(reason).strip().lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(reason)
    formatted = deduped
    if not formatted:
        return "nenhum"
    return "\n" + "\n".join(f"- {reason}" for reason in formatted)


def normalized_username(value):
    return str(value or "").strip().lower().lstrip("@")


def first_trigger_post(alert):
    posts = alert.get("trigger_posts") or []
    if not posts:
        return {}

    affiliation_summary = alert.get("best_affiliation_author_summary") or {}
    affiliation_username = normalized_username(affiliation_summary.get("username"))
    if affiliation_username:
        for post in posts:
            if not isinstance(post, dict):
                continue
            if normalized_username(post.get("author_username")) == affiliation_username:
                return post

    first = posts[0]
    return first if isinstance(first, dict) else {}


def build_alert_message(alert, entry=None):
    entry = entry or {}
    trigger_post = first_trigger_post(alert)
    chain_id = alert.get("chain_id") or entry.get("chain_id") or entry.get("chain") or "unknown"
    token_address = alert.get("token_address") or entry.get("token_address")
    pair_label = token_pair_label(entry, chain_id)
    reasons = alert.get("alert_reasons") or []
    reason_text = format_alert_reasons(reasons, alert=alert)
    author = alert.get("author_username") or trigger_post.get("author_username") or "indisponivel"
    gmgn_url = build_gmgn_url(chain_id, token_address)
    post_url = trigger_post.get("url")
    affiliation = best_affiliation_label(alert)
    posts_found = alert.get("posts_found")
    if posts_found is None:
        posts_found = len(alert.get("trigger_posts") or [])

    lines = [
        "<b>KRPTO-V | Alerta social</b>",
        f"<b>Rank:</b> {escape_html(alert.get('alert_rank', 'indisponivel'))}",
        f"<b>Posts encontrados:</b> {escape_html(posts_found)}",
        f"<b>Token:</b> {escape_html(pair_label)}",
        f"<b>Chain:</b> {escape_html(chain_id)}",
        f"<b>Motivos:</b>{reason_text}",
        f"<b>Autor:</b> {format_x_user_link(author)}",
    ]

    if alert.get("author_followers") is not None:
        lines.append(f"<b>Seguidores:</b> {escape_html(format_number(alert.get('author_followers') or 0))}")
    verified_type = alert.get("author_verified_type")
    if verified_type or alert.get("author_verified"):
        lines.append(f"<b>Verificacao:</b> {escape_html(verified_type or 'verificada')}")

    if affiliation:
        lines.append(f"<b>Afiliação:</b> {format_affiliation_html(affiliation)}")
    if alert.get("automated_operator_username"):
        lines.append(f"<b>Operador:</b> {format_x_user_link(alert.get('automated_operator_username'))}")
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
