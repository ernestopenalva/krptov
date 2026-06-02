import argparse
import json
import os
import signal
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POOL_SCANNER_DATA_DIR = PROJECT_ROOT / "data" / "pool_scanner"
DIAGNOSTICS_DATA_DIR = PROJECT_ROOT / "data" / "pool_diagnostics"
STATE_FILE = DIAGNOSTICS_DATA_DIR / "state.json"

DEXSCREENER_PAIR_URL = "https://api.dexscreener.com/latest/dex/pairs/{chain}/{pool_address}"
DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chain}/{token_address}"

DEFAULT_POLL_SECONDS = 15
DEFAULT_SNAPSHOT_MINUTES = (5, 15, 30)
DEFAULT_EXPIRY_MINUTES = 45
DEFAULT_LOOKBACK_SECONDS = 120
REQUEST_TIMEOUT_SECONDS = 20

stop_requested = False


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Observa pools descobertos pelo pool_scanner e mede indexacao, liquidez, "
            "volume e transacoes na Dexscreener."
        ),
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=DEFAULT_POLL_SECONDS,
        help="Intervalo entre consultas de indexacao. Padrao: 15.",
    )
    parser.add_argument(
        "--snapshot-minutes",
        default=",".join(str(value) for value in DEFAULT_SNAPSHOT_MINUTES),
        help="Idades dos snapshots, separadas por virgula. Padrao: 5,15,30.",
    )
    parser.add_argument(
        "--expiry-minutes",
        type=int,
        default=DEFAULT_EXPIRY_MINUTES,
        help="Tempo maximo para acompanhar uma pool. Padrao: 45.",
    )
    parser.add_argument(
        "--lookback-seconds",
        type=int,
        default=DEFAULT_LOOKBACK_SECONDS,
        help="Ao iniciar uma sessao nova, inclui eventos recentes. Padrao: 120.",
    )
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="Inicia uma nova sessao diagnostica, sem reutilizar o estado anterior.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Exibe o relatorio do estado atual e encerra.",
    )
    return parser.parse_args()


def utc_now():
    return datetime.now(timezone.utc)


def utc_now_iso():
    return utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def timestamp_iso(timestamp):
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def parse_snapshot_minutes(value):
    try:
        minutes = sorted({int(item.strip()) for item in value.split(",") if item.strip()})
    except ValueError as error:
        raise ValueError("--snapshot-minutes deve conter apenas inteiros.") from error

    if not minutes or any(item <= 0 for item in minutes):
        raise ValueError("--snapshot-minutes deve conter valores maiores que zero.")

    return minutes


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


def observations_file_path(observed_at_utc):
    return DIAGNOSTICS_DATA_DIR / f"observations_{observed_at_utc[:10]}.jsonl"


def create_state(snapshot_minutes, expiry_minutes):
    now = utc_now_iso()
    return {
        "session_started_at_utc": now,
        "updated_at_utc": now,
        "snapshot_minutes": snapshot_minutes,
        "expiry_minutes": expiry_minutes,
        "stream_positions": {},
        "tasks": {},
    }


def load_state(snapshot_minutes, expiry_minutes, new_session=False):
    if not new_session and STATE_FILE.exists():
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)
        if not isinstance(state, dict) or not isinstance(state.get("tasks"), dict):
            raise ValueError(f"Estado invalido: {STATE_FILE}")
        return state, True

    return create_state(snapshot_minutes, expiry_minutes), False


def event_created_at(scanner_event):
    raw_log = scanner_event.get("raw_log") or {}
    block_timestamp = raw_log.get("blockTimestamp")
    if isinstance(block_timestamp, str):
        try:
            return timestamp_iso(int(block_timestamp, 16)), "alchemy_block_timestamp"
        except ValueError:
            pass
    return scanner_event["received_at_utc"], "scanner_received_at_fallback"


def task_id_for(scanner_event):
    decoded_event = scanner_event["decoded_event"]
    identity = decoded_event.get("pool_address") or decoded_event.get("pool_id")
    return f"{scanner_event['chain']}:{identity.lower()}"


def build_task(scanner_event):
    decoded_event = scanner_event["decoded_event"]
    candidate = scanner_event["candidate"]
    pool_created_at_utc, timestamp_source = event_created_at(scanner_event)
    pool_address = decoded_event.get("pool_address")
    pool_id = decoded_event.get("pool_id")

    return {
        "task_id": task_id_for(scanner_event),
        "chain": scanner_event["chain"],
        "source": scanner_event["source"],
        "source_type": scanner_event["source_type"],
        "token_address": candidate["token_address"],
        "quote_token": candidate["quote_token"],
        "quote_token_address": candidate["quote_token_address"],
        "pool_address": pool_address,
        "pool_id": pool_id,
        "pool_manager_address": scanner_event.get("pool_manager_address"),
        "pool_created_at_utc": pool_created_at_utc,
        "pool_created_at_source": timestamp_source,
        "scanner_received_at_utc": scanner_event["received_at_utc"],
        "lookup_mode": "exact_pool_address" if pool_address else "token_address_fallback",
        "association_precision": "exact_pool" if pool_address else "token_level",
        "first_seen_on_dexscreener_at_utc": None,
        "first_seen_delay_seconds": None,
        "last_polled_at_utc": None,
        "snapshots": {},
        "completed_at_utc": None,
        "completion_reason": None,
    }


