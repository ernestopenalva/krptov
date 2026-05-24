import argparse
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests import HTTPError


X_SEARCH_RECENT_URL = "https://api.x.com/2/tweets/search/recent"
X_USER_BY_USERNAME_URL = "https://api.x.com/2/users/by/username/{username}"
SOCIAL_INFERENCE_VERSION = "krptov-social-inference-v1-x-ca-monitor-2026-05-22"
X_USER_FIELDS = "username,name,description,created_at,verified,verified_type,is_identity_verified,affiliation,public_metrics,protected,parody,url"

STATUS_NOVO = "novo"
STATUS_ATIVO = "ativo"
STATUS_DESCARTE = "descarte"
STATUS_REASON_SOCIAL_TIMEOUT = "social_timeout"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
WATCHLIST_LOCK_FILE = DATA_DIR / "watchlist.lock"
LATEST_SNAPSHOT_FILE = DATA_DIR / "social_inference_latest.json"
ALERTS_FILE = DATA_DIR / "social_alerts.json"
POSTS_DIR = DATA_DIR / "social_posts"

DEFAULT_CONFIG = {
    "enabled": True,
    "scoring_mode": "origin_reputation",
    "disable_post_metric_alerts": True,
    "monitoring_window_hours": 22,
    "max_posts_per_token": 8,
    "cycle_interval_seconds": 180,
    "max_new_tokens_per_day": 30,
    "followers_alert_threshold": 2000,
    "excluded_author_usernames": [
        "dexsignals",
    ],
    "badges": {
        "alert_on_affiliation": True,
        "alert_on_verified_business": True,
        "alert_on_verified_government": True,
        "ignore_blue_as_alert": True,
    },
    "automation": {
        "enabled": True,
        "detect_operator": True,
        "analyze_operator_profile": True,
        "operator_patterns": [
            "automatizado por @",
            "automated by @",
            "bot by @",
        ],
    },
    "author_followers_thresholds": {
        "medium": 2000,
        "high": 20000,
        "critical": 100000,
    },
}


def now():
    return datetime.now().replace(microsecond=0)


def to_iso(value):
    return value.isoformat()


