import argparse
import html
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOCIAL_POSTS_DIR = PROJECT_ROOT / "data" / "social_posts"
WATCHLIST_FILE = PROJECT_ROOT / "data" / "watchlist.json"
SOCIAL_ALERTS_FILE = PROJECT_ROOT / "data" / "social_alerts.json"
LEGACY_INPUT_FILE = PROJECT_ROOT / "data" / "x_test_response.json"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_json(input_file):
    with input_file.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_watchlist():
    if not WATCHLIST_FILE.exists():
        return {}

    try:
        with WATCHLIST_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        return {}

    if not isinstance(data, dict):
        return {}

    return data


def load_social_alerts():
    if not SOCIAL_ALERTS_FILE.exists():
        return []

    try:
        with SOCIAL_ALERTS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        return []

    if isinstance(data, list):
        return [alert for alert in data if isinstance(alert, dict)]

    if isinstance(data, dict):
        return [data]

    return []


def find_latest_social_posts_file():
    if not SOCIAL_POSTS_DIR.exists():
        return None

    files = [
        path
        for path in SOCIAL_POSTS_DIR.rglob("*.json")
        if path.is_file()
    ]

    if not files:
        return None

    return max(files, key=lambda path: path.stat().st_mtime)


def resolve_input_file(value):
    if value:
        input_file = Path(value)
        if not input_file.is_absolute():
            input_file = PROJECT_ROOT / input_file
        return input_file

    latest_file = find_latest_social_posts_file()
    if latest_file:
        return latest_file

    return LEGACY_INPUT_FILE


def unwrap_payload(payload):
    if isinstance(payload, dict) and isinstance(payload.get("response"), dict):
        return {
            "metadata": {
                "timestamp": payload.get("timestamp"),
                "token_address": payload.get("token_address"),
                "source": payload.get("source"),
            },
            "response": payload["response"],
            "format": "social_posts",
        }

    return {
        "metadata": {},
        "response": payload if isinstance(payload, dict) else {},
        "format": "legacy",
    }


def build_users_by_id(response):
    users = response.get("includes", {}).get("users", [])
    return {user["id"]: user for user in users if isinstance(user, dict) and "id" in user}


def filter_users_by_tweets(users_by_id, tweets):
    author_ids = {
        tweet.get("author_id")
        for tweet in tweets
        if isinstance(tweet, dict) and tweet.get("author_id")
    }
    return {
        user_id: user
        for user_id, user in users_by_id.items()
        if user_id in author_ids
    }


def format_bool(value):
    return "sim" if value else "nao"


def format_metric(value):
    try:
        return f"{int(value):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def short_text(value, max_length=220):
    value = " ".join(str(value or "").split())

    if len(value) <= max_length:
        return value

    return value[: max_length - 3].rstrip() + "..."


def get_tweet_url(tweet, author):
    username = author.get("username")
    tweet_id = tweet.get("id")

    if not username or not tweet_id:
        return None

    return f"https://x.com/{username}/status/{tweet_id}"


def get_post_score(tweet):
    score = tweet.get("krptov_post_score")

    if score is None:
        return None

    try:
        return float(score)
    except (TypeError, ValueError):
        return None


def format_score(score):
    if score is None:
        return "indisponivel"

    try:
        number = float(score)
    except (TypeError, ValueError):
        return str(score)

    if number.is_integer():
        return str(int(number))

    return f"{number:.2f}".replace(".", ",")


def get_reason_values(tweet):
    for key in (
        "krptov_score_reasons",
        "krptov_reasons",
        "krptov_alert_reasons",
        "post_score_reasons",
        "reasons",
    ):
        reasons = tweet.get(key)

        if isinstance(reasons, list):
            return [str(reason) for reason in reasons if reason]

        if isinstance(reasons, str) and reasons.strip():
            return [part.strip() for part in reasons.split(",") if part.strip()]

    return []


def format_alert_reasons(reasons):
    if not reasons:
        return "indisponivel"

    formatted = []
    legacy_markers = ("post_score", "bio_pattern", "blue", "author_badge")

    for reason in reasons:
        reason_text = str(reason)
        if any(marker in reason_text for marker in legacy_markers):
            reason_text = f"{reason_text} [legado/telemetria; nao e criterio atual]"
        formatted.append(reason_text)

    return ", ".join(formatted)


def get_alert_for_token(token_address):
    if not token_address:
        return None

    wanted = str(token_address).lower()
    matches = [
        alert for alert in load_social_alerts()
        if str(alert.get("token_address", "")).lower() == wanted
    ]

    if not matches:
        return None

    return sorted(matches, key=lambda alert: str(alert.get("timestamp", "")))[-1]


