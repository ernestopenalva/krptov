import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests import HTTPError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
TOKEN_ADDRESS = "0xc3732E78d985C299E5E7688a03C92CA12402b027"


load_dotenv(PROJECT_ROOT / ".env")
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN")


def search_token_mentions(token_address):
    if not X_BEARER_TOKEN:
        raise RuntimeError("X_BEARER_TOKEN nao encontrado no arquivo .env")

    if token_address == "0xSEU_CA_AQUI":
        raise RuntimeError("Defina um Contract Address real em TOKEN_ADDRESS")

    headers = {
        "Authorization": f"Bearer {X_BEARER_TOKEN}",
    }

    params = {
        "query": f'"{token_address}"',
        "max_results": 10,
        "tweet.fields": "author_id,created_at,public_metrics",
        "expansions": "author_id",
        "user.fields": "username,name,verified,public_metrics",
    }

    response = requests.get(
        SEARCH_URL,
        headers=headers,
        params=params,
        timeout=30,
    )
    try:
        response.raise_for_status()
    except HTTPError as exc:
        error_file = DATA_DIR / "x_test_error.json"

        try:
            error_payload = response.json()
        except ValueError:
            error_payload = {"error": response.text}

        with error_file.open("w", encoding="utf-8") as f:
            json.dump(error_payload, f, ensure_ascii=False, indent=2)

        print()
        print(f"Erro da API do X: HTTP {response.status_code}")
        print(f"Detalhes salvos em: {error_file}")
        raise exc

    return response.json()


def main():
    print("=== KRPTO-V | X Search Test ===")

    result = search_token_mentions(TOKEN_ADDRESS)
    output_file = DATA_DIR / "x_test_response.json"

    with output_file.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    tweets = result.get("data", [])
    users = result.get("includes", {}).get("users", [])
    users_by_id = {user["id"]: user for user in users}

    print()
    print(f"Tweets encontrados: {len(tweets)}")

    for tweet in tweets:
        author = users_by_id.get(tweet.get("author_id"), {})
        user_metrics = author.get("public_metrics", {})

        print("-" * 50)
        print(f"User: @{author.get('username', 'unknown')}")
        print(f"Name: {author.get('name', 'unknown')}")
        print(f"Verified: {author.get('verified', False)}")
        print(f"Followers: {user_metrics.get('followers_count', 0)}")
        print(f"Created at: {tweet.get('created_at')}")
        print()
        print("Tweet:")
        print(tweet.get("text"))

    print()
    print(f"JSON salvo em: {output_file}")


if __name__ == "__main__":
    main()