def register_scanner_event(state, scanner_event):
    if not scanner_event.get("candidate"):
        return False

    task_id = task_id_for(scanner_event)
    if task_id in state["tasks"]:
        return False

    task = build_task(scanner_event)
    state["tasks"][task_id] = task
    print(
        f"Novo pool acompanhado: {task['source']} | token={task['token_address']} | "
        f"identidade={task['pool_address'] or task['pool_id']}"
    )
    return True


def stream_files():
    return sorted(POOL_SCANNER_DATA_DIR.glob("events_*.jsonl"))


def ingest_streams(state, fresh_state=False, lookback_seconds=DEFAULT_LOOKBACK_SECONDS):
    changed = False
    minimum_received_at = utc_now().timestamp() - lookback_seconds if fresh_state else None

    for path in stream_files():
        path_key = str(path.resolve())
        start_position = 0 if fresh_state else int(state["stream_positions"].get(path_key, 0))

        with path.open("r", encoding="utf-8") as file:
            file.seek(start_position)
            while True:
                line_start = file.tell()
                line = file.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    file.seek(line_start)
                    break

                try:
                    scanner_event = json.loads(line)
                except json.JSONDecodeError:
                    print(f"Linha JSONL invalida ignorada: {path.name}")
                    continue

                if minimum_received_at is not None:
                    received_at = parse_iso(scanner_event["received_at_utc"]).timestamp()
                    if received_at < minimum_received_at:
                        continue

                changed = register_scanner_event(state, scanner_event) or changed

            state["stream_positions"][path_key] = file.tell()

    return changed


def get_nested_number(data, path, default=0.0):
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
    txns = pair.get("txns") or {}
    return {
        "chain_id": pair.get("chainId"),
        "dex_id": pair.get("dexId"),
        "pair_address": (pair.get("pairAddress") or "").lower() or None,
        "url": pair.get("url"),
        "base_token": pair.get("baseToken"),
        "quote_token": pair.get("quoteToken"),
        "price_usd": pair.get("priceUsd"),
        "liquidity_usd": get_nested_number(pair, ["liquidity", "usd"]),
        "volume_m5_usd": get_nested_number(pair, ["volume", "m5"]),
        "volume_h1_usd": get_nested_number(pair, ["volume", "h1"]),
        "volume_h24_usd": get_nested_number(pair, ["volume", "h24"]),
        "buys_m5": int(get_nested_number(txns, ["m5", "buys"])),
        "sells_m5": int(get_nested_number(txns, ["m5", "sells"])),
        "buys_h1": int(get_nested_number(txns, ["h1", "buys"])),
        "sells_h1": int(get_nested_number(txns, ["h1", "sells"])),
        "buys_h24": int(get_nested_number(txns, ["h24", "buys"])),
        "sells_h24": int(get_nested_number(txns, ["h24", "sells"])),
        "pair_created_at_ms": pair.get("pairCreatedAt"),
    }


def token_pair_matches_task(pair, task):
    addresses = {
        ((pair.get("baseToken") or {}).get("address") or "").lower(),
        ((pair.get("quoteToken") or {}).get("address") or "").lower(),
    }
    return {
        task["token_address"].lower(),
        task["quote_token_address"].lower(),
    }.issubset(addresses)


def select_v4_token_pair(pairs, task):
    matching = [pair for pair in pairs if token_pair_matches_task(pair, task)]
    if not matching:
        return None

    created_at_ms = int(parse_iso(task["pool_created_at_utc"]).timestamp() * 1000)

    def score(pair):
        pair_created_at = pair.get("pairCreatedAt")
        distance = abs(pair_created_at - created_at_ms) if isinstance(pair_created_at, int) else 10**30
        return distance, -get_nested_number(pair, ["liquidity", "usd"])

    return min(matching, key=score)


