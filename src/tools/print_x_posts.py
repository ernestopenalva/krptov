import argparse
import html
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT_FILE = PROJECT_ROOT / "data" / "x_test_response.json"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_response(input_file):
    with input_file.open("r", encoding="utf-8") as f:
        return json.load(f)


def build_users_by_id(response):
    users = response.get("includes", {}).get("users", [])
    return {user["id"]: user for user in users if "id" in user}


def format_bool(value):
    return "sim" if value else "nao"


def print_post(tweet, author):
    user_metrics = author.get("public_metrics", {})
    tweet_metrics = tweet.get("public_metrics", {})
    username = author.get("username", "unknown")
    name = author.get("name", "unknown")
    verified = format_bool(author.get("verified", False))
    followers = user_metrics.get("followers_count", 0)
    created_at = tweet.get("created_at", "unknown")
    text = html.unescape(tweet.get("text", ""))

    print("-" * 80)
    print(f"User: @{username}")
    print(f"Name: {name}")
    print(f"Verified: {verified}")
    print(f"Followers: {followers}")
    print(f"Created at: {created_at}")
    print(
        "Tweet metrics: "
        f"likes={tweet_metrics.get('like_count', 0)} | "
        f"replies={tweet_metrics.get('reply_count', 0)} | "
        f"retweets={tweet_metrics.get('retweet_count', 0)} | "
        f"quotes={tweet_metrics.get('quote_count', 0)} | "
        f"impressions={tweet_metrics.get('impression_count', 0)}"
    )
    print()
    print("Tweet:")
    print(text)
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Imprime posts do JSON bruto da busca no X."
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT_FILE,
        type=Path,
        help="Caminho do JSON bruto salvo pelo x_search_test.py.",
    )
    args = parser.parse_args()

    response = load_response(args.input)
    tweets = response.get("data", [])
    users_by_id = build_users_by_id(response)

    print("=== KRPTO-V | X Posts ===")
    print(f"Arquivo: {args.input}")
    print(f"Posts: {len(tweets)}")
    print(f"Usuarios: {len(users_by_id)}")
    print()

    for tweet in tweets:
        author = users_by_id.get(tweet.get("author_id"), {})
        print_post(tweet, author)


if __name__ == "__main__":
    main()
