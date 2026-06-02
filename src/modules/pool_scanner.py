import argparse
import asyncio
import json
import os
import signal
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

import websockets
import yaml
from dotenv import load_dotenv


POOL_SCANNER_VERSION = "krptov-pool-scanner-v1-2026-05-31"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "config" / "pool_sources.yaml"
DATA_DIR = PROJECT_ROOT / "data"
POOL_SCANNER_DATA_DIR = DATA_DIR / "pool_scanner"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
WATCHLIST_LOCK_FILE = DATA_DIR / "watchlist.lock"

RECONNECT_DELAY_SECONDS = 5

PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"

SOURCE_TYPES = {
    "uniswap_v2_factory": {
        "event": "PairCreated",
        "topic": PAIR_CREATED_TOPIC,
        "decoder": "decode_uniswap_v2_pair_created",
        "address_field": "factory_address",
    },
    "uniswap_v3_factory": {
        "event": "PoolCreated",
        "topic": POOL_CREATED_TOPIC,
        "decoder": "decode_uniswap_v3_pool_created",
        "address_field": "factory_address",
    },
    "uniswap_v4_pool_manager": {
        "event": "Initialize",
        "topic": INITIALIZE_TOPIC,
        "decoder": "decode_uniswap_v4_initialize",
        "address_field": "pool_manager_address",
    },
}

PRESERVED_EXISTING_FIELDS = {
    "status",
    "social_status",
    "monitor_status",
    "telegram_alert_sent",
    "discarded_reason",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Escuta criacao de pools em DEXes e alimenta a watchlist padronizada.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_FILE,
        help="Arquivo YAML com chains, RPCs e fontes de pools.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Escuta e grava eventos, sem alterar data/watchlist.json.",
    )
    return parser.parse_args()


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_evm_address(address):
    if not isinstance(address, str):
        return None

    address = address.strip()
    if len(address) != 42 or not address.startswith("0x"):
        return None
    if not all(character in "0123456789abcdefABCDEF" for character in address[2:]):
        return None

    return address.lower()


def decode_topic_address(topic):
    if not isinstance(topic, str) or not topic.startswith("0x") or len(topic) != 66:
        raise ValueError(f"Topic de endereco invalido: {topic}")
    return normalize_evm_address(f"0x{topic[-40:]}")


def decode_data_address(data, word_index):
    word = decode_data_word(data, word_index)
    return normalize_evm_address(f"0x{word[-40:]}")


def decode_data_word(data, word_index):
    if not isinstance(data, str) or not data.startswith("0x"):
        raise ValueError("Campo data invalido no log.")

    start = 2 + (word_index * 64)
    word = data[start:start + 64]
    if len(word) != 64:
        raise ValueError("Campo data incompleto no log.")

    return word


def decode_uint(value):
    return int(value, 16)


def decode_data_uint(data, word_index):
    return decode_uint(decode_data_word(data, word_index))


def decode_data_int(data, word_index):
    value = decode_data_uint(data, word_index)
    if value >= 1 << 255:
        value -= 1 << 256
    return value


def decode_uniswap_v2_pair_created(log):
    topics = log["topics"]
    return {
        "token0": decode_topic_address(topics[1]),
        "token1": decode_topic_address(topics[2]),
        "pool_address": decode_data_address(log["data"], 0),
        "fee": None,
    }


def decode_uniswap_v3_pool_created(log):
    topics = log["topics"]
    return {
        "token0": decode_topic_address(topics[1]),
        "token1": decode_topic_address(topics[2]),
        "pool_address": decode_data_address(log["data"], 1),
        "fee": decode_uint(topics[3]),
    }


def decode_uniswap_v4_initialize(log):
    topics = log["topics"]
    return {
        "pool_id": topics[1].lower(),
        "currency0": decode_topic_address(topics[2]),
        "currency1": decode_topic_address(topics[3]),
        "pool_address": None,
        "fee": decode_data_uint(log["data"], 0),
        "tick_spacing": decode_data_int(log["data"], 1),
        "hooks": decode_data_address(log["data"], 2),
        "sqrt_price_x96": decode_data_uint(log["data"], 3),
        "tick": decode_data_int(log["data"], 4),
    }


DECODERS = {
    "decode_uniswap_v2_pair_created": decode_uniswap_v2_pair_created,
    "decode_uniswap_v3_pool_created": decode_uniswap_v3_pool_created,
    "decode_uniswap_v4_initialize": decode_uniswap_v4_initialize,
}


def load_config(config_file):
    with Path(config_file).open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}

    chains = config.get("chains")
    if not isinstance(chains, dict) or not chains:
        raise ValueError("config/pool_sources.yaml precisa declarar chains.")

    return config