def fetch_dexscreener_pair(task, session=requests):
    if task["pool_address"]:
        url = DEXSCREENER_PAIR_URL.format(
            chain=task["chain"],
            pool_address=task["pool_address"],
        )
        response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        pairs = response.json().get("pairs") or []
        exact_pool = task["pool_address"].lower()
        pair = next(
            (
                item for item in pairs
                if (item.get("pairAddress") or "").lower() == exact_pool
            ),
            pairs[0] if pairs else None,
        )
    else:
        url = DEXSCREENER_TOKEN_PAIRS_URL.format(
            chain=task["chain"],
            token_address=task["token_address"],
        )
        response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        pair = select_v4_token_pair(response.json() or [], task)

    return summarize_pair(pair) if pair else None


def seconds_since(then_iso, now):
    return max(0, int((now - parse_iso(then_iso)).total_seconds()))


def should_poll(task, now, poll_seconds, snapshot_minutes):
    if task["completed_at_utc"]:
        return False

    age_seconds = seconds_since(task["pool_created_at_utc"], now)
    missing_due_snapshot = any(
        str(minute) not in task["snapshots"] and age_seconds >= minute * 60
        for minute in snapshot_minutes
    )
    if missing_due_snapshot:
        return True

    if task["first_seen_on_dexscreener_at_utc"]:
        return False

    last_polled_at = task.get("last_polled_at_utc")
    if not last_polled_at:
        return True
    return seconds_since(last_polled_at, now) >= poll_seconds


def build_observation(task, pair, observed_at_utc, observation_type, target_minutes=None, error=None):
    observation = {
        "observed_at_utc": observed_at_utc,
        "observation_type": observation_type,
        "target_age_minutes": target_minutes,
        "task_id": task["task_id"],
        "chain": task["chain"],
        "source": task["source"],
        "token_address": task["token_address"],
        "quote_token": task["quote_token"],
        "pool_address": task["pool_address"],
        "pool_id": task["pool_id"],
        "lookup_mode": task["lookup_mode"],
        "association_precision": task["association_precision"],
        "pool_created_at_utc": task["pool_created_at_utc"],
        "age_seconds": seconds_since(task["pool_created_at_utc"], parse_iso(observed_at_utc)),
        "found_on_dexscreener": pair is not None,
        "error": error,
        "pair": pair,
    }
    return observation


def save_observation(observation):
    append_jsonl(observations_file_path(observation["observed_at_utc"]), observation)


def poll_task(task, snapshot_minutes, session=requests, now=None):
    now = now or utc_now()
    observed_at_utc = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    task["last_polled_at_utc"] = observed_at_utc

    try:
        pair = fetch_dexscreener_pair(task, session=session)
        error = None
    except requests.RequestException as request_error:
        pair = None
        error = str(request_error)
        print(f"Falha Dexscreener: {task['task_id']} | {error}")

    observations = []
    if error:
        observation = build_observation(task, None, observed_at_utc, "poll_error", error=error)
        save_observation(observation)
        return True

    if pair and not task["first_seen_on_dexscreener_at_utc"]:
        task["first_seen_on_dexscreener_at_utc"] = observed_at_utc
        task["first_seen_delay_seconds"] = seconds_since(task["pool_created_at_utc"], now)
        observations.append(build_observation(task, pair, observed_at_utc, "first_seen"))
        print(
            f"Dexscreener indexou: {task['source']} | token={task['token_address']} | "
            f"atraso={task['first_seen_delay_seconds']}s"
        )

    age_seconds = seconds_since(task["pool_created_at_utc"], now)
    for minute in snapshot_minutes:
        snapshot_key = str(minute)
        if snapshot_key in task["snapshots"] or age_seconds < minute * 60:
            continue
        observation = build_observation(
            task,
            pair,
            observed_at_utc,
            "snapshot",
            target_minutes=minute,
            error=error,
        )
        task["snapshots"][snapshot_key] = observation
        observations.append(observation)
        liquidity = (pair or {}).get("liquidity_usd", 0)
        print(
            f"Snapshot {minute}m: {task['source']} | token={task['token_address']} | "
            f"encontrado={'sim' if pair else 'nao'} | liquidez=${liquidity:.2f}"
        )

    for observation in observations:
        save_observation(observation)

    return bool(observations)


def complete_expired_tasks(state, now, expiry_minutes, snapshot_minutes):
    changed = False
    for task in state["tasks"].values():
        if task["completed_at_utc"]:
            continue
        age_seconds = seconds_since(task["pool_created_at_utc"], now)
        has_all_snapshots = all(str(minute) in task["snapshots"] for minute in snapshot_minutes)
        if has_all_snapshots:
            task["completed_at_utc"] = utc_now_iso()
            task["completion_reason"] = "snapshots_completed"
            changed = True
        elif age_seconds >= expiry_minutes * 60:
            task["completed_at_utc"] = utc_now_iso()
            task["completion_reason"] = "expired"
            changed = True
    return changed


def percentile_95(values):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * 0.95) + 0.999999) - 1))
    return ordered[index]


def format_seconds(value):
    return "n/a" if value is None else f"{value:.1f}s"


