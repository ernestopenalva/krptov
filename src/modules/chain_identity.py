"""Canonical token identity helpers shared by EVM and Solana pipelines."""

from __future__ import annotations

from typing import Optional, Tuple


EVM_CHAINS = {"ethereum", "base", "bsc", "robinhood"}
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_VALUES = {character: index for index, character in enumerate(BASE58_ALPHABET)}


def normalize_evm_address(address: object) -> Optional[str]:
    if not isinstance(address, str):
        return None
    text = address.strip()
    if len(text) != 42 or not text.startswith("0x"):
        return None
    if not all(character in "0123456789abcdefABCDEF" for character in text[2:]):
        return None
    return text.lower()


def _base58_decoded_length(text: str) -> Optional[int]:
    value = 0
    for character in text:
        digit = BASE58_VALUES.get(character)
        if digit is None:
            return None
        value = value * 58 + digit
    payload_length = (value.bit_length() + 7) // 8 if value else 0
    return len(text) - len(text.lstrip("1")) + payload_length


def normalize_solana_address(address: object) -> Optional[str]:
    if not isinstance(address, str):
        return None
    text = address.strip()
    if not 32 <= len(text) <= 44 or _base58_decoded_length(text) != 32:
        return None
    return text


def normalize_token_address(chain: object, address: object) -> Optional[str]:
    chain_id = str(chain or "").strip().lower()
    if chain_id == "solana":
        return normalize_solana_address(address)
    return normalize_evm_address(address)


def make_watchlist_key(chain: object, address: object) -> Optional[str]:
    chain_id = str(chain or "").strip().lower()
    normalized = normalize_token_address(chain_id, address)
    if not chain_id or not normalized:
        return None
    return f"{chain_id}:{normalized}"


def split_watchlist_key(key: object) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(key, str) or ":" not in key:
        return None, None
    chain, address = key.split(":", 1)
    chain = chain.strip().lower()
    return chain or None, normalize_token_address(chain, address)


def same_token_address(chain: object, left: object, right: object) -> bool:
    normalized_left = normalize_token_address(chain, left)
    return bool(normalized_left and normalized_left == normalize_token_address(chain, right))
