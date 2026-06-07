import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
TOKEN_AGE_DATA_DIR = DATA_DIR / "token_age"

ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
REQUEST_TIMEOUT_SECONDS = 20
ETHERSCAN_CONTRACT_CREATION_BATCH_SIZE = 5

CHAIN_IDS = {
    "ethereum": "1",
    "base": "8453",
}

TOKEN_AGE_STATUS_RESOLVED = "resolved"
TOKEN_AGE_STATUS_PENDING_INDEXER = "pending_indexer"
TOKEN_AGE_STATUS_NOT_FOUND = "not_found"
TOKEN_AGE_STATUS_ERROR = "error"

TOKEN_AGE_SOURCE_ETHERSCAN = "etherscan_contract_creation"


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_evm_address(address):
    if not isinstance(address, str):
        return None

    address = address.strip()
    if len(address) != 42 or not address.startswith("0x"):
        return None
    if not all(character in "0123456789abcdefABCDEF" for character in address[2:]):
        return None
    return address.lower()


def append_jsonl(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded_line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

    with path.open("ab+") as file:
        file.seek(0, os.SEEK_END)
        file.write(encoded_line)
        file.flush()
        os.fsync(file.fileno())


def resolutions_file_path(current_time):
    return TOKEN_AGE_DATA_DIR / f"resolutions_{current_time.strftime('%Y-%m-%d')}.jsonl"


def batched(items, batch_size):
    for index in range(0, len(items), batch_size):
        yield items[index:index + batch_size]


def token_age_source_for_chain(chain):
    if chain in CHAIN_IDS:
        return TOKEN_AGE_SOURCE_ETHERSCAN
    return None


def chain_id_for_chain(chain):
    return CHAIN_IDS.get(chain)


def load_api_key(config):
    load_dotenv(PROJECT_ROOT / ".env")
    env_name = config.get("api_key_env") or "ETHERSCAN_API_KEY"
    return os.getenv(env_name)


def parse_creation_timestamp(value):
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def request_contract_creation(chain, token_addresses, api_key, session=requests):
    chain_id = chain_id_for_chain(chain)
    if not chain_id:
        raise ValueError(f"Chain nao suportada para token_age: {chain}")

    params = {
        "chainid": chain_id,
        "module": "contract",
        "action": "getcontractcreation",
        "contractaddresses": ",".join(token_addresses),
        "apikey": api_key,
    }
    response = session.get(ETHERSCAN_V2_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()

    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, list):
        return payload, result

    message = payload.get("message") if isinstance(payload, dict) else "resposta invalida"
    raise RuntimeError(f"Etherscan getcontractcreation falhou: {message} | {result}")


def resolution_from_item(chain, token_address, item, current_time):
    normalized = normalize_evm_address(token_address)
    created_at = parse_creation_timestamp(item.get("timestamp"))
    if not created_at:
        return {
            "watchlist_update": {
                "token_age_status": TOKEN_AGE_STATUS_PENDING_INDEXER,
                "token_age_source": token_age_source_for_chain(chain),
                "token_age_updated_at": to_iso(current_time),
            },
            "diagnostic": {
                "status": TOKEN_AGE_STATUS_PENDING_INDEXER,
                "chain": chain,
                "token_address": normalized,
                "raw_result": item,
            },
        }

    age_minutes = max(0, (current_time - created_at).total_seconds() / 60)
    creator = normalize_evm_address(item.get("contractCreator"))
    update = {
        "token_created_at_utc": to_iso(created_at),
        "token_age_minutes": round(age_minutes, 2),
        "token_age_status": TOKEN_AGE_STATUS_RESOLVED,
        "token_age_source": token_age_source_for_chain(chain),
        "token_age_updated_at": to_iso(current_time),
    }

    return {
        "watchlist_update": update,
        "diagnostic": {
            "status": TOKEN_AGE_STATUS_RESOLVED,
            "chain": chain,
            "token_address": normalized,
            "token_created_at_utc": update["token_created_at_utc"],
            "token_age_minutes": update["token_age_minutes"],
            "token_age_source": update["token_age_source"],
            "token_creation_tx_hash": item.get("txHash"),
            "token_creator_address": creator,
            "block_number": item.get("blockNumber"),
        },
    }


def unresolved_resolution(chain, token_address, status, current_time, error=None):
    update = {
        "token_age_status": status,
        "token_age_source": token_age_source_for_chain(chain),
        "token_age_updated_at": to_iso(current_time),
    }
    diagnostic = {
        "status": status,
        "chain": chain,
        "token_address": normalize_evm_address(token_address),
    }
    if error:
        diagnostic["error"] = str(error)
    return {"watchlist_update": update, "diagnostic": diagnostic}


def should_resolve_token_age(token, current_time, refresh_hours):
    entry = token["entry"]
    if entry.get("token_age_status") == TOKEN_AGE_STATUS_RESOLVED:
        return False

    updated_at = entry.get("token_age_updated_at")
    if not updated_at:
        return True

    try:
        parsed = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
    except ValueError:
        return True

    return (current_time - parsed.astimezone(timezone.utc)).total_seconds() >= refresh_hours * 3600


def resolve_token_ages(tokens, config=None, current_time=None, session=requests):
    config = config or {}
    current_time = current_time or utc_now()
    if not config.get("enabled", True):
        return {}, {"enabled": False, "checked": 0, "resolved": 0, "unresolved": 0, "errors": 0}

    api_key = load_api_key(config)
    refresh_hours = int(config.get("refresh_hours") or 6)
    updates = {}
    summary = {"enabled": True, "checked": 0, "resolved": 0, "unresolved": 0, "errors": 0}

    candidates = [
        token
        for token in tokens
        if chain_id_for_chain(token["chain"])
        and should_resolve_token_age(token, current_time, refresh_hours)
    ]

    if not api_key:
        for token in candidates:
            result = unresolved_resolution(
                token["chain"],
                token["token_address"],
                TOKEN_AGE_STATUS_ERROR,
                current_time,
                error="ETHERSCAN_API_KEY ausente",
            )
            updates[token["watchlist_key"]] = result["watchlist_update"]
            append_jsonl(resolutions_file_path(current_time), {"checked_at_utc": to_iso(current_time), **result["diagnostic"]})
            summary["checked"] += 1
            summary["unresolved"] += 1
            summary["errors"] += 1
        return updates, summary

    tokens_by_chain = {}
    for token in candidates:
        tokens_by_chain.setdefault(token["chain"], []).append(token)

    for chain, chain_tokens in tokens_by_chain.items():
        for batch in batched(chain_tokens, ETHERSCAN_CONTRACT_CREATION_BATCH_SIZE):
            addresses = [token["token_address"] for token in batch]
            try:
                _payload, results = request_contract_creation(chain, addresses, api_key, session=session)
                result_by_address = {
                    normalize_evm_address(item.get("contractAddress")): item
                    for item in results
                    if isinstance(item, dict)
                }
                for token in batch:
                    item = result_by_address.get(token["token_address"])
                    if item:
                        resolution = resolution_from_item(chain, token["token_address"], item, current_time)
                    else:
                        resolution = unresolved_resolution(
                            chain,
                            token["token_address"],
                            TOKEN_AGE_STATUS_NOT_FOUND,
                            current_time,
                        )
                    updates[token["watchlist_key"]] = resolution["watchlist_update"]
                    append_jsonl(
                        resolutions_file_path(current_time),
                        {"checked_at_utc": to_iso(current_time), **resolution["diagnostic"]},
                    )
                    summary["checked"] += 1
                    if resolution["watchlist_update"].get("token_age_status") == TOKEN_AGE_STATUS_RESOLVED:
                        summary["resolved"] += 1
                    else:
                        summary["unresolved"] += 1
            except Exception as error:
                summary["errors"] += len(batch)
                for token in batch:
                    resolution = unresolved_resolution(
                        chain,
                        token["token_address"],
                        TOKEN_AGE_STATUS_ERROR,
                        current_time,
                        error=error,
                    )
                    updates[token["watchlist_key"]] = resolution["watchlist_update"]
                    append_jsonl(
                        resolutions_file_path(current_time),
                        {"checked_at_utc": to_iso(current_time), **resolution["diagnostic"]},
                    )
                    summary["checked"] += 1
                    summary["unresolved"] += 1
            time.sleep(float(config.get("sleep_seconds") or 0))

    return updates, summary
