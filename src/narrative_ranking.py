import argparse
import html
import json
import math
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_INPUT_FILE = PROJECT_ROOT / "data" / "x_test_response.json"
ETH_ADDRESS_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_configs(config_dir):
    return {
        "entity_weights": load_json(config_dir / "entity_weights.json"),
        "metric_weights": load_json(config_dir / "metric_weights.json"),
        "attribute_weights": load_json(config_dir / "attribute_weights.json"),
        "entity_rules": load_json(config_dir / "entity_rules.json"),
    }


def followers_score(followers_count):
    if followers_count <= 100:
        return 1
    if followers_count <= 1000:
        return 2
    if followers_count <= 5000:
        return 3
    if followers_count <= 20000:
        return 4
    return 5


def iter_keyword_rules(keywords):
    if isinstance(keywords, dict):
        return keywords.items()

    return ((keyword, 1) for keyword in keywords)


def infer_entity(text, entity_rules):
    text_lower = text.lower()
    entity_scores = {}

    for entity_name, entity_data in entity_rules.items():
        entity_score = 0

        for keyword, keyword_weight in iter_keyword_rules(
            entity_data.get("keywords", {})
        ):
            if keyword.lower() in text_lower:
                entity_score += keyword_weight

        if entity_score > 0:
            entity_scores[entity_name] = entity_score

    if not entity_scores:
        return {
            "entity": "organic",
            "entity_rule_scores": {},
        }

    dominant_entity = max(
        entity_scores.items(),
        key=lambda item: (item[1], item[0]),
    )[0]

    return {
        "entity": dominant_entity,
        "entity_rule_scores": entity_scores,
    }


def calculate_metric_score(metrics, metric_weights):
    impressions = metrics.get("impression_count", 0)

    score = 0
    score += metrics.get("like_count", 0) * metric_weights["likes"]
    score += metrics.get("reply_count", 0) * metric_weights["replies"]
    score += metrics.get("retweet_count", 0) * metric_weights["retweets"]
    score += metrics.get("quote_count", 0) * metric_weights["quotes"]
    score += math.log10(impressions + 1) * metric_weights["impressions"]

    return score


def calculate_post_score(tweet, user, configs):
    text = html.unescape(tweet.get("text", ""))
    entity_data = infer_entity(text, configs["entity_rules"])
    entity = entity_data["entity"]
    entity_weight = configs["entity_weights"].get(entity, 1)

    followers_count = user.get("public_metrics", {}).get("followers_count", 0)
    follower_score = (
        followers_score(followers_count)
        * configs["attribute_weights"]["followers"]
    )
    metric_score = calculate_metric_score(
        tweet.get("public_metrics", {}),
        configs["metric_weights"],
    )
    post_score = entity_weight * (follower_score + metric_score)

    return {
        "tweet_id": tweet.get("id", "unknown"),
        "username": user.get("username", "unknown"),
        "entity": entity,
        "entity_rule_scores": entity_data["entity_rule_scores"],
        "entity_weight": entity_weight,
        "followers": followers_count,
        "follower_score": round(follower_score, 2),
        "metric_score": round(metric_score, 2),
        "post_score": round(post_score, 2),
    }


def build_users_by_id(response):
    users = response.get("includes", {}).get("users", [])
    return {user["id"]: user for user in users if "id" in user}


def extract_token_addresses(response):
    addresses = {}

    for tweet in response.get("data", []):
        text = html.unescape(tweet.get("text", ""))
        for address in ETH_ADDRESS_RE.findall(text):
            normalized_address = address.lower()
            addresses[normalized_address] = addresses.get(normalized_address, 0) + 1

    return addresses


def infer_token_key(response, token_ca=None):
    if token_ca:
        return token_ca

    addresses = extract_token_addresses(response)
    if not addresses:
        return "UNKNOWN_TOKEN"

    return max(addresses.items(), key=lambda item: item[1])[0]


def calculate_token_scores(response, configs, token_ca=None):
    tweets = response.get("data", [])
    users_by_id = build_users_by_id(response)

    token_key = infer_token_key(response, token_ca=token_ca)
    token_scores = {
        token_key: {
            "token_score": 0,
            "posts": [],
            "entities": {},
        }
    }

    for tweet in tweets:
        user = users_by_id.get(tweet.get("author_id"))
        if not user:
            continue

        post_data = calculate_post_score(tweet, user, configs)
        token_scores[token_key]["token_score"] += post_data["post_score"]
        token_scores[token_key]["posts"].append(post_data)

        entity = post_data["entity"]
        token_scores[token_key]["entities"][entity] = (
            token_scores[token_key]["entities"].get(entity, 0) + 1
        )

    return token_scores


def print_ranking(token_scores, show_posts=False):
    ranking = sorted(
        token_scores.items(),
        key=lambda item: item[1]["token_score"],
        reverse=True,
    )

    print()
    print("=== KRPTO-V Narrative Ranking ===")
    print()

    for index, (token, data) in enumerate(ranking, start=1):
        print(f"#{index}")
        print(f"Token: {token}")
        print(f"Score: {round(data['token_score'], 2)}")
        print(f"Posts analisados: {len(data['posts'])}")
        print(f"Entidades: {format_entities(data['entities'])}")

        if show_posts:
            print()
            for post in sorted(
                data["posts"],
                key=lambda item: item["post_score"],
                reverse=True,
            ):
                print(
                    f"  @{post['username']} | "
                    f"{post['entity']} | "
                    f"post_score={post['post_score']} | "
                    f"followers={post['followers']} | "
                    f"metric_score={post['metric_score']} | "
                    f"entity_rules={format_rule_scores(post['entity_rule_scores'])}"
                )

        print("-" * 50)


def format_entities(entities):
    if not entities:
        return "nenhuma"

    return ", ".join(
        f"{entity}={count}"
        for entity, count in sorted(entities.items())
    )


def format_rule_scores(rule_scores):
    if not rule_scores:
        return "organic_fallback"

    return ", ".join(
        f"{entity}:{score}"
        for entity, score in sorted(rule_scores.items())
    )


def main():
    parser = argparse.ArgumentParser(
        description="Calcula ranking narrativo a partir do JSON bruto da busca no X."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=DEFAULT_INPUT_FILE,
        type=Path,
        help="Caminho do JSON bruto salvo pelo x_search_test.py.",
    )
    parser.add_argument(
        "--show-posts",
        action="store_true",
        help="Mostra o score de cada post analisado.",
    )
    parser.add_argument(
        "--token-ca",
        help="Contract address pesquisado no X. Se omitido, usa o CA mais frequente nos posts.",
    )
    args = parser.parse_args()

    configs = load_configs(CONFIG_DIR)
    response = load_json(args.input)
    token_scores = calculate_token_scores(response, configs, token_ca=args.token_ca)

    print_ranking(token_scores, show_posts=args.show_posts)


if __name__ == "__main__":
    main()