def parse_iso(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError:
        return None


def parse_config_value(value):
    value = value.strip()

    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False

    try:
        return int(value)
    except ValueError:
        return value.strip('"').strip("'")


def merge_dict(base, updates):
    merged = base.copy()

    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dict(merged[key], value)
            continue
        merged[key] = value

    return merged


def load_simple_yaml_social_inference(config_file):
    config = {}
    stack = []
    in_social = False
    list_key = None

    for raw_line in config_file.read_text(encoding="utf-8").splitlines():
        line_without_comment = raw_line.split("#", 1)[0].rstrip()

        if not line_without_comment:
            continue

        stripped = line_without_comment.strip()
        indent = len(raw_line) - len(raw_line.lstrip(" "))

        if stripped == "social_inference:":
            in_social = True
            stack = [(0, config)]
            list_key = None
            continue

        if in_social and indent == 0 and not raw_line.startswith((" ", "\t")):
            break

        if not in_social:
            continue

        if stripped.startswith("- "):
            if list_key:
                parent = stack[-2][1] if len(stack) > 1 else stack[-1][1]
                if not isinstance(parent.get(list_key), list):
                    parent[list_key] = []
                parent[list_key].append(parse_config_value(stripped[2:]))
            continue

        if ":" not in stripped:
            continue

        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        current = stack[-1][1]

        if value == "":
            current[key] = {}
            stack.append((indent, current[key]))
            list_key = key
            continue

        current[key] = parse_config_value(value)
        list_key = None

    if isinstance(config.get("bio_patterns"), dict):
        config["bio_patterns"] = []

    return config


def load_config(config_file=CONFIG_FILE):
    config = DEFAULT_CONFIG.copy()

    if not Path(config_file).exists():
        return config

    loaded = load_simple_yaml_social_inference(Path(config_file))
    return merge_dict(config, loaded)


def ensure_directories():
    DATA_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    POSTS_DIR.mkdir(exist_ok=True)


def load_watchlist(path=WATCHLIST_FILE):
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("data/watchlist.json precisa ser um dict indexado por token.")

    return migrate_watchlist_keys(data)


def atomic_save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")

    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    os.replace(temp_path, path)


@contextmanager
def watchlist_lock(timeout_seconds=120, poll_seconds=0.2):
    DATA_DIR.mkdir(exist_ok=True)
    started_at = time.time()
    lock_handle = None

    while True:
        try:
            lock_handle = os.open(
                WATCHLIST_LOCK_FILE,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.write(lock_handle, str(os.getpid()).encode("utf-8"))
            break
        except FileExistsError:
            if time.time() - started_at >= timeout_seconds:
                raise TimeoutError(f"Timeout aguardando lock da watchlist: {WATCHLIST_LOCK_FILE}")
            time.sleep(poll_seconds)

    try:
        yield
    finally:
        if lock_handle is not None:
            os.close(lock_handle)
        try:
            WATCHLIST_LOCK_FILE.unlink()
        except FileNotFoundError:
            pass


def append_jsonl(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def write_log_lines(lines, current_time):
    log_file = LOGS_DIR / f"social_inference_{current_time.strftime('%Y-%m-%d')}.txt"

    with log_file.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
        f.write("\n")


def normalize_ethereum_address(address):
    if not isinstance(address, str):
        return None

    address = address.strip()

    if len(address) != 42:
        return None
    if not address.startswith("0x"):
        return None

    hex_part = address[2:]
    if not all(char in "0123456789abcdefABCDEF" for char in hex_part):
        return None

    return address.lower()


def make_watchlist_key(chain_id, token_address):
    normalized_address = normalize_ethereum_address(token_address)
    if not chain_id or not normalized_address:
        return None

    return f"{chain_id}:{normalized_address}"


def split_watchlist_key(key):
    if isinstance(key, str) and ":" in key:
        chain_id, token_address = key.split(":", 1)
        return chain_id, normalize_ethereum_address(token_address)

    return None, normalize_ethereum_address(key)


def migrate_watchlist_keys(watchlist):
    migrated = {}

    for key, entry in watchlist.items():
        if not isinstance(entry, dict):
            migrated[key] = entry
            continue

        token_key = key
        if ":" not in key:
            token_key = make_watchlist_key(entry.get("chain_id"), entry.get("token_address") or key) or key

        migrated[token_key] = entry

    return migrated


def load_bearer_token():
    load_dotenv(PROJECT_ROOT / ".env")
    return os.getenv("X_BEARER_TOKEN")


def build_x_query(token_address, config):
    excluded_usernames = [
        normalize_username(username)
        for username in config.get("excluded_author_usernames", [])
    ]
    excluded_usernames = [username for username in excluded_usernames if username]
    exclusions = " ".join(f"-from:{username}" for username in excluded_usernames)

    if not exclusions:
        return f'"{token_address}"'

    return f'"{token_address}" {exclusions}'


def search_token_mentions(token_address, bearer_token, max_results, config):
    api_max_results = max(10, int(max_results))
    headers = {
        "Authorization": f"Bearer {bearer_token}",
    }
    params = {
        "query": build_x_query(token_address, config),
        "max_results": api_max_results,
        "tweet.fields": "author_id,created_at,public_metrics,text",
        "expansions": "author_id",
        "user.fields": X_USER_FIELDS,
    }
    response = requests.get(
        X_SEARCH_RECENT_URL,
        headers=headers,
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def fetch_user_by_username(username, bearer_token):
    headers = {
        "Authorization": f"Bearer {bearer_token}",
    }
    params = {
        "user.fields": X_USER_FIELDS,
    }
    response = requests.get(
        X_USER_BY_USERNAME_URL.format(username=username),
        headers=headers,
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("data")


def save_x_error(response, token_address, current_time, chain_id=None):
    error_file = DATA_DIR / f"social_inference_error_{current_time.strftime('%Y-%m-%d')}.json"

    try:
        error_payload = response.json()
    except ValueError:
        error_payload = {"error": response.text}

    existing = []
    if error_file.exists():
        with error_file.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, list):
                existing = loaded

    existing.append(
        {
            "timestamp": to_iso(current_time),
            "token_address": token_address,
            "chain_id": chain_id,
            "status_code": response.status_code,
            "payload": error_payload,
        }
    )
    atomic_save_json(error_file, existing)


def save_raw_posts(token_address, response_payload, current_time, chain_id=None):
    date_stamp = current_time.strftime("%Y-%m-%d")
    file_stem = f"{chain_id}_{token_address}" if chain_id else token_address
    output_file = POSTS_DIR / date_stamp / f"{file_stem}.json"
    atomic_save_json(
        output_file,
        {
            "timestamp": to_iso(current_time),
            "token_address": token_address,
            "chain_id": chain_id,
            "source": X_SEARCH_RECENT_URL,
            "response": response_payload,
        },
    )


def normalize_bio(description):
    text = (description or "").lower()
    text = text.replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def find_bio_patterns(description, patterns):
    normalized = normalize_bio(description)
    return sorted({pattern for pattern in patterns if pattern.lower() in normalized})


def calculate_post_score(tweet, config):
    metrics = tweet.get("public_metrics") or {}
    weights = config["post_metric_weights"]
    score = 0

    for metric_name, weight in weights.items():
        score += int(metrics.get(metric_name) or 0) * int(weight)

    divisor = float(config["post_score"].get("divisor") or 1)
    max_score = float(config["post_score"].get("max_score") or 1000000)

    if divisor <= 0:
        divisor = 1

    return min(score / divisor, max_score)


def calculate_engagement_rate(tweet):
    metrics = tweet.get("public_metrics") or {}
    impressions = metrics.get("impression_count") or 0

    if impressions <= 0:
        return None

    engagement = (
        (metrics.get("like_count") or 0)
        + (metrics.get("retweet_count") or 0)
        + (metrics.get("reply_count") or 0)
        + (metrics.get("quote_count") or 0)
    )
    return round((engagement / impressions) * 100, 4)


def get_affiliation(user):
    affiliation = user.get("affiliation")
    if isinstance(affiliation, dict):
        return affiliation if affiliation else None
    return affiliation or None


def get_author_badge(user):
    if get_affiliation(user):
        return "affiliation"

    verified_type = user.get("verified_type")
    if verified_type:
        return verified_type

    if user.get("is_identity_verified") is True:
        return "identity_verified"

    if user.get("verified") is True:
        return "blue"

    return None


def get_followers_count(user):
    metrics = user.get("public_metrics") or {}
    return int(metrics.get("followers_count") or 0)


def follower_rank(followers_count, config):
    thresholds = config["author_followers_thresholds"]

    if followers_count >= int(thresholds["critical"]):
        return 60, "critical"
    if followers_count >= int(thresholds["high"]):
        return 40, "high"
    if followers_count >= int(thresholds["medium"]):
        return 20, "medium"

    return 0, None


def badge_rank(user, config):
    badges = config["badges"]

    if get_affiliation(user) and badges.get("alert_on_affiliation", True):
        return 100, "affiliation_found"

    verified_type = user.get("verified_type")
    if verified_type == "business" and badges.get("alert_on_verified_business", True):
        return 90, "verified_type_business"
    if verified_type == "government" and badges.get("alert_on_verified_government", True):
        return 85, "verified_type_government"

    return 0, None


def relevant_profile_signal(user, config, prefix):
    followers_count = get_followers_count(user)
    rank = 0
    reasons = []

    badge_signal_rank, badge_reason = badge_rank(user, config)
    if badge_signal_rank:
        rank = max(rank, badge_signal_rank)
        reasons.append(f"{prefix}_{badge_reason}")

    follower_signal_rank, follower_band = follower_rank(followers_count, config)
    if followers_count >= int(config["followers_alert_threshold"]):
        rank = max(rank, follower_signal_rank)
        reasons.append(f"{prefix}_followers_{follower_band}>={followers_count}")

    return rank, reasons


def normalize_username(value):
    if not value:
        return None

    username = str(value).strip().lstrip("@")
    username = re.sub(r"[^A-Za-z0-9_].*$", "", username)

    if not username:
        return None

    return username


def get_structured_automation_operator(user):
    for key in ["automated_by", "automation", "automated", "bot_operator", "operator"]:
        value = user.get(key)

        if isinstance(value, dict):
            username = (
                value.get("username")
                or value.get("screen_name")
                or value.get("handle")
            )
            if username:
                return normalize_username(username)

        if isinstance(value, str) and "@" in value:
            match = re.search(r"@([A-Za-z0-9_]{1,15})", value)
            if match:
                return normalize_username(match.group(1))

    return None


def get_fallback_automation_operator(user, config):
    text = " ".join(
        str(user.get(key) or "")
        for key in ["description", "name"]
    )
    normalized = text.lower()

    for pattern in config["automation"].get("operator_patterns", []):
        pattern_index = normalized.find(pattern.lower())
        if pattern_index < 0:
            continue

        candidate = text[pattern_index + len(pattern):]
        match = re.search(r"@?([A-Za-z0-9_]{1,15})", candidate)
        if match:
            return normalize_username(match.group(1))

    return None


def get_automation_operator_username(user, config):
    structured = get_structured_automation_operator(user)
    if structured:
        return structured

    return get_fallback_automation_operator(user, config)


def empty_origin_summary(origin_type="unknown"):
    return {
        "origin_type": origin_type,
        "author_username": None,
        "author_followers": 0,
        "author_verified": False,
        "author_verified_type": None,
        "author_affiliation_found": False,
        "automated_operator_detected": False,
        "automated_operator_username": None,
        "operator_followers": None,
        "operator_verified": None,
        "operator_verified_type": None,
        "operator_affiliation_found": None,
    }


def build_origin_summary(user, operator_user=None):
    summary = {
        "origin_type": "automated" if operator_user else "human",
        "author_username": user.get("username"),
        "author_followers": get_followers_count(user),
        "author_verified": bool(user.get("verified")),
        "author_verified_type": user.get("verified_type"),
        "author_affiliation_found": bool(get_affiliation(user)),
        "automated_operator_detected": operator_user is not None,
        "automated_operator_username": None,
        "operator_followers": None,
        "operator_verified": None,
        "operator_verified_type": None,
        "operator_affiliation_found": None,
    }

    if operator_user:
        summary["automated_operator_username"] = operator_user.get("username")
        summary["operator_followers"] = get_followers_count(operator_user)
        summary["operator_verified"] = bool(operator_user.get("verified"))
        summary["operator_verified_type"] = operator_user.get("verified_type")
        summary["operator_affiliation_found"] = bool(get_affiliation(operator_user))

    return summary


def build_social_analysis(response_payload, config, bearer_token=None):
    tweets = response_payload.get("data") or []
    users = response_payload.get("includes", {}).get("users", [])

    users_by_id = {user.get("id"): user for user in users}
    alert_reasons = []
    alert_rank = 0
    best_post_score = 0
    best_author_followers = 0
    origin_summary = empty_origin_summary()

    for tweet in tweets:
        tweet["krptov_engagement_rate"] = calculate_engagement_rate(tweet)

    for user in users:
        followers_count = get_followers_count(user)
        best_author_followers = max(best_author_followers, followers_count)

        user_rank, user_reasons = relevant_profile_signal(user, config, "author")
        if user_rank > alert_rank:
            origin_summary = build_origin_summary(user)
        alert_rank = max(alert_rank, user_rank)
        alert_reasons.extend(user_reasons)

        operator_user = None
        operator_username = None
        if config["automation"].get("enabled", True) and config["automation"].get("detect_operator", True):
            operator_username = get_automation_operator_username(user, config)

        if (
            operator_username
            and bearer_token
            and config["automation"].get("analyze_operator_profile", True)
        ):
            try:
                operator_user = fetch_user_by_username(operator_username, bearer_token)
            except Exception:
                operator_user = {"username": operator_username}

        if operator_username and operator_user is None:
            operator_user = {"username": operator_username}

        if operator_user:
            operator_rank, operator_reasons = relevant_profile_signal(operator_user, config, "operator")
            if operator_rank:
                operator_rank = 80
                operator_reasons.append("automated_operator_relevant")

            if operator_rank > alert_rank:
                origin_summary = build_origin_summary(user, operator_user)
            elif origin_summary["author_username"] == user.get("username"):
                origin_summary.update(build_origin_summary(user, operator_user))

            alert_rank = max(alert_rank, operator_rank)
            alert_reasons.extend(operator_reasons)

    alert_signature = None
    if alert_rank > 0:
        alert_signature = "|".join(sorted(set(alert_reasons)))

    return {
        "tweets": tweets,
        "users": users,
        "users_by_id": users_by_id,
        "posts_found": len(tweets),
        "users_found": len(users),
        "best_post_score": best_post_score,
        "best_author_followers": best_author_followers,
        "author_badge_found": origin_summary["author_verified_type"] in ["business", "government"],
        "affiliation_found": origin_summary["author_affiliation_found"],
        "bio_patterns_found": [],
        "origin_summary": origin_summary,
        "alert_rank": alert_rank,
        "alert_reasons": sorted(set(alert_reasons)),
        "alert_signature": alert_signature,
    }


def get_latest_tweet_id(tweets):
    numeric_ids = []

    for tweet in tweets:
        tweet_id = tweet.get("id")
        if tweet_id and str(tweet_id).isdigit():
            numeric_ids.append(int(tweet_id))

    if not numeric_ids:
        return None

    return str(max(numeric_ids))


def get_tweet_ids(tweets, limit=None):
    tweet_ids = [
        str(tweet.get("id"))
        for tweet in tweets
        if tweet.get("id")
    ]

    if limit is None:
        return tweet_ids

    return tweet_ids[: int(limit)]


def filter_response_to_tracked_posts(response_payload, tracked_tweet_ids):
    if not tracked_tweet_ids:
        return response_payload

    tracked = {str(tweet_id) for tweet_id in tracked_tweet_ids}
    filtered_payload = json.loads(json.dumps(response_payload))
    filtered_tweets = [
        tweet
        for tweet in filtered_payload.get("data", []) or []
        if str(tweet.get("id")) in tracked
    ]
    author_ids = {tweet.get("author_id") for tweet in filtered_tweets}
    filtered_users = [
        user
        for user in filtered_payload.get("includes", {}).get("users", []) or []
        if user.get("id") in author_ids
    ]

    filtered_payload["data"] = filtered_tweets
    filtered_payload.setdefault("includes", {})["users"] = filtered_users
    return filtered_payload


def start_social_monitoring(entry, current_time, config):
    started_at = current_time
    expires_at = started_at + timedelta(hours=int(config["monitoring_window_hours"]))

    entry["status"] = STATUS_ATIVO
    entry["social_monitoring_started_at"] = to_iso(started_at)
    entry["social_monitoring_expires_at"] = to_iso(expires_at)


def needs_social_monitoring_start(entry):
    return not entry.get("social_monitoring_started_at") or not entry.get("social_monitoring_expires_at")


def expire_social_monitoring(entry, current_time):
    entry["status"] = STATUS_DESCARTE
    entry["status_reason"] = STATUS_REASON_SOCIAL_TIMEOUT
    entry["social_monitoring_completed_at"] = to_iso(current_time)


def build_alert(token_address, entry, analysis, current_time, status_before, watchlist_key=None):
    origin = analysis["origin_summary"]

    return {
        "timestamp": to_iso(current_time),
        "token_address": token_address,
        "chain_id": entry.get("chain_id"),
        "watchlist_key": watchlist_key or entry.get("watchlist_key"),
        "status_before": status_before,
        "status_after": entry.get("status"),
        "alert_rank": analysis["alert_rank"],
        "alert_reason": analysis["alert_signature"],
        "alert_signature": analysis["alert_signature"],
        "alert_reasons": analysis["alert_reasons"],
        "best_post_score": analysis["best_post_score"],
        "best_author_followers": analysis["best_author_followers"],
        "author_badge_found": analysis["author_badge_found"],
        "affiliation_found": analysis["affiliation_found"],
        "bio_patterns_found": analysis["bio_patterns_found"],
        "origin_type": origin["origin_type"],
        "author_username": origin["author_username"],
        "author_followers": origin["author_followers"],
        "author_verified": origin["author_verified"],
        "author_verified_type": origin["author_verified_type"],
        "author_affiliation_found": origin["author_affiliation_found"],
        "automated_operator_detected": origin["automated_operator_detected"],
        "automated_operator_username": origin["automated_operator_username"],
        "operator_followers": origin["operator_followers"],
        "operator_verified": origin["operator_verified"],
        "operator_verified_type": origin["operator_verified_type"],
        "operator_affiliation_found": origin["operator_affiliation_found"],
        "social_monitoring_started_at": entry.get("social_monitoring_started_at"),
        "social_monitoring_expires_at": entry.get("social_monitoring_expires_at"),
        "telegram_alert_sent": True,
    }


def load_alerts():
    if not ALERTS_FILE.exists():
        return []

    with ALERTS_FILE.open("r", encoding="utf-8") as f:
        loaded = json.load(f)

    if isinstance(loaded, list):
        return loaded
    if isinstance(loaded, dict):
        return loaded.get("alerts", [])
    return []


def daily_usage_file(current_time):
    return DATA_DIR / f"social_inference_usage_{current_time.strftime('%Y-%m-%d')}.json"


def load_daily_usage(current_time):
    path = daily_usage_file(current_time)
    default_usage = {
        "date": current_time.strftime("%Y-%m-%d"),
        "new_tokens_started": 0,
        "tokens_started": [],
    }

    if not path.exists():
        return default_usage

    with path.open("r", encoding="utf-8") as f:
        loaded = json.load(f)

    if not isinstance(loaded, dict):
        return default_usage

    loaded.setdefault("date", default_usage["date"])
    loaded.setdefault("new_tokens_started", 0)
    loaded.setdefault("tokens_started", [])
    return loaded


def save_daily_usage(current_time, usage):
    atomic_save_json(daily_usage_file(current_time), usage)


def can_start_new_social_token(config, usage):
    max_new_tokens = int(config.get("max_new_tokens_per_day") or 0)
    if max_new_tokens <= 0:
        return True

    return int(usage.get("new_tokens_started") or 0) < max_new_tokens


def register_new_social_token_started(token_address, current_time, usage, chain_id=None, watchlist_key=None):
    usage["new_tokens_started"] = int(usage.get("new_tokens_started") or 0) + 1
    usage.setdefault("tokens_started", []).append(
        {
            "token_address": token_address,
            "chain_id": chain_id,
            "watchlist_key": watchlist_key,
            "started_at": to_iso(current_time),
        }
    )


def should_generate_alert(entry, analysis):
    current_rank = int(analysis["alert_rank"] or 0)
    best_rank = int(entry.get("best_alert_rank") or 0)
    return current_rank > best_rank


def apply_alert(entry, analysis, current_time):
    entry["telegram_alert_sent"] = True
    entry["last_alert_at"] = to_iso(current_time)
    entry["last_alert_level"] = analysis["alert_rank"]
    entry["last_alert_reason"] = "; ".join(analysis["alert_reasons"])
    entry["best_social_score"] = max(
        float(entry.get("best_social_score") or 0),
        float(analysis["best_post_score"] or 0),
    )
    entry["best_alert_rank"] = analysis["alert_rank"]
    entry["last_alert_signature"] = analysis["alert_signature"]


def build_history_record(token_address, status_before, entry, analysis, alert_generated, current_time, watchlist_key=None):
    origin = analysis["origin_summary"]

    return {
        "timestamp": to_iso(current_time),
        "token_address": token_address,
        "chain_id": entry.get("chain_id"),
        "watchlist_key": watchlist_key or entry.get("watchlist_key"),
        "status_before": status_before,
        "status_after": entry.get("status"),
        "posts_found": analysis["posts_found"],
        "users_found": analysis["users_found"],
        "best_post_score": analysis["best_post_score"],
        "best_author_followers": analysis["best_author_followers"],
        "author_badge_found": analysis["author_badge_found"],
        "affiliation_found": analysis["affiliation_found"],
        "bio_patterns_found": analysis["bio_patterns_found"],
        "origin_type": origin["origin_type"],
        "author_username": origin["author_username"],
        "author_followers": origin["author_followers"],
        "author_verified": origin["author_verified"],
        "author_verified_type": origin["author_verified_type"],
        "author_affiliation_found": origin["author_affiliation_found"],
        "automated_operator_detected": origin["automated_operator_detected"],
        "automated_operator_username": origin["automated_operator_username"],
        "operator_followers": origin["operator_followers"],
        "operator_verified": origin["operator_verified"],
        "operator_verified_type": origin["operator_verified_type"],
        "operator_affiliation_found": origin["operator_affiliation_found"],
        "alert_generated": alert_generated,
        "alert_rank": analysis["alert_rank"],
        "alert_reasons": analysis["alert_reasons"],
    }


def social_managed_update(entry):
    prefixes = (
        "social_",
        "telegram_alert_",
        "last_alert_",
    )
    exact_keys = {
        "best_social_score",
        "best_alert_rank",
        "social_latest_tweet_id",
        "social_total_posts_seen",
        "social_tracked_tweet_ids",
        "social_tracked_posts_count",
    }
    update = {
        key: value
        for key, value in entry.items()
        if key.startswith(prefixes) or key in exact_keys
    }

    if entry.get("status") == STATUS_ATIVO:
        update["status"] = STATUS_ATIVO
    elif entry.get("status_reason") == STATUS_REASON_SOCIAL_TIMEOUT:
        update["status"] = STATUS_DESCARTE
        update["status_reason"] = STATUS_REASON_SOCIAL_TIMEOUT

    return update


def merge_social_updates(current_watchlist, social_watchlist):
    merged = current_watchlist.copy()

    for token_address, social_entry in social_watchlist.items():
        current_entry = merged.get(token_address)
        if not isinstance(current_entry, dict):
            merged[token_address] = social_entry
            continue

        current_entry.update(social_managed_update(social_entry))

    return merged


def empty_analysis():
    return {
        "posts_found": 0,
        "users_found": 0,
        "best_post_score": 0,
        "best_author_followers": 0,
        "author_badge_found": False,
        "affiliation_found": False,
        "bio_patterns_found": [],
        "origin_summary": empty_origin_summary(),
        "alert_rank": 0,
        "alert_reasons": [],
    }


def print_summary(snapshot):
    lines = [
        "=== KRPTO-V | Social Inference ===",
        f"Versao: {snapshot['version']}",
        f"Ciclo: {snapshot['timestamp']}",
        f"Tokens verificados: {snapshot['tokens_checked']}",
        f"Alertas gerados: {snapshot['alerts_generated']}",
        f"Tokens expirados: {snapshot['tokens_expired']}",
        f"Tokens bloqueados pelo limite diario: {snapshot.get('tokens_blocked_by_daily_limit', 0)}",
        f"Erros: {len(snapshot['errors'])}",
    ]

    for line in lines:
        print(line)

    return lines


def run_cycle(config_file=CONFIG_FILE):
    ensure_directories()

    config = load_config(Path(config_file))
    current_time = now()
    now_text = to_iso(current_time)
    date_stamp = current_time.strftime("%Y-%m-%d")
    errors = []

    if not config.get("enabled", True):
        snapshot = {
            "timestamp": now_text,
            "version": SOCIAL_INFERENCE_VERSION,
            "enabled": False,
    "tokens_checked": 0,
            "alerts_generated": 0,
            "tokens_expired": 0,
            "tokens_blocked_by_daily_limit": 0,
            "errors": [],
        }
        atomic_save_json(LATEST_SNAPSHOT_FILE, snapshot)
        write_log_lines(["=== KRPTO-V | Social Inference ===", "Modulo desabilitado no config."], current_time)
        return snapshot

    bearer_token = load_bearer_token()
    if not bearer_token:
        error = "X_BEARER_TOKEN nao encontrado no arquivo .env. Modulo social encerrado sem alterar a watchlist."
        snapshot = {
            "timestamp": now_text,
            "version": SOCIAL_INFERENCE_VERSION,
            "enabled": True,
            "tokens_checked": 0,
            "alerts_generated": 0,
            "tokens_expired": 0,
            "tokens_blocked_by_daily_limit": 0,
            "errors": [error],
        }
        atomic_save_json(LATEST_SNAPSHOT_FILE, snapshot)
        write_log_lines(["=== KRPTO-V | Social Inference ===", error], current_time)
        print(error)
        return snapshot

    alerts = load_alerts()
    daily_usage = load_daily_usage(current_time)
    tokens_checked = 0
    alerts_generated = 0
    tokens_expired = 0
    tokens_blocked_by_daily_limit = 0
    with watchlist_lock():
        watchlist = load_watchlist(WATCHLIST_FILE)

        for watchlist_key, entry in watchlist.items():
            key_chain_id, key_token_address = split_watchlist_key(watchlist_key)
            chain_id = entry.get("chain_id") or key_chain_id
            normalized_address = normalize_ethereum_address(entry.get("token_address")) or key_token_address
            if not normalized_address:
                continue

            if chain_id:
                entry["chain_id"] = chain_id
                entry["watchlist_key"] = make_watchlist_key(chain_id, normalized_address)
            entry["token_address"] = normalized_address

            status_before = entry.get("status")
            if status_before not in [STATUS_NOVO, STATUS_ATIVO]:
                continue

            starting_new_social_token = status_before == STATUS_NOVO or needs_social_monitoring_start(entry)
            if starting_new_social_token and not can_start_new_social_token(config, daily_usage):
                entry["social_last_skipped_at"] = now_text
                entry["social_skip_reason"] = "daily_new_token_limit"
                tokens_blocked_by_daily_limit += 1
                continue

            expires_at = parse_iso(entry.get("social_monitoring_expires_at"))
            if status_before == STATUS_ATIVO and expires_at and current_time >= expires_at:
                expire_social_monitoring(entry, current_time)
                tokens_expired += 1
                history_record = build_history_record(
                    token_address=normalized_address,
                    status_before=status_before,
                    entry=entry,
                    analysis=empty_analysis(),
                    alert_generated=False,
                    current_time=current_time,
                    watchlist_key=watchlist_key,
                )
                append_jsonl(DATA_DIR / f"social_inference_{date_stamp}.jsonl", history_record)
                continue

            try:
                response_payload = search_token_mentions(
                    token_address=normalized_address,
                    bearer_token=bearer_token,
                    max_results=config["max_posts_per_token"],
                    config=config,
                )
            except HTTPError as exc:
                response = exc.response
                if response is not None:
                    save_x_error(response, normalized_address, current_time, chain_id=chain_id)
                    errors.append(f"{normalized_address}: HTTP {response.status_code}")
                else:
                    errors.append(f"{normalized_address}: HTTPError sem response")
                continue
            except Exception as exc:
                errors.append(f"{normalized_address}: {exc}")
                continue

            if starting_new_social_token:
                start_social_monitoring(entry, current_time, config)
                register_new_social_token_started(
                    normalized_address,
                    current_time,
                    daily_usage,
                    chain_id=chain_id,
                    watchlist_key=watchlist_key,
                )

            entry["social_last_checked_at"] = now_text

            save_raw_posts(normalized_address, response_payload, current_time, chain_id=chain_id)
            tracked_tweet_ids = entry.get("social_tracked_tweet_ids") or []
            if not tracked_tweet_ids:
                tracked_tweet_ids = get_tweet_ids(
                    response_payload.get("data") or [],
                    limit=config["max_posts_per_token"],
                )
                entry["social_tracked_tweet_ids"] = tracked_tweet_ids
                entry["social_tracked_posts_count"] = len(tracked_tweet_ids)
                entry["social_total_posts_seen"] = len(tracked_tweet_ids)

            analysis_payload = filter_response_to_tracked_posts(response_payload, tracked_tweet_ids)
            analysis = build_social_analysis(analysis_payload, config, bearer_token=bearer_token)
            tokens_checked += 1
            latest_tweet_id = get_latest_tweet_id(analysis["tweets"])
            if latest_tweet_id:
                entry["social_latest_tweet_id"] = latest_tweet_id

            alert_generated = False
            if should_generate_alert(entry, analysis):
                apply_alert(entry, analysis, current_time)
                alert = build_alert(
                    normalized_address,
                    entry,
                    analysis,
                    current_time,
                    status_before,
                    watchlist_key=watchlist_key,
                )
                alerts.append(alert)
                append_jsonl(DATA_DIR / f"social_alerts_{date_stamp}.jsonl", alert)
                alerts_generated += 1
                alert_generated = True
            else:
                entry["best_social_score"] = max(
                    float(entry.get("best_social_score") or 0),
                    float(analysis["best_post_score"] or 0),
                )

            history_record = build_history_record(
                token_address=normalized_address,
                status_before=status_before,
                entry=entry,
                analysis=analysis,
                alert_generated=alert_generated,
                current_time=current_time,
                watchlist_key=watchlist_key,
            )
            append_jsonl(DATA_DIR / f"social_inference_{date_stamp}.jsonl", history_record)

        latest_watchlist = load_watchlist(WATCHLIST_FILE)
        atomic_save_json(WATCHLIST_FILE, merge_social_updates(latest_watchlist, watchlist))

    snapshot = {
        "timestamp": now_text,
        "version": SOCIAL_INFERENCE_VERSION,
        "enabled": True,
        "tokens_checked": tokens_checked,
        "alerts_generated": alerts_generated,
        "tokens_expired": tokens_expired,
        "tokens_blocked_by_daily_limit": tokens_blocked_by_daily_limit,
        "errors": errors,
    }

    atomic_save_json(ALERTS_FILE, alerts)
    save_daily_usage(current_time, daily_usage)
    atomic_save_json(LATEST_SNAPSHOT_FILE, snapshot)

    summary_lines = print_summary(snapshot)
    write_log_lines(summary_lines + errors, current_time)

    return snapshot


def run_social_inference():
    return run_cycle()


def run_loop(config_file=CONFIG_FILE):
    while True:
        config = load_config(Path(config_file))
        interval_seconds = int(config.get("cycle_interval_seconds") or 60)

        try:
            run_cycle(config_file=config_file)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            current_time = now()
            message = f"Erro no ciclo social: {exc}"
            print(message)
            write_log_lines(["=== KRPTO-V | Social Inference ===", message], current_time)

        time.sleep(interval_seconds)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Executa o modulo de inferencia social do KRPTO-V."
    )
    parser.add_argument(
        "--config",
        default=CONFIG_FILE,
        type=Path,
        help="Caminho do config.yaml.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Mantem o modulo rodando em ciclos continuos.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.loop:
        run_loop(config_file=args.config)
        return

    run_cycle(config_file=args.config)


if __name__ == "__main__":
    main()
