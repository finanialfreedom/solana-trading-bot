"""Read-only validation for the isolated live deployment."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solana.rpc.async_api import AsyncClient

from config import (
    DRY_RUN,
    ENABLE_LIVE_TRADING,
    MAX_ACCOUNT_RISK_PCT,
    MAX_OPEN_POSITIONS,
    MAX_SOL_PER_TRADE,
    MAX_TRADES,
    RPC_URL,
    RPC_URL_LOG,
    WALLET_KEYPAIR_PATH,
)
from wallet import get_sol_balance, lamports_to_sol, load_keypair


async def main() -> None:
    keypair = load_keypair()
    owner = keypair.pubkey()
    async with AsyncClient(RPC_URL) as client:
        balance = lamports_to_sol(await get_sol_balance(client, owner))

    print(f"Wallet key file: {WALLET_KEYPAIR_PATH}")
    print(f"Public address: {owner}")
    print(f"On-chain balance: {balance:.9f} SOL")
    print(f"RPC: {RPC_URL_LOG}")
    print(f"Trade size: {MAX_SOL_PER_TRADE:.5f} SOL")
    print(f"Maximum open positions: {MAX_OPEN_POSITIONS}")
    print(f"Session trade limit: {MAX_TRADES}")
    print(f"Configured account risk: {MAX_ACCOUNT_RISK_PCT:.0f}%")
    print(
        "Live status: "
        + (
            "ARMED"
            if ENABLE_LIVE_TRADING and not DRY_RUN
            else "LOCKED (no transactions can be sent)"
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
