import argparse
import html
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOCIAL_POSTS_DIR = PROJECT_ROOT / "data" / "social_posts"
LEGACY_INPUT_FILE = PROJECT_ROOT / "data" / "x_test_response.json"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_json(input_file):
    with input_file.open("r", encoding="utf-8") as f:
        return json.load(f)


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

    if float(score).is_integer():
        return str(int(score))

    return f"{score:.2f}".replace(".", ",")


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


def build_summary(tweets, users_by_id):
    returned_posts = len(tweets)
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
    best_tweet = scored_tweets[0][1] if scored_tweets else {}
    alert = best_score is not None and best_score > 0
    reasons = get_reason_values(best_tweet)

    if alert and not reasons:
        best_author = users_by_id.get(best_tweet.get("author_id"), {})
        followers = best_author.get("public_metrics", {}).get("followers_count", 0)

        try:
            followers = int(followers)
        except (TypeError, ValueError):
            followers = 0

        if followers >= 10000:
            reasons.append("followers_high")
        reasons.append("post_score")

    return {
        "returned_posts": returned_posts,
        "tracked_posts": tracked_posts,
        "top_author": top_author,
        "top_followers": top_followers or 0,
        "best_score": best_score,
        "alert": alert,
        "reasons": reasons,
    }


def print_summary(summary):
    top_username = summary["top_author"].get("username", "unknown")
    reasons = ", ".join(summary["reasons"]) if summary["reasons"] else "indisponivel"

    print("Resumo:")
    print(f"- Tracked posts: {summary['tracked_posts']} de {summary['returned_posts']} retornados")
    print(f"- Maior autor: @{top_username}, {format_metric(summary['top_followers'])} followers")
    print(f"- Melhor post_score: {format_score(summary['best_score'])}")

    if summary["alert"]:
        print(f"- Alerta: sim, rank {format_score(summary['best_score'])}")
    else:
        print("- Alerta: nao")

    print(f"- Motivos: {reasons}")
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
    print(f"Verified: {verified}")

    if verified_type:
        print(f"Verified type: {verified_type}")

    print(f"Followers: {format_metric(followers)}")

    if user_created_at:
        print(f"User created at: {user_created_at}")

    if affiliation:
        print(f"Affiliation: {affiliation}")

    if description:
        print(f"Description: {short_text(description)}")


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
        print(f"KRPTO-V post score: {score}")

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
    tracked_tweets = [tweet for tweet in original_tweets if get_post_score(tweet) is not None]
    tracked_filter_applied = False

    if tracked_only and tracked_tweets:
        tweets = tracked_tweets
        tracked_filter_applied = True

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
    if metadata.get("source"):
        print(f"Source: {metadata['source']}")

    print(f"Posts: {len(tweets)}")
    if tracked_only:
        if tracked_filter_applied:
            print(f"Filtro tracked-only: {len(tweets)} de {original_tweet_count}")
        else:
            print(
                "Filtro tracked-only: nenhum post com krptov_post_score no arquivo; "
                f"mostrando todos os {original_tweet_count} retornados."
            )
    print(f"Usuarios: {len(users_by_id)}")
    print_meta(response)
    print()

    print_summary(build_summary(original_tweets, users_by_id))

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
        help="Mostra apenas posts com krptov_post_score.",
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
