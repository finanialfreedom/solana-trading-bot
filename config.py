"""Solana RPC, program IDs, and environment settings."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from solana.rpc.commitment import Confirmed, Finalized, Processed
from solders.pubkey import Pubkey

load_dotenv(Path(__file__).resolve().parent / ".env")

# Public mainnet fallbacks. For production use a private RPC (Helius, Triton, QuickNode, Chainstack).
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
WSS_URL = os.getenv(
    "WSS_URL",
    os.getenv("RPC_WSS", "wss://api.mainnet-beta.solana.com"),
)

# Phantom/Solflare private key as base58, or a JSON byte array via PRIVATE_KEY_JSON.
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip()
PRIVATE_KEY_JSON = os.getenv("PRIVATE_KEY_JSON", "").strip()

# Pump.fun main program (token mint + bonding curve).
PUMP_FUN_PROGRAM_ID = Pubkey.from_string(
    os.getenv("PUMP_FUN_PROGRAM_ID", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
)

TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)

COMMITMENT_NAME = os.getenv("COMMITMENT", "processed").lower()
COMMITMENT_MAP = {
    "processed": Processed,
    "confirmed": Confirmed,
    "finalized": Finalized,
}
COMMITMENT = COMMITMENT_MAP.get(COMMITMENT_NAME, Processed)

LAMPORTS_PER_SOL = 1_000_000_000


def require_private_key() -> None:
    if not PRIVATE_KEY and not PRIVATE_KEY_JSON:
        raise RuntimeError(
            "Missing wallet secret. Set PRIVATE_KEY or PRIVATE_KEY_JSON in the .env file."
        )