def build_enabled_chains(config):
    chains = []

    for chain_name, chain_config in config["chains"].items():
        if not chain_config.get("enabled", False):
            continue

        rpc_env = chain_config.get("rpc_env")
        rpc_url = os.getenv(rpc_env) if rpc_env else None
        if not rpc_url:
            raise RuntimeError(f"Defina {rpc_env} antes de iniciar a chain {chain_name}.")

        quote_tokens = {}
        for symbol, address in (chain_config.get("quote_tokens") or {}).items():
            normalized = normalize_evm_address(address)
            if not normalized:
                raise ValueError(f"Quote token invalido em {chain_name}: {symbol}={address}")
            quote_tokens[normalized] = symbol

        sources = []
        for source in chain_config.get("sources") or []:
            if not source.get("enabled", False):
                continue

            source_type = source.get("type")
            source_definition = SOURCE_TYPES.get(source_type)
            if not source_definition:
                raise ValueError(f"Tipo de source nao suportado: {source_type}")

            address_field = source_definition["address_field"]
            subscription_address = normalize_evm_address(source.get(address_field))
            if not subscription_address:
                raise ValueError(
                    f"{address_field} invalido em {chain_name}/{source.get('name')}."
                )

            configured_event = source.get("event")
            if configured_event != source_definition["event"]:
                raise ValueError(
                    f"Evento invalido em {chain_name}/{source.get('name')}: {configured_event}"
                )

            sources.append(
                {
                    "chain": chain_name,
                    "name": source["name"],
                    "type": source_type,
                    "factory_address": (
                        subscription_address if address_field == "factory_address" else None
                    ),
                    "pool_manager_address": (
                        subscription_address if address_field == "pool_manager_address" else None
                    ),
                    "subscription_address": subscription_address,
                    "topic": source_definition["topic"],
                    "decoder": DECODERS[source_definition["decoder"]],
                }
            )

        if sources:
            chains.append(
                {
                    "name": chain_name,
                    "rpc_url": rpc_url,
                    "quote_tokens": quote_tokens,
                    "sources": sources,
                }
            )

    if not chains:
        raise RuntimeError("Nenhuma chain habilitada com sources ativas.")

    return chains


def event_file_path(received_at_utc=None):
    if received_at_utc:
        date_stamp = received_at_utc[:10]
    else:
        date_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return POOL_SCANNER_DATA_DIR / f"events_{date_stamp}.jsonl"


def append_jsonl(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded_line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

    with path.open("ab+") as file:
        file.seek(0, os.SEEK_END)
        end_position = file.tell()

        if end_position:
            file.seek(end_position - 1)
            if file.read(1) != b"\n":
                file.seek(0)
                existing = file.read()
                last_complete_line = existing.rfind(b"\n")
                file.seek(last_complete_line + 1 if last_complete_line >= 0 else 0)
                file.truncate()

        file.seek(0, os.SEEK_END)
        file.write(encoded_line)
        file.flush()
        os.fsync(file.fileno())


def load_watchlist(path=WATCHLIST_FILE):
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as file:
        watchlist = json.load(file)

    if not isinstance(watchlist, dict):
        raise ValueError("data/watchlist.json precisa ser um dict indexado por token.")

    return watchlist


def atomic_save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")

    try:
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())

        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


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


def identify_new_token(decoded_event, quote_tokens, no_quote_reason="pool_without_known_quote_token"):
    token0 = decoded_event.get("token0") or decoded_event.get("currency0")
    token1 = decoded_event.get("token1") or decoded_event.get("currency1")
    token0_quote = quote_tokens.get(token0)
    token1_quote = quote_tokens.get(token1)

    if token0_quote and token1_quote:
        return None, "both_tokens_are_known_quotes"
    if token0_quote:
        return {
            "token_address": token1,
            "quote_token": token0_quote,
            "quote_token_address": token0,
        }, None
    if token1_quote:
        return {
            "token_address": token0,
            "quote_token": token1_quote,
            "quote_token_address": token1,
        }, None

    return None, no_quote_reason


def source_type_for(source):
    if source["type"] == "uniswap_v4_pool_manager":
        return "pool_initialized"
    return "pool_created"


