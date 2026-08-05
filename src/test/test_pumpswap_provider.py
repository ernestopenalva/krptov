import base64
import unittest

from src.market_data.pumpswap_provider import (
    BASE_MINT_OFFSET,
    BASE_VAULT_OFFSET,
    PUMPSWAP_PROGRAM_ID,
    PUBKEY_LENGTH,
    QUOTE_MINT_OFFSET,
    QUOTE_VAULT_OFFSET,
    PumpSwapProvider,
    _b58,
)
from src.market_data.types import MarketContext


def parsed_vault(mint, amount, decimals):
    return {
        "data": {
            "parsed": {
                "info": {
                    "mint": mint,
                    "tokenAmount": {"amount": str(amount), "decimals": decimals},
                }
            }
        }
    }


class FakeRpc:
    def __init__(self, pool_info, vaults):
        self.pool_info = pool_info
        self.vaults = vaults
        self.account_calls = 0
        self.multiple_calls = 0

    def account(self, _address, _encoding="base64"):
        self.account_calls += 1
        return self.pool_info

    def multiple_accounts(self, addresses, _encoding="base64"):
        self.multiple_calls += 1
        return [self.vaults[address] for address in addresses], 12345


class PumpSwapProviderTests(unittest.TestCase):
    def test_cached_layout_reduces_steady_state_tick_to_one_rpc(self):
        base_mint_bytes = bytes(range(1, PUBKEY_LENGTH + 1))
        quote_mint_bytes = bytes(range(33, 33 + PUBKEY_LENGTH))
        base_vault_bytes = bytes(range(65, 65 + PUBKEY_LENGTH))
        quote_vault_bytes = bytes(range(97, 97 + PUBKEY_LENGTH))
        raw = bytearray(QUOTE_VAULT_OFFSET + PUBKEY_LENGTH)
        raw[BASE_MINT_OFFSET:BASE_MINT_OFFSET + PUBKEY_LENGTH] = base_mint_bytes
        raw[QUOTE_MINT_OFFSET:QUOTE_MINT_OFFSET + PUBKEY_LENGTH] = quote_mint_bytes
        raw[BASE_VAULT_OFFSET:BASE_VAULT_OFFSET + PUBKEY_LENGTH] = base_vault_bytes
        raw[QUOTE_VAULT_OFFSET:QUOTE_VAULT_OFFSET + PUBKEY_LENGTH] = quote_vault_bytes

        base_mint = _b58(base_mint_bytes)
        quote_mint = _b58(quote_mint_bytes)
        base_vault = _b58(base_vault_bytes)
        quote_vault = _b58(quote_vault_bytes)
        rpc = FakeRpc(
            {
                "owner": PUMPSWAP_PROGRAM_ID,
                "data": [base64.b64encode(raw).decode("ascii"), "base64"],
            },
            {
                base_vault: parsed_vault(base_mint, 2_000_000, 6),
                quote_vault: parsed_vault(quote_mint, 4_000_000_000, 9),
            },
        )
        provider = PumpSwapProvider("https://example.invalid", lambda _context: {
            "value": 100,
            "source": "test",
            "age_seconds": 0,
        })
        provider.rpc = rpc
        context = MarketContext(
            chain="solana",
            protocol="pumpswap",
            token_address=base_mint,
            quote_address=quote_mint,
            pool_address="pool",
        )

        first = provider.get_tick(context)
        second = provider.get_tick(context)

        self.assertEqual(first.status, "ok")
        self.assertEqual(first.price_quote, 2)
        self.assertEqual(first.price_usd, 200)
        self.assertEqual(first.slot, 12345)
        self.assertEqual(second.status, "ok")
        self.assertEqual(rpc.account_calls, 1)
        self.assertEqual(rpc.multiple_calls, 2)


if __name__ == "__main__":
    unittest.main()
