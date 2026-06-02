import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / f"pool_backfill_{datetime.now():%Y-%m-%d}.jsonl"

UNISWAP_V2_FACTORY = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
UNISWAP_V3_FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"

PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"

BLOCK_CHUNK_SIZE = 10
REQUEST_TIMEOUT_SECONDS = 30


def parse_args():
    parser = argparse.ArgumentParser(
        description="Consulta eventos historicos de pools Uniswap V2 e V3 na Ethereum Mainnet.",
    )
    parser.add_argument(
        "--blocks",
        type=int,
        default=10_000,
        help="Quantidade de blocos recentes para analisar. Padrao: 10000.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Arquivo JSONL de saida. Padrao: data/pool_backfill_YYYY-MM-DD.jsonl.",
    )
    return parser.parse_args()


def decode_address(value):
    return f"0x{value[-40:]}".lower()


def decode_uint(value):
    return int(value, 16)


def load_https_url():
    https_url = os.getenv("ALCHEMY_ETH_HTTPS_URL")
    if https_url:
        return https_url

    wss_url = os.getenv("ALCHEMY_ETH_WSS_URL")
    if wss_url and wss_url.startswith("wss://"):
        return f"https://{wss_url[6:]}"

    raise RuntimeError(
        "Defina ALCHEMY_ETH_HTTPS_URL ou ALCHEMY_ETH_WSS_URL antes de executar a ferramenta."
    )


def decode_event(log, version):
    topics = log["topics"]
    data = log["data"][2:]

    event = {
        "version": version,
        "token0": decode_address(topics[1]),
        "token1": decode_address(topics[2]),
        "txHash": log["transactionHash"],
        "blockNumber": decode_uint(log["blockNumber"]),
        "logIndex": decode_uint(log["logIndex"]),
    }

    if version == "V2":
        event["pair"] = decode_address(data[:64])
    else:
        event["fee"] = decode_uint(topics[3])
        event["pool"] = decode_address(data[64:128])

    return event


def rpc_request(https_url, method, params):
    try:
        response = requests.post(
            https_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        details = str(error).replace(https_url, "<Alchemy HTTPS URL>")
        raise RuntimeError(f"Falha de rede ao consultar a Alchemy: {details}") from None

    try:
        payload = response.json()
    except requests.JSONDecodeError:
        payload = None

    if not response.ok:
        details = payload if payload is not None else response.text[:500]
        raise RuntimeError(f"Erro HTTP {response.status_code} retornado pela Alchemy: {details}")

    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(f"Erro retornado pela Alchemy: {payload['error']}")

    return payload["result"]


def fetch_logs(https_url, from_block, to_block, factory_address, event_topic):
    return rpc_request(
        https_url,
        "eth_getLogs",
        [
            {
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": factory_address,
                "topics": [event_topic],
            },
        ],
    )


def append_events(output_file, events):
    with output_file.open("a", encoding="utf-8") as file:
        for event in events:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")


def fetch_events(https_url, from_block, to_block, output_file):
    events = []

    for chunk_start in range(from_block, to_block + 1, BLOCK_CHUNK_SIZE):
        chunk_end = min(chunk_start + BLOCK_CHUNK_SIZE - 1, to_block)
        print(f"Consultando blocos {chunk_start} a {chunk_end}...")

        v2_logs = fetch_logs(
            https_url,
            chunk_start,
            chunk_end,
            UNISWAP_V2_FACTORY,
            PAIR_CREATED_TOPIC,
        )
        v3_logs = fetch_logs(
            https_url,
            chunk_start,
            chunk_end,
            UNISWAP_V3_FACTORY,
            POOL_CREATED_TOPIC,
        )

        chunk_events = [decode_event(log, "V2") for log in v2_logs]
        chunk_events.extend(decode_event(log, "V3") for log in v3_logs)
        chunk_events.sort(key=lambda event: (event["blockNumber"], event["logIndex"]))

        append_events(output_file, chunk_events)
        events.extend(chunk_events)

    return events


def contains_token(event, token_address):
    return token_address in (event["token0"], event["token1"])


def initialize_output(output_file):
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text("", encoding="utf-8")


def print_recent_events(events):
    print()
    print("=== ULTIMOS 20 EVENTOS ===")

    if not events:
        print("Nenhum evento encontrado.")
        return

    for event in events[-20:]:
        pool_or_pair = event.get("pool") or event.get("pair")
        print(
            f"- {event['version']} | bloco={event['blockNumber']} | "
            f"token0={event['token0']} | token1={event['token1']} | "
            f"pool/pair={pool_or_pair} | txHash={event['txHash']}"
        )


def main():
    args = parse_args()

    if args.blocks <= 0:
        raise ValueError("--blocks deve ser maior que zero.")

    load_dotenv()
    https_url = load_https_url()

    latest_block = decode_uint(rpc_request(https_url, "eth_blockNumber", []))
    first_block = max(0, latest_block - args.blocks + 1)
    initialize_output(args.output)
    print(f"Salvando eventos em: {args.output}")
    events = fetch_events(https_url, first_block, latest_block, args.output)

    v2_count = sum(event["version"] == "V2" for event in events)
    v3_count = sum(event["version"] == "V3" for event in events)

    print()
    print("=== RESUMO ===")
    print(f"Blocos analisados: {latest_block - first_block + 1} ({first_block} a {latest_block})")
    print(f"Eventos V2 encontrados: {v2_count}")
    print(f"Eventos V3 encontrados: {v3_count}")
    print(f"Eventos contendo WETH: {sum(contains_token(event, WETH) for event in events)}")
    print(f"Eventos contendo USDC: {sum(contains_token(event, USDC) for event in events)}")
    print(f"Eventos contendo USDT: {sum(contains_token(event, USDT) for event in events)}")
    print(f"Arquivo gerado: {args.output}")

    print_recent_events(events)


if __name__ == "__main__":
    main()