def build_watchlist_entry(chain, source, decoded_event, candidate, received_at_utc, raw_log):
    token_address = candidate["token_address"]
    entry = {
        "watchlist_key": f"{chain}:{token_address}",
        "chain": chain,
        "chain_id": chain,
        "token_address": token_address,
        "token_symbol": None,
        "token_name": None,
        "pool_address": decoded_event["pool_address"],
        "quote_token": candidate["quote_token"],
        "quote_token_address": candidate["quote_token_address"],
        "source": source["name"],
        "source_type": source_type_for(source),
        "discovered_at_utc": received_at_utc,
        "created_at_utc": received_at_utc,
        "created_block": decode_uint(raw_log["blockNumber"]),
        "created_tx": raw_log["transactionHash"],
        "last_seen_at_utc": received_at_utc,
        "times_seen": 1,
        "status": "novo",
        "social_status": "pendente",
        "monitor_status": "pendente",
        "status_reason": None,
        "discarded_reason": None,
        "telegram_alert_sent": False,
        "scanner_validation_status": "approved",
        "scanner_validation_reason": "pool_with_known_quote_token",
    }
    if source["type"] == "uniswap_v4_pool_manager":
        entry["pool_id"] = decoded_event["pool_id"]
        entry["pool_manager_address"] = source["pool_manager_address"]
    return entry


def upsert_watchlist_entry(entry):
    watchlist_key = entry["watchlist_key"]

    with watchlist_lock():
        watchlist = load_watchlist()
        existing = watchlist.get(watchlist_key)

        if isinstance(existing, dict):
            for field in PRESERVED_EXISTING_FIELDS:
                if field in existing:
                    entry[field] = existing[field]
            existing["last_seen_at_utc"] = entry["last_seen_at_utc"]
            existing["times_seen"] = int(existing.get("times_seen", 0)) + 1
            watchlist[watchlist_key] = existing
            action = "updated"
        else:
            watchlist[watchlist_key] = entry
            action = "created"

        atomic_save_json(WATCHLIST_FILE, watchlist)

    return action


def build_raw_event_record(chain, source, raw_log, decoded_event, candidate, ignored_reason, received_at_utc):
    record = {
        "scanner_version": POOL_SCANNER_VERSION,
        "received_at_utc": received_at_utc,
        "chain": chain,
        "source": source["name"],
        "source_type": source_type_for(source),
        "factory_address": source["factory_address"],
        "decoded_event": decoded_event,
        "candidate": candidate,
        "ignored_reason": ignored_reason,
        "raw_log": raw_log,
    }
    if source["type"] == "uniswap_v4_pool_manager":
        record["pool_manager_address"] = source["pool_manager_address"]
    if source["type"] == "uniswap_v4_pool_manager" and candidate:
        record["normalized_event"] = {
            "chain": chain,
            "source": source["name"],
            "source_type": "pool_initialized",
            "token_address": candidate["token_address"],
            "quote_token": candidate["quote_token"],
            "quote_token_address": candidate["quote_token_address"],
            "pool_id": decoded_event["pool_id"],
            "pool_manager_address": source["pool_manager_address"],
            "pool_address": None,
            "fee": decoded_event["fee"],
            "tick_spacing": decoded_event["tick_spacing"],
            "hooks": decoded_event["hooks"],
            "sqrt_price_x96": decoded_event["sqrt_price_x96"],
            "created_block": decode_uint(raw_log["blockNumber"]),
            "created_tx": raw_log["transactionHash"],
            "discovered_at_utc": received_at_utc,
        }
    return record


def print_pool_event(chain, source, decoded_event, candidate, ignored_reason, dry_run):
    print()
    print(f"=== Pool detectado | {chain}/{source['name']} ===")
    if source["type"] == "uniswap_v4_pool_manager":
        print(f"Pool ID:      {decoded_event['pool_id']}")
        print(f"Currency 0:   {decoded_event['currency0']}")
        print(f"Currency 1:   {decoded_event['currency1']}")
        print(f"Tick spacing: {decoded_event['tick_spacing']}")
        print(f"Hooks:        {decoded_event['hooks']}")
    else:
        print(f"Token 0: {decoded_event['token0']}")
        print(f"Token 1: {decoded_event['token1']}")
        print(f"Pool:    {decoded_event['pool_address']}")

    if decoded_event.get("fee") is not None:
        print(f"Fee:     {decoded_event['fee']}")

    if ignored_reason:
        print(f"Ignorado: {ignored_reason}")
        return

    print(f"Novo token: {candidate['token_address']}")
    print(f"Quote:      {candidate['quote_token']} ({candidate['quote_token_address']})")
    if dry_run:
        print("Dry-run: watchlist nao alterada.")


