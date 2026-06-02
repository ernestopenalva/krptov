import asyncio
import json
import os

import websockets
from dotenv import load_dotenv


UNISWAP_V2_FACTORY = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
UNISWAP_V3_FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"

PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

RECONNECT_DELAY_SECONDS = 5


def decode_address(value):
    return f"0x{value[-40:]}"


def decode_uint(value):
    return int(value, 16)


def decode_event(log, version):
    topics = log["topics"]
    data = log["data"][2:]

    event = {
        "version": version,
        "token0": decode_address(topics[1]),
        "token1": decode_address(topics[2]),
        "tx_hash": log["transactionHash"],
        "block_number": decode_uint(log["blockNumber"]),
    }

    if version == "V2":
        event["pair"] = decode_address(data[:64])
    else:
        event["fee"] = decode_uint(topics[3])
        event["pool"] = decode_address(data[64:128])

    return event


def print_event(event):
    print()
    print(f"=== Uniswap {event['version']} pool criado ===")
    print(f"Token 0:       {event['token0']}")
    print(f"Token 1:       {event['token1']}")

    if event["version"] == "V2":
        print(f"Pair:          {event['pair']}")
    else:
        print(f"Pool:          {event['pool']}")
        print(f"Fee:           {event['fee']}")

    print(f"Bloco:         {event['block_number']}")
    print(f"Transaction:   {event['tx_hash']}")


async def subscribe(ws, request_id, factory_address, event_topic):
    if len(event_topic) != 66:
        raise ValueError(f"Topico de evento invalido: {event_topic}")

    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "eth_subscribe",
        "params": [
            "logs",
            {
                "address": factory_address,
                "topics": [event_topic],
            },
        ],
    }
    await ws.send(json.dumps(payload))


async def listen_for_pool_events(wss_url):
    async with websockets.connect(wss_url) as ws:
        await subscribe(ws, 1, UNISWAP_V2_FACTORY, PAIR_CREATED_TOPIC)
        await subscribe(ws, 2, UNISWAP_V3_FACTORY, POOL_CREATED_TOPIC)

        subscriptions = {}
        print("Conectado à Ethereum Mainnet. Aguardando novos pools da Uniswap V2 e V3...")

        while True:
            message = json.loads(await ws.recv())

            if "error" in message:
                raise RuntimeError(f"Erro retornado pela Alchemy: {message['error']}")

            if message.get("id") in (1, 2):
                version = "V2" if message["id"] == 1 else "V3"
                subscriptions[message["result"]] = version
                print(f"Assinatura Uniswap {version} ativa.")
                continue

            if message.get("method") != "eth_subscription":
                continue

            params = message.get("params", {})
            version = subscriptions.get(params.get("subscription"))
            if version:
                print_event(decode_event(params["result"], version))


async def main():
    load_dotenv()
    wss_url = os.getenv("ALCHEMY_ETH_WSS_URL")
    if not wss_url:
        raise RuntimeError("Defina ALCHEMY_ETH_WSS_URL antes de executar o script.")

    while True:
        try:
            await listen_for_pool_events(wss_url)
        except asyncio.CancelledError:
            raise
        except KeyboardInterrupt:
            raise
        except Exception as error:
            print(f"WebSocket desconectado: {error}")
            print(f"Reconectando em {RECONNECT_DELAY_SECONDS} segundos...")
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nListener encerrado.")