def build_summary(tweets, users_by_id, returned_posts=None, tracked_posts=None, alert_context=None):
    if returned_posts is None:
        returned_posts = len(tweets)

    if tracked_posts is None:
        tracked_posts = sum(1 for tweet in tweets if get_post_score(tweet) is not None)

    top_author = {}
    top_followers = None

    for tweet in tweets:
        author = users_by_id.get(tweet.get("author_id"), {})
        followers = author.get("public_metrics", {}).get("followers_count", 0)

        try:
            followers = int(followers)
        except (TypeError, ValueError):
            followers = 0

        if top_followers is None or followers > top_followers:
            top_author = author
            top_followers = followers

    scored_tweets = [
        (get_post_score(tweet), tweet)
        for tweet in tweets
        if get_post_score(tweet) is not None
    ]
    scored_tweets.sort(key=lambda item: item[0], reverse=True)

    best_score = scored_tweets[0][0] if scored_tweets else None
    alert_rank = None
    alert = False
    reasons = []

    if alert_context:
        alert_score = alert_context.get("best_post_score")
        alert_rank = alert_context.get("alert_rank")
        alert_reasons = alert_context.get("alert_reasons") or []

        if best_score is None and alert_score is not None:
            try:
                best_score = float(alert_score)
            except (TypeError, ValueError):
                best_score = alert_score

        if alert_rank is not None:
            alert = True

        if alert_reasons:
            reasons = [str(reason) for reason in alert_reasons]

    return {
        "returned_posts": returned_posts,
        "tracked_posts": tracked_posts,
        "top_author": top_author,
        "top_followers": top_followers or 0,
        "best_score": best_score,
        "alert": alert,
        "alert_rank": alert_rank,
        "reasons": reasons,
    }


def print_summary(summary):
    top_username = summary["top_author"].get("username", "unknown")
    reasons = format_alert_reasons(summary["reasons"])

    print("Resumo:")
    print(f"- Tracked posts: {summary['tracked_posts']} de {summary['returned_posts']} retornados")
    print(f"- Maior autor: @{top_username}, {format_metric(summary['top_followers'])} followers")

    if summary["best_score"] is not None:
        print(f"- Telemetria de post legado: {format_score(summary['best_score'])}")

    if summary["alert"]:
        rank = summary["alert_rank"] if summary["alert_rank"] is not None else summary["best_score"]
        print(f"- Alerta registrado: sim, rank {format_score(rank)}")
    else:
        print("- Alerta registrado: nao")

    print(f"- Motivos de origem/reputacao: {reasons}")
    print()


def print_user(author):
    user_metrics = author.get("public_metrics", {})
    username = author.get("username", "unknown")
    name = author.get("name", "unknown")
    verified = format_bool(author.get("verified", False))
    verified_type = author.get("verified_type")
    followers = user_metrics.get("followers_count", 0)
    user_created_at = author.get("created_at")
    description = author.get("description")
    affiliation = author.get("affiliation")

    print(f"User: @{username}")
    print(f"Name: {name}")
    print(f"Verified context: {verified}")

    if verified_type:
        print(f"Verified type context: {verified_type}")

    print(f"Followers: {format_metric(followers)}")

    if user_created_at:
        print(f"User created at: {user_created_at}")

    if affiliation:
        print(f"Affiliation context: {affiliation}")

    if description:
        print(f"Description context: {short_text(description)}")


def print_post(tweet, author):
    tweet_metrics = tweet.get("public_metrics", {})
    created_at = tweet.get("created_at", "unknown")
    text = html.unescape(tweet.get("text", ""))
    score = tweet.get("krptov_post_score")
    url = get_tweet_url(tweet, author)

    print("-" * 80)
    print_user(author)
    print(f"Tweet id: {tweet.get('id', 'unknown')}")
    print(f"Created at: {created_at}")

    if score is not None:
        print(f"Post metric telemetry (legacy): {score}")

    print(
        "Tweet metrics: "
        f"likes={format_metric(tweet_metrics.get('like_count', 0))} | "
        f"replies={format_metric(tweet_metrics.get('reply_count', 0))} | "
        f"retweets={format_metric(tweet_metrics.get('retweet_count', 0))} | "
        f"quotes={format_metric(tweet_metrics.get('quote_count', 0))} | "
        f"bookmarks={format_metric(tweet_metrics.get('bookmark_count', 0))} | "
        f"impressions={format_metric(tweet_metrics.get('impression_count', 0))}"
    )

    if url:
        print(f"URL: {url}")

    print()
    print("Tweet:")
    print(text)
    print()


def print_meta(response):
    meta = response.get("meta", {})

    if not meta:
        return

    result_count = meta.get("result_count")
    newest_id = meta.get("newest_id")
    oldest_id = meta.get("oldest_id")

    if result_count is not None:
        print(f"Result count: {result_count}")
    if newest_id:
        print(f"Newest id: {newest_id}")
    if oldest_id:
        print(f"Oldest id: {oldest_id}")


def get_watchlist_token(watchlist, token_address):
    if not token_address:
        return None

    token_address = str(token_address).lower()
    return watchlist.get(token_address) or watchlist.get(str(token_address))


def get_nested(data, keys, default=None):
    current = data

    for key in keys:
        if not isinstance(current, dict):
            return default

        current = current.get(key)

        if current is None:
            return default

    return current


