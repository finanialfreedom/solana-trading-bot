"""Solana RPC, program IDs, and environment settings."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from solana.rpc.commitment import Confirmed, Finalized, Processed
from solders.pubkey import Pubkey

load_dotenv(Path(__file__).resolve().parent / ".env")


def _redact_rpc(url: str) -> str:
    if "api-key=" in url.lower():
        head, _sep, _key = url.partition("api-key=")
        return f"{head}api-key=***"
    return url


# Helius free/dev RPC. If HELIUS_API_KEY is set, it replaces the public Solana endpoints.
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "").strip()
_raw_rpc = os.getenv("RPC_URL", "").strip()
_raw_wss = (os.getenv("WSS_URL") or os.getenv("RPC_WSS") or "").strip()
_public_http = "https://api.mainnet-beta.solana.com"
_public_wss = "wss://api.mainnet-beta.solana.com"

if HELIUS_API_KEY and (not _raw_rpc or _public_http in _raw_rpc):
    RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
else:
    RPC_URL = _raw_rpc or _public_http

if HELIUS_API_KEY and (not _raw_wss or _public_wss in _raw_wss):
    WSS_URL = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
else:
    WSS_URL = _raw_wss or _public_wss

RPC_URL_LOG = _redact_rpc(RPC_URL)
WSS_URL_LOG = _redact_rpc(WSS_URL)

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
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
FEE_PROGRAM_ID = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    return int(raw)


# Trading — dry-run is on until you set DRY_RUN=false AND ENABLE_LIVE_TRADING=true.
DRY_RUN = _env_bool("DRY_RUN", True)
ENABLE_LIVE_TRADING = _env_bool("ENABLE_LIVE_TRADING", False)
BUY_ON_NEW_TOKEN = _env_bool("BUY_ON_NEW_TOKEN", True)
SCAN_EXISTING_COINS = _env_bool("SCAN_EXISTING_COINS", True)
SCAN_PAGE_SIZE = _env_int("SCAN_PAGE_SIZE", 50)
SCAN_INTERVAL_SECONDS = _env_float("SCAN_INTERVAL_SECONDS", 3.0)
SCAN_REVIEW_DELAY_SECONDS = _env_float("SCAN_REVIEW_DELAY_SECONDS", 0.05)

BUY_SOL_AMOUNT = _env_float("BUY_SOL_AMOUNT", 1.0)
BUY_PROBE_SOL = _env_float("BUY_PROBE_SOL", 0.2)
BUY_ADD_SOL = _env_float("BUY_ADD_SOL", 0.8)
MAX_SOL_PER_TRADE = _env_float("MAX_SOL_PER_TRADE", 1.0)
MAX_TOTAL_SOL_AT_RISK = _env_float("MAX_TOTAL_SOL_AT_RISK", 8.0)
MAX_OPEN_POSITIONS = _env_int("MAX_OPEN_POSITIONS", 16)
MAX_TRADES = _env_int("MAX_TRADES", 100)
MIN_SOL_RESERVE = _env_float("MIN_SOL_RESERVE", 0.02)

CONFIRM_SECONDS = _env_float("CONFIRM_SECONDS", 5.0)
CONFIRM_EXPIRE_SECONDS = _env_float("CONFIRM_EXPIRE_SECONDS", 25.0)
CONFIRM_TICK_SECONDS = _env_float("CONFIRM_TICK_SECONDS", 0.25)
ADD_AFTER_SECONDS = _env_float("ADD_AFTER_SECONDS", 10.0)
MIN_UNIQUE_BUYERS = _env_int("MIN_UNIQUE_BUYERS", 0)
MIN_CONFIRM_BUY_SOL = _env_float("MIN_CONFIRM_BUY_SOL", 0.0)
MIN_CONFIRM_MC_USD = _env_float("MIN_CONFIRM_MC_USD", 0.0)
MIN_ADD_UNIQUE_BUYERS = _env_int("MIN_ADD_UNIQUE_BUYERS", 0)
MIN_ADD_MC_USD = _env_float("MIN_ADD_MC_USD", 5_000.0)
DEAD_COIN_SECONDS = _env_float("DEAD_COIN_SECONDS", 30.0)
DEAD_COIN_MIN_PNL_PCT = _env_float("DEAD_COIN_MIN_PNL_PCT", 10.0)
PUMP_TOKEN_SUPPLY = _env_float("PUMP_TOKEN_SUPPLY", 1_000_000_000_000_000.0)

STOP_LOSS_PCT = _env_float("STOP_LOSS_PCT", 3.0)
BREAKEVEN_ARM_PCT = _env_float("BREAKEVEN_ARM_PCT", 3.5)
TAKE_PROFIT_PCT = _env_float("TAKE_PROFIT_PCT", 30.0)  # kept as fallback
TAKE_PROFIT_TRIGGER_PCT = _env_float("TAKE_PROFIT_TRIGGER_PCT", TAKE_PROFIT_PCT)
TRAILING_STOP_PCT = _env_float("TRAILING_STOP_PCT", 15.0)
TRAILING_LOCK_BREAKEVEN = _env_bool("TRAILING_LOCK_BREAKEVEN", True)
SLIPPAGE_BPS = _env_int("SLIPPAGE_BPS", 800)
PRIORITY_FEE_MICROLAMPORTS = _env_int("PRIORITY_FEE_MICROLAMPORTS", 100_000)
COMPUTE_UNIT_LIMIT = _env_int("COMPUTE_UNIT_LIMIT", 250_000)
POSITION_POLL_SECONDS = _env_float("POSITION_POLL_SECONDS", 2.0)
TRAILING_TICK_SECONDS = _env_float("TRAILING_TICK_SECONDS", 0.25)
TRAILING_ARMED_TICK_SECONDS = _env_float("TRAILING_ARMED_TICK_SECONDS", 0.08)
# Re-poll a position's curve only if it has not been marked for this long.
TRAILING_STALE_SECONDS = _env_float("TRAILING_STALE_SECONDS", 0.2)

# Graduation (bonding curve -> PumpSwap AMM) handling.# When a coin completes its curve the curve account is closed and reports zero
# reserves. Treating that as "price is zero" liquidated every winner for -100%,
# so instead we hold through graduation and re-price from the AMM.
HOLD_THROUGH_GRADUATION = _env_bool("HOLD_THROUGH_GRADUATION", True)
# How long to keep retrying for an AMM price before giving up on a position.
GRADUATION_PRICE_TIMEOUT_SECONDS = _env_float("GRADUATION_PRICE_TIMEOUT_SECONDS", 120.0)

# Hybrid scale-out: bank partial profit at market-cap milestones, let the rest ride
# on the trailing stop. Set a milestone to 0 to disable it.
MC_MILESTONE_1_USD = _env_float("MC_MILESTONE_1_USD", 250_000.0)
MC_MILESTONE_1_SELL_PCT = _env_float("MC_MILESTONE_1_SELL_PCT", 50.0)
MC_MILESTONE_2_USD = _env_float("MC_MILESTONE_2_USD", 500_000.0)
MC_MILESTONE_2_SELL_PCT = _env_float("MC_MILESTONE_2_SELL_PCT", 50.0)
MC_MILESTONE_3_USD = _env_float("MC_MILESTONE_3_USD", 0.0)
MC_MILESTONE_3_SELL_PCT = _env_float("MC_MILESTONE_3_SELL_PCT", 50.0)

# (market-cap USD, percent of remaining tokens to sell) in ascending order.
MC_SCALE_OUT: tuple[tuple[float, float], ...] = tuple(
    (target, pct)
    for target, pct in (
        (MC_MILESTONE_1_USD, MC_MILESTONE_1_SELL_PCT),
        (MC_MILESTONE_2_USD, MC_MILESTONE_2_SELL_PCT),
        (MC_MILESTONE_3_USD, MC_MILESTONE_3_SELL_PCT),
    )
    if target > 0 and pct > 0
)

# Tiered trailing tolerance. A fixed callback arms early and cuts a runner on an
# ordinary wobble, so the tolerance widens as the peak gain grows: a coin that only
# manages +20% is protected tightly, while one that has already multiplied gets room
# to keep going. Entries are (peak gain %, callback %), ascending; TRAILING_STOP_PCT
# is the floor used below the first tier.
TRAIL_TIERS: tuple[tuple[float, float], ...] = tuple(
    (peak, callback)
    for peak, callback in (
        (_env_float("TRAIL_TIER_1_PEAK", 50.0), _env_float("TRAIL_TIER_1_CALLBACK", 18.0)),
        (_env_float("TRAIL_TIER_2_PEAK", 150.0), _env_float("TRAIL_TIER_2_CALLBACK", 28.0)),
        (_env_float("TRAIL_TIER_3_PEAK", 500.0), _env_float("TRAIL_TIER_3_CALLBACK", 38.0)),
        (_env_float("TRAIL_TIER_4_PEAK", 2000.0), _env_float("TRAIL_TIER_4_CALLBACK", 48.0)),
    )
    if peak > 0 and callback > 0
)

# Ratchet floor. A "percent below peak" trailing stop scales with the peak, so a big
# winner can still round-trip into an overall loss during a gradual decline. Once a
# position's peak gain passes a level, it can never be sold below the paired gain.
# (peak gain %, locked minimum gain %), ascending.
RATCHET_LEVELS: tuple[tuple[float, float], ...] = tuple(
    (peak, floor)
    for peak, floor in (
        (_env_float("RATCHET_1_PEAK", 50.0), _env_float("RATCHET_1_FLOOR", 25.0)),
        (_env_float("RATCHET_2_PEAK", 150.0), _env_float("RATCHET_2_FLOOR", 100.0)),
        (_env_float("RATCHET_3_PEAK", 500.0), _env_float("RATCHET_3_FLOOR", 350.0)),
        (_env_float("RATCHET_4_PEAK", 2000.0), _env_float("RATCHET_4_FLOOR", 1400.0)),
    )
    if peak > 0 and floor > 0
)

# Security filters (apply in dry-run and live).
SECURITY_FILTERS_ENABLED = _env_bool("SECURITY_FILTERS_ENABLED", True)
MIN_LIQUIDITY_USD = _env_float("MIN_LIQUIDITY_USD", 0.0)
MAX_INSIDER_TOKENS = _env_float("MAX_INSIDER_TOKENS", 10_000_000.0)
MAX_HOLDER_PCT = _env_float("MAX_HOLDER_PCT", 30.0)
MAX_TAX_BPS = _env_int("MAX_TAX_BPS", 100)  # 1% buy/sell tax cap
SOL_USD_FALLBACK = _env_float("SOL_USD_FALLBACK", 150.0)
REJECT_FREEZE_AUTHORITY = _env_bool("REJECT_FREEZE_AUTHORITY", True)
REJECT_EXTERNAL_MINT_AUTH = _env_bool("REJECT_EXTERNAL_MINT_AUTH", True)
REJECT_TRANSFER_FEE = _env_bool("REJECT_TRANSFER_FEE", False)
REJECT_MUTABLE_TAX = _env_bool("REJECT_MUTABLE_TAX", True)
REJECT_UNVERIFIED = _env_bool("REJECT_UNVERIFIED", False)
REJECT_PERMANENT_DELEGATE = _env_bool("REJECT_PERMANENT_DELEGATE", True)
REJECT_TRANSFER_HOOK = _env_bool("REJECT_TRANSFER_HOOK", True)
REJECT_NON_TRANSFERABLE = _env_bool("REJECT_NON_TRANSFERABLE", True)
REJECT_PAUSABLE = _env_bool("REJECT_PAUSABLE", True)
MIN_LP_LOCKED_PCT = _env_float("MIN_LP_LOCKED_PCT", 0.0)
MAX_CREATOR_HOLD_PCT = _env_float("MAX_CREATOR_HOLD_PCT", 10.0)
MIN_HOLDERS = _env_int("MIN_HOLDERS", 0)
BUNDLE_WALLET_COUNT = _env_int("BUNDLE_WALLET_COUNT", 5)
MAX_CREATOR_LAUNCHES = _env_int("MAX_CREATOR_LAUNCHES", 5)
MAX_CREATOR_RUGS = _env_int("MAX_CREATOR_RUGS", 1)
REJECT_CLONE_TICKERS = _env_bool("REJECT_CLONE_TICKERS", True)
REJECT_UNKNOWN_TOKEN_PROGRAM = _env_bool("REJECT_UNKNOWN_TOKEN_PROGRAM", True)
HONEYPOT_SIMULATION = _env_bool("HONEYPOT_SIMULATION", True)


def require_private_key() -> None:
    if not PRIVATE_KEY and not PRIVATE_KEY_JSON:
        raise RuntimeError(
            "Missing wallet secret. Set PRIVATE_KEY or PRIVATE_KEY_JSON in the .env file."
        )


def live_trading_enabled() -> bool:
    return ENABLE_LIVE_TRADING and not DRY_RUN
