import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from textwrap import dedent


DEXSCREENER_LATEST_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chain_id}/{token_address}"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


HELP_EPILOG = """\
O que esta ferramenta faz:
  - Busca os token profiles mais recentes da Dexscreener.
  - Conta quais chains apareceram no retorno.
  - Filtra fora tokens da Solana.
  - Para cada token nao-Solana, busca os pares negociaveis.
  - Resume liquidez, volume, transacoes, variacao de preco, FDV e market cap.
  - Escolhe o melhor par de cada token usando maior liquidez como criterio.

Arquivos gerados:
  - data/dexscreener_latest_non_solana_enriched.json
    Guarda o ultimo ciclo enriquecido.
  - data/krptov_dexscreener_YYYY-MM-DD.jsonl
    Guarda o historico do dia, uma linha por ciclo.

Exemplos:
  python src/tools/dexscreener_non_solana_test.py
  python src/tools/dexscreener_non_solana_test.py --help
"""


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def today_stamp():
    return datetime.now().strftime("%Y-%m-%d")


def fetch_latest_profiles():
    import requests

    response = requests.get(DEXSCREENER_LATEST_PROFILES_URL, timeout=20)
    response.raise_for_status()
    return response.json()


def fetch_token_pairs(chain_id, token_address):
    import requests

    url = DEXSCREENER_TOKEN_PAIRS_URL.format(
        chain_id=chain_id,
        token_address=token_address,
    )

    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.json()


def get_nested_number(data, path, default=0):
    current = data

    for key in path:
        if not isinstance(current, dict):
            return default

        current = current.get(key)

        if current is None:
            return default

    try:
        return float(current)
    except (TypeError, ValueError):
        return default


def summarize_pair(pair):
    return {
        "chain_id": pair.get("chainId"),
        "dex_id": pair.get("dexId"),
        "pair_address": pair.get("pairAddress"),
        "url": pair.get("url"),
        "base_token": pair.get("baseToken"),
        "quote_token": pair.get("quoteToken"),
        "price_usd": pair.get("priceUsd"),
        "liquidity_usd": get_nested_number(pair, ["liquidity", "usd"]),
        "volume_m5": get_nested_number(pair, ["volume", "m5"]),
        "volume_h1": get_nested_number(pair, ["volume", "h1"]),
        "volume_h24": get_nested_number(pair, ["volume", "h24"]),
        "txns_m5": pair.get("txns", {}).get("m5"),
        "txns_h1": pair.get("txns", {}).get("h1"),
        "price_change_m5": get_nested_number(pair, ["priceChange", "m5"]),
        "price_change_h1": get_nested_number(pair, ["priceChange", "h1"]),
        "fdv": pair.get("fdv"),
        "market_cap": pair.get("marketCap"),
        "pair_created_at": pair.get("pairCreatedAt"),
    }


def enrich_tokens(tokens):
    import requests

    enriched = []

    for index, token in enumerate(tokens, start=1):
        chain_id = token.get("chainId")
        token_address = token.get("tokenAddress")

        print(f"[{index}/{len(tokens)}] Enriquecendo {chain_id}: {token_address}")

        try:
            pairs = fetch_token_pairs(chain_id, token_address)
            summarized_pairs = [summarize_pair(pair) for pair in pairs]

            best_pair = None
            if summarized_pairs:
                best_pair = max(
                    summarized_pairs,
                    key=lambda pair: pair.get("liquidity_usd", 0),
                )

            enriched.append({
                "token_profile": token,
                "pairs_count": len(summarized_pairs),
                "pairs": summarized_pairs,
                "best_pair": best_pair,
            })

        except requests.RequestException as exc:
            enriched.append({
                "token_profile": token,
                "error": str(exc),
                "pairs_count": 0,
                "pairs": [],
                "best_pair": None,
            })

    return enriched


def parse_args():
    parser = argparse.ArgumentParser(
        description="Descobre e enriquece token profiles nao-Solana da Dexscreener.",
        epilog=dedent(HELP_EPILOG),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    return parser.parse_args()


def main():
    parse_args()

    print("=== KRPTO-V | Dexscreener discovery ===")

    generated_at = now_iso()
    date_stamp = today_stamp()

    tokens = fetch_latest_profiles()

    chain_counter = Counter(
        token.get("chainId", "unknown")
        for token in tokens
    )

    non_solana_tokens = [
        token for token in tokens
        if token.get("chainId") != "solana"
    ]

    enriched_non_solana = enrich_tokens(non_solana_tokens)

    payload = {
        "generated_at": generated_at,
        "source": DEXSCREENER_LATEST_PROFILES_URL,
        "total_returned": len(tokens),
        "chains_found": dict(chain_counter),
        "total_solana": chain_counter.get("solana", 0),
        "total_non_solana": len(non_solana_tokens),
        "non_solana_tokens": enriched_non_solana,
    }

    latest_file = DATA_DIR / "dexscreener_latest_non_solana_enriched.json"
    history_file = DATA_DIR / f"krptov_dexscreener_{date_stamp}.jsonl"

    with latest_file.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with history_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    print()
    print("=== RESUMO ===")
    print(f"Tokens retornados: {len(tokens)}")
    print(f"Solana: {chain_counter.get('solana', 0)}")
    print(f"Nao-Solana: {len(non_solana_tokens)}")

    print()
    print("Chains encontradas:")
    for chain, count in chain_counter.most_common():
        print(f"- {chain}: {count}")

    print()
    print("Tokens nao-Solana enriquecidos:")

    if not enriched_non_solana:
        print("Nenhum token nao-Solana encontrado neste ciclo.")
    else:
        for item in enriched_non_solana:
            token = item["token_profile"]
            best_pair = item.get("best_pair")

            print(
                f"- chain={token.get('chainId')} | "
                f"address={token.get('tokenAddress')} | "
                f"pairs={item.get('pairs_count')} | "
                f"url={token.get('url')}"
            )

            if best_pair:
                print(
                    f"  dex={best_pair.get('dex_id')} | "
                    f"liquidez=${best_pair.get('liquidity_usd'):.2f} | "
                    f"vol_h1=${best_pair.get('volume_h1'):.2f} | "
                    f"m5={best_pair.get('price_change_m5'):.2f}% | "
                    f"h1={best_pair.get('price_change_h1'):.2f}% | "
                    f"pair_url={best_pair.get('url')}"
                )

    print()
    print(f"Ultimo ciclo: {latest_file}")
    print(f"Historico do dia: {history_file}")


if __name__ == "__main__":
    main()