def get_token_chain(token_data):
    if not isinstance(token_data, dict):
        return None

    return (
        token_data.get("chain_id")
        or get_nested(token_data, ["selected_pair", "chainId"])
        or get_nested(token_data, ["token_profile", "chainId"])
    )


def get_tracked_tweet_ids(token_data):
    if not isinstance(token_data, dict):
        return []

    tweet_ids = token_data.get("social_tracked_tweet_ids")

    if not isinstance(tweet_ids, list):
        return []

    return [str(tweet_id) for tweet_id in tweet_ids if tweet_id]


def filter_tracked_tweets_from_watchlist(tweets, token_address):
    watchlist = load_watchlist()
    token_data = get_watchlist_token(watchlist, token_address)
    tracked_ids = set(get_tracked_tweet_ids(token_data))

    if not token_data:
        return tweets, False, "token nao encontrado em data/watchlist.json"

    if not tracked_ids:
        return tweets, False, "social_tracked_tweet_ids nao encontrado ou vazio na watchlist"

    filtered = [
        tweet for tweet in tweets
        if str(tweet.get("id")) in tracked_ids
    ]
    return filtered, True, None


def print_report(input_file, payload, limit, tracked_only):
    unwrapped = unwrap_payload(payload)
    metadata = unwrapped["metadata"]
    response = unwrapped["response"]
    tweets = response.get("data", [])
    users_by_id = build_users_by_id(response)

    if not isinstance(tweets, list):
        tweets = []

    original_tweets = tweets
    original_tweet_count = len(original_tweets)
    alert_context = get_alert_for_token(metadata.get("token_address"))
    watchlist = load_watchlist()
    watch_token = get_watchlist_token(watchlist, metadata.get("token_address"))
    chain_id = get_token_chain(watch_token)
    tracked_filter_applied = False
    tracked_filter_warning = None

    if tracked_only:
        tweets, tracked_filter_applied, tracked_filter_warning = filter_tracked_tweets_from_watchlist(
            original_tweets,
            metadata.get("token_address"),
        )

    if tracked_filter_applied:
        users_by_id = filter_users_by_tweets(users_by_id, tweets)

    tracked_posts_count = len(tweets) if tracked_filter_applied else sum(
        1 for tweet in original_tweets if get_post_score(tweet) is not None
    )

    if limit is not None:
        tweets_to_print = tweets[:limit]
    else:
        tweets_to_print = tweets

    print("=== KRPTO-V | X Posts ===")
    print(f"Arquivo: {input_file}")
    print(f"Formato: {unwrapped['format']}")

    if metadata.get("timestamp"):
        print(f"Timestamp: {metadata['timestamp']}")
    if metadata.get("token_address"):
        print(f"Token: {metadata['token_address']}")
    if chain_id:
        print(f"Chain: {chain_id}")
    if metadata.get("source"):
        print(f"Source: {metadata['source']}")

    print(f"Posts: {len(tweets)}")
    if tracked_only:
        if tracked_filter_applied:
            print(f"Tracked posts: {len(tweets)} de {original_tweet_count} retornados")
        else:
            print(
                f"Filtro tracked-only: {tracked_filter_warning}; "
                f"mostrando todos os {original_tweet_count} retornados."
            )
    print(f"Usuarios: {len(users_by_id)}")
    print_meta(response)
    print()

    print_summary(
        build_summary(
            tweets if tracked_filter_applied else original_tweets,
            users_by_id,
            returned_posts=original_tweet_count,
            tracked_posts=tracked_posts_count,
            alert_context=alert_context,
        )
    )

    if not tweets:
        print("Nenhum post encontrado neste arquivo.")
        return

    for tweet in tweets_to_print:
        author = users_by_id.get(tweet.get("author_id"), {})
        print_post(tweet, author)

    if limit is not None and len(tweets) > limit:
        print(f"... {len(tweets) - limit} posts omitidos pelo limite informado.")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Imprime posts salvos pelo modulo social/X do KRPTO-V.",
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        help=(
            "Arquivo JSON salvo em data/social_posts/YYYY-MM-DD/token.json. "
            "Se omitido, usa o JSON mais recente em data/social_posts."
        ),
    )
    parser.add_argument(
        "--input",
        dest="input_file_option",
        help="Compatibilidade com a versao antiga: caminho do JSON de entrada.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limita a quantidade de posts impressos.",
    )
    parser.add_argument(
        "--tracked-only",
        action="store_true",
        help="Mostra apenas tweets cujos IDs estejam em social_tracked_tweet_ids na watchlist.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    input_value = args.input_file_option or args.input_file
    input_file = resolve_input_file(input_value)

    if not input_file.exists():
        raise SystemExit(f"Arquivo nao encontrado: {input_file}")

    payload = load_json(input_file)
    print_report(input_file, payload, args.limit, args.tracked_only)


if __name__ == "__main__":
    main()