def format_money(value):
    return "n/a" if value is None else f"${value:.2f}"


def print_report(state):
    tasks = list(state["tasks"].values())
    found_tasks = [task for task in tasks if task["first_seen_delay_seconds"] is not None]
    delays = [task["first_seen_delay_seconds"] for task in found_tasks]

    print()
    print("=== KRPTO-V | Pool Diagnostics | Relatorio ===")
    print(f"Sessao iniciada: {state['session_started_at_utc']}")
    print(f"Pools acompanhados: {len(tasks)}")
    print(f"Encontrados na Dexscreener: {len(found_tasks)}")
    print(f"Ainda nao encontrados: {len(tasks) - len(found_tasks)}")
    print(f"Sources: {dict(Counter(task['source'] for task in tasks))}")
    print(
        "Precisao da associacao: "
        f"{dict(Counter(task['association_precision'] for task in tasks))}"
    )
    print()
    print("Atraso observado ate a primeira indexacao na Dexscreener:")
    print(f"- media: {format_seconds(statistics.mean(delays) if delays else None)}")
    print(f"- mediana: {format_seconds(statistics.median(delays) if delays else None)}")
    print(f"- p95: {format_seconds(percentile_95(delays))}")
    print(f"- maior atraso: {format_seconds(max(delays) if delays else None)}")

    print()
    print("Snapshots de sobrevivencia:")
    for minute in state["snapshot_minutes"]:
        snapshots = [
            task["snapshots"][str(minute)]
            for task in tasks
            if str(minute) in task["snapshots"]
        ]
        found = [item for item in snapshots if item["found_on_dexscreener"]]
        liquidities = [item["pair"]["liquidity_usd"] for item in found]
        volumes = [item["pair"]["volume_h24_usd"] for item in found]
        with_liquidity = sum(value > 0 for value in liquidities)
        with_txns = sum(
            item["pair"]["buys_h24"] + item["pair"]["sells_h24"] > 0
            for item in found
        )
        print(
            f"- {minute}m | snapshots={len(snapshots)} | encontrados={len(found)} | "
            f"liquidez>0={with_liquidity} | transacoes>0={with_txns} | "
            f"liq_mediana={format_money(statistics.median(liquidities) if liquidities else None)} | "
            f"volume_h24>100={sum(value > 100 for value in volumes)} | "
            f"volume_h24>1000={sum(value > 1000 for value in volumes)}"
        )


def save_state(state):
    state["updated_at_utc"] = utc_now_iso()
    atomic_save_json(STATE_FILE, state)


def request_stop(_signal_number, _frame):
    global stop_requested
    stop_requested = True
    print("\nEncerramento solicitado. Salvando diagnostico...")


def run(args):
    snapshot_minutes = parse_snapshot_minutes(args.snapshot_minutes)
    if args.poll_seconds <= 0:
        raise ValueError("--poll-seconds deve ser maior que zero.")
    if args.expiry_minutes <= max(snapshot_minutes):
        raise ValueError("--expiry-minutes deve ser maior que o ultimo snapshot.")
    if args.lookback_seconds < 0:
        raise ValueError("--lookback-seconds nao pode ser negativo.")

    state, resumed = load_state(
        snapshot_minutes=snapshot_minutes,
        expiry_minutes=args.expiry_minutes,
        new_session=args.new_session,
    )

    if args.report_only:
        print_report(state)
        return

    state["snapshot_minutes"] = snapshot_minutes
    state["expiry_minutes"] = args.expiry_minutes
    fresh_state = not resumed
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    print("=== KRPTO-V | Pool Diagnostics ===")
    print(f"Modo: {'retomando sessao' if resumed else 'nova sessao'}")
    print(f"Sessao: {state['session_started_at_utc']}")
    print(f"Snapshots: {', '.join(f'{minute}m' for minute in snapshot_minutes)}")
    print(f"Consulta de indexacao: a cada {args.poll_seconds}s")
    print("Acompanhando novos eventos do pool_scanner...")
    save_state(state)

    try:
        while not stop_requested:
            changed = ingest_streams(
                state,
                fresh_state=fresh_state,
                lookback_seconds=args.lookback_seconds,
            )
            fresh_state = False
            now = utc_now()

            for task in state["tasks"].values():
                if should_poll(task, now, args.poll_seconds, snapshot_minutes):
                    changed = poll_task(task, snapshot_minutes, now=now) or changed

            changed = complete_expired_tasks(
                state,
                now,
                args.expiry_minutes,
                snapshot_minutes,
            ) or changed

            if changed:
                save_state(state)

            time.sleep(1)
    finally:
        save_state(state)
        print_report(state)
        print(f"Estado salvo em: {STATE_FILE}")


def main():
    run(parse_args())


if __name__ == "__main__":
    main()
