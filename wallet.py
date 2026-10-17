"""Load a Solana keypair and read SOL / SPL balances."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from solana.rpc.async_api import AsyncClient
from solana.rpc.models import TokenAccountOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from config import (
    LAMPORTS_PER_SOL,
    PRIVATE_KEY,
    PRIVATE_KEY_JSON,
    TOKEN_PROGRAM_ID,
    WALLET_KEYPAIR_PATH,
    require_private_key,
)


def load_keypair() -> Keypair:
    """Load a keypair from a local file or a legacy inline secret."""
    require_private_key()

    if WALLET_KEYPAIR_PATH:
        path = Path(WALLET_KEYPAIR_PATH).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Wallet keypair file not found: {path}")
        secret = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(secret, list) or not all(
            isinstance(value, int) and 0 <= value <= 255 for value in secret
        ):
            raise ValueError(
                "WALLET_KEYPAIR_PATH must contain a Solana CLI JSON byte array."
            )
        return Keypair.from_bytes(bytes(secret))

    if PRIVATE_KEY_JSON:
        secret = json.loads(PRIVATE_KEY_JSON)
        if not isinstance(secret, list):
            raise ValueError("PRIVATE_KEY_JSON must be a JSON array of bytes.")
        return Keypair.from_bytes(bytes(secret))

    # Phantom-style base58 secret, or a JSON array pasted into PRIVATE_KEY.
    if PRIVATE_KEY.startswith("["):
        secret = json.loads(PRIVATE_KEY)
        return Keypair.from_bytes(bytes(secret))

    return Keypair.from_base58_string(PRIVATE_KEY)


def lamports_to_sol(lamports: int) -> float:
    return lamports / LAMPORTS_PER_SOL


async def get_sol_balance(client: AsyncClient, owner: Pubkey) -> int:
    response = await client.get_balance(owner)
    return int(response.value)


async def get_token_accounts(client: AsyncClient, owner: Pubkey) -> list[dict[str, Any]]:
    """Return SPL token accounts owned by `owner` (mint, amount, decimals)."""
    opts = TokenAccountOpts(program_id=TOKEN_PROGRAM_ID)
    response = await client.get_token_accounts_by_owner_json_parsed(owner, opts)
    accounts: list[dict[str, Any]] = []

    for item in response.value:
        parsed = item.account.data.parsed
        info = parsed.get("info", {}) if isinstance(parsed, dict) else {}
        token_amount = info.get("tokenAmount", {})
        accounts.append(
            {
                "pubkey": str(item.pubkey),
                "mint": info.get("mint"),
                "amount": token_amount.get("uiAmount"),
                "decimals": token_amount.get("decimals"),
                "raw": token_amount.get("amount"),
            }
        )
    return accounts


async def print_wallet_summary(client: AsyncClient, keypair: Keypair) -> None:
    owner = keypair.pubkey()
    lamports = await get_sol_balance(client, owner)
    tokens = await get_token_accounts(client, owner)

    print(f"Wallet: {owner}")
    print(f"SOL balance: {lamports_to_sol(lamports):.9f} SOL ({lamports} lamports)")
    if not tokens:
        print("SPL tokens: none")
        return

    print(f"SPL tokens ({len(tokens)}):")
    for token in tokens:
        print(
            f"  mint={token['mint']}  amount={token['amount']}  "
            f"account={token['pubkey']}"
        )