def process_pool_log(chain_config, source, raw_log, dry_run):
    received_at_utc = utc_now_iso()
    decoded_event = source["decoder"](raw_log)
    no_quote_reason = (
        "ignored_no_known_quote_token"
        if source["type"] == "uniswap_v4_pool_manager"
        else "pool_without_known_quote_token"
    )
    candidate, ignored_reason = identify_new_token(
        decoded_event,
        chain_config["quote_tokens"],
        no_quote_reason=no_quote_reason,
    )

    raw_record = build_raw_event_record(
        chain=chain_config["name"],
        source=source,
        raw_log=raw_log,
        decoded_event=decoded_event,
        candidate=candidate,
        ignored_reason=ignored_reason,
        received_at_utc=received_at_utc,
    )
    append_jsonl(event_file_path(received_at_utc), raw_record)
    print_pool_event(chain_config["name"], source, decoded_event, candidate, ignored_reason, dry_run)

    if ignored_reason or dry_run:
        return "ignored" if ignored_reason else "dry_run"

    entry = build_watchlist_entry(
        chain=chain_config["name"],
        source=source,
        decoded_event=decoded_event,
        candidate=candidate,
        received_at_utc=received_at_utc,
        raw_log=raw_log,
    )
    action = upsert_watchlist_entry(entry)
    print(f"Watchlist: {action} | {entry['watchlist_key']}")
    return action


async def subscribe_sources(ws, chain_config):
    request_sources = {}

    for request_id, source in enumerate(chain_config["sources"], start=1):
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "eth_subscribe",
            "params": [
                "logs",
                {
                    "address": source["subscription_address"],
                    "topics": [source["topic"]],
                },
            ],
        }
        await ws.send(json.dumps(payload))
        request_sources[request_id] = source

    return request_sources


async def receive_until_stopped(ws, stop_event):
    receive_task = asyncio.create_task(ws.recv())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            (receive_task, stop_task),
            return_when=asyncio.FIRST_COMPLETED,
        )

        if stop_task in done:
            return None

        return receive_task.result()
    finally:
        for task in (receive_task, stop_task):
            if not task.done():
                task.cancel()
        with suppress(asyncio.CancelledError):
            await receive_task
        with suppress(asyncio.CancelledError):
            await stop_task


async def listen_chain(chain_config, dry_run, stop_event):
    async with websockets.connect(chain_config["rpc_url"]) as ws:
        request_sources = await subscribe_sources(ws, chain_config)
        subscriptions = {}

        print(f"Conectado: {chain_config['name']}. Aguardando pools...")

        while not stop_event.is_set():
            raw_message = await receive_until_stopped(ws, stop_event)
            if raw_message is None:
                return

            message = json.loads(raw_message)

            if "error" in message:
                raise RuntimeError(f"Erro retornado pelo RPC: {message['error']}")

            request_id = message.get("id")
            if request_id in request_sources:
                source = request_sources[request_id]
                subscriptions[message["result"]] = source
                print(f"Assinatura ativa: {chain_config['name']}/{source['name']}")
                continue

            if message.get("method") != "eth_subscription":
                continue

            params = message.get("params") or {}
            source = subscriptions.get(params.get("subscription"))
            if not source:
                continue

            try:
                process_pool_log(
                    chain_config=chain_config,
                    source=source,
                    raw_log=params["result"],
                    dry_run=dry_run,
                )
            except Exception as error:
                print(f"Falha ao processar evento {chain_config['name']}/{source['name']}: {error}")


async def run_chain_with_reconnect(chain_config, dry_run, stop_event):
    while not stop_event.is_set():
        try:
            await listen_chain(chain_config, dry_run, stop_event)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"WebSocket desconectado em {chain_config['name']}: {error}")
            if stop_event.is_set():
                return
            print(f"Reconectando em {RECONNECT_DELAY_SECONDS} segundos...")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=RECONNECT_DELAY_SECONDS)
            except asyncio.TimeoutError:
                pass


async def run_pool_scanner(config_file=CONFIG_FILE, dry_run=False, stop_event=None):
    load_dotenv(PROJECT_ROOT / ".env")
    config = load_config(config_file)
    enabled_chains = build_enabled_chains(config)
    stop_event = stop_event or asyncio.Event()

    mode = "dry-run" if dry_run else "normal"
    print(f"=== KRPTO-V | Pool Scanner | modo={mode} ===")
    print(f"Versao: {POOL_SCANNER_VERSION}")

    await asyncio.gather(
        *(
            run_chain_with_reconnect(chain_config, dry_run, stop_event)
            for chain_config in enabled_chains
        )
    )


async def run_until_stopped(args):
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_handlers = {}

    def request_stop(signal_number, _frame):
        print("\nEncerramento solicitado. Fechando conexoes com seguranca...")
        loop.call_soon_threadsafe(stop_event.set)

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signal_number] = signal.getsignal(signal_number)
        signal.signal(signal_number, request_stop)

    try:
        await run_pool_scanner(
            config_file=args.config,
            dry_run=args.dry_run,
            stop_event=stop_event,
        )
    finally:
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)


def main():
    args = parse_args()
    asyncio.run(run_until_stopped(args))
    print("Pool scanner encerrado.")


if __name__ == "__main__":
    main()
