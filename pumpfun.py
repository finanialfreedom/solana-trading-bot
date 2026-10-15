"""Pump.fun PDAs, bonding-curve math, event decode, and buy/sell instructions."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import create_associated_token_account, get_associated_token_address

from config import FEE_PROGRAM_ID, PUMP_FUN_PROGRAM_ID, SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID

BUY_EXACT_SOL_IN_DISC = bytes([56, 252, 116, 8, 158, 223, 205, 95])
SELL_DISC = bytes([51, 230, 133, 164, 1, 127, 131, 173])
INIT_USER_VOLUME_DISC = bytes([94, 6, 202, 115, 255, 96, 232, 183])
CREATE_EVENT_DISC = hashlib.sha256(b"event:CreateEvent").digest()[:8]
TRADE_EVENT_DISC = hashlib.sha256(b"event:TradeEvent").digest()[:8]

GLOBAL_PDA, _ = Pubkey.find_program_address([b"global"], PUMP_FUN_PROGRAM_ID)
EVENT_AUTHORITY_PDA, _ = Pubkey.find_program_address([b"__event_authority"], PUMP_FUN_PROGRAM_ID)
GLOBAL_VOLUME_PDA, _ = Pubkey.find_program_address([b"global_volume_accumulator"], PUMP_FUN_PROGRAM_ID)
FEE_CONFIG_PDA, _ = Pubkey.find_program_address(
    [b"fee_config", bytes(PUMP_FUN_PROGRAM_ID)],
    FEE_PROGRAM_ID,
)

# Fallback start curve (classic Pump.fun SOL-paired defaults).
DEFAULT_VIRTUAL_TOKEN_RESERVES = 1_073_000_000_000_000
DEFAULT_VIRTUAL_QUOTE_RESERVES = 30_000_000_000


@dataclass(slots=True)
class BondingCurve:
    virtual_token_reserves: int
    virtual_quote_reserves: int
    real_token_reserves: int
    real_quote_reserves: int
    token_total_supply: int
    complete: bool
    creator: Pubkey | None = None
    creator_fee_bps: int = 0

    @property
    def price(self) -> float:
        if self.virtual_token_reserves <= 0:
            return 0.0
        return self.virtual_quote_reserves / self.virtual_token_reserves


@dataclass(slots=True)
class GlobalConfig:
    fee_recipient: Pubkey
    fee_basis_points: int
    creator_fee_basis_points: int
    initial_virtual_token_reserves: int
    initial_virtual_sol_reserves: int
    initial_real_token_reserves: int = 793_100_000_000_000


@dataclass(slots=True)
class CreateInfo:
    name: str
    symbol: str
    uri: str
    mint: Pubkey
    bonding_curve: Pubkey
    creator: Pubkey | None
    virtual_token_reserves: int
    virtual_quote_reserves: int


@dataclass(slots=True)
class TradeInfo:
    mint: Pubkey
    sol_amount: int
    token_amount: int
    is_buy: bool
    virtual_quote_reserves: int
    virtual_token_reserves: int
    user: Pubkey | None = None


def bonding_curve_pda(mint: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(mint)], PUMP_FUN_PROGRAM_ID)
    return pda


def creator_vault_pda(creator: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address([b"creator-vault", bytes(creator)], PUMP_FUN_PROGRAM_ID)
    return pda


def user_volume_pda(user: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(user)],
        PUMP_FUN_PROGRAM_ID,
    )
    return pda


def associated_bonding_curve(mint: Pubkey, curve: Pubkey) -> Pubkey:
    return get_associated_token_address(curve, mint, TOKEN_PROGRAM_ID)


def _u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def _read_borsh_string(data: bytes, offset: int) -> tuple[str, int]:
    if offset + 4 > len(data):
        raise ValueError("truncated string")
    (length,) = struct.unpack_from("<I", data, offset)
    offset += 4
    end = offset + length
    if end > len(data):
        raise ValueError("truncated string bytes")
    return data[offset:end].decode("utf-8", errors="replace"), end


def encode_option_bool(value: bool | None) -> bytes:
    if value is None:
        return b"\x00"
    return bytes([1, 1 if value else 0])


def parse_bonding_curve(data: bytes) -> BondingCurve:
    if len(data) < 49:
        raise ValueError("bonding curve account too small")
    offset = 8
    virtual_token = _u64(data, offset)
    virtual_quote = _u64(data, offset + 8)
    real_token = _u64(data, offset + 16)
    real_quote = _u64(data, offset + 24)
    supply = _u64(data, offset + 32)
    complete = bool(data[offset + 40])
    creator = None
    creator_fee_bps = 0
    if len(data) >= 81:
        creator = Pubkey.from_bytes(data[49:81])
    if len(data) >= 123:
        creator_fee_bps = _u64(data, 115)
    return BondingCurve(
        virtual_token_reserves=virtual_token,
        virtual_quote_reserves=virtual_quote,
        real_token_reserves=real_token,
        real_quote_reserves=real_quote,
        token_total_supply=supply,
        complete=complete,
        creator=creator,
        creator_fee_bps=creator_fee_bps,
    )


def parse_global(data: bytes) -> GlobalConfig:
    if len(data) < 105:
        raise ValueError("global account too small")
    fee_recipient = Pubkey.from_bytes(data[41:73])
    initial_virtual_token = _u64(data, 73)
    initial_virtual_sol = _u64(data, 81)
    initial_real_token = _u64(data, 89) if len(data) >= 97 else 793_100_000_000_000
    fee_bps = _u64(data, 105) if len(data) >= 113 else 100
    creator_fee_bps = _u64(data, 154) if len(data) >= 162 else 0
    return GlobalConfig(
        fee_recipient=fee_recipient,
        fee_basis_points=fee_bps,
        creator_fee_basis_points=creator_fee_bps,
        initial_virtual_token_reserves=initial_virtual_token,
        initial_virtual_sol_reserves=initial_virtual_sol,
        initial_real_token_reserves=initial_real_token,
    )


def parse_create_event(payload: bytes) -> CreateInfo | None:
    candidates = [payload]
    if len(payload) > 8:
        candidates.append(payload[8:])
    for blob in candidates:
        try:
            return _parse_create_body(blob)
        except Exception:
            continue
    return None


def _parse_create_body(data: bytes) -> CreateInfo:
    offset = 0
    name, offset = _read_borsh_string(data, offset)
    symbol, offset = _read_borsh_string(data, offset)
    uri, offset = _read_borsh_string(data, offset)
    mint = Pubkey.from_bytes(data[offset : offset + 32])
    bonding_curve = Pubkey.from_bytes(data[offset + 32 : offset + 64])
    creator = None
    virtual_token = DEFAULT_VIRTUAL_TOKEN_RESERVES
    virtual_quote = DEFAULT_VIRTUAL_QUOTE_RESERVES
    if offset + 96 <= len(data):
        # user is at +64; creator follows at +96 in current CreateEvent.
        if offset + 128 <= len(data):
            creator = Pubkey.from_bytes(data[offset + 96 : offset + 128])
        if offset + 152 <= len(data):
            virtual_token = _u64(data, offset + 136)
            virtual_quote = _u64(data, offset + 144)
    return CreateInfo(
        name=name,
        symbol=symbol,
        uri=uri,
        mint=mint,
        bonding_curve=bonding_curve,
        creator=creator,
        virtual_token_reserves=virtual_token,
        virtual_quote_reserves=virtual_quote,
    )


def parse_trade_event(payload: bytes) -> TradeInfo | None:
    body = payload[8:] if payload[:8] == TRADE_EVENT_DISC else payload
    if len(body) < 32 + 8 + 8 + 1 + 32 + 8 + 8 + 8:
        return None
    try:
        mint = Pubkey.from_bytes(body[0:32])
        sol_amount = _u64(body, 32)
        token_amount = _u64(body, 40)
        is_buy = bool(body[48])
        virtual_sol = _u64(body, 89)
        virtual_token = _u64(body, 97)
        user = None
        if len(body) >= 81:
            user = Pubkey.from_bytes(body[49:81])
        return TradeInfo(
            mint=mint,
            sol_amount=sol_amount,
            token_amount=token_amount,
            is_buy=is_buy,
            virtual_quote_reserves=virtual_sol,
            virtual_token_reserves=virtual_token,
            user=user,
        )
    except Exception:
        return None


def apply_fee(sol_amount: int, fee_bps: int) -> int:
    if fee_bps <= 0:
        return sol_amount
    return sol_amount * (10_000 - fee_bps) // 10_000


def tokens_out_for_sol_in(curve: BondingCurve, sol_in: int, fee_bps: int) -> int:
    net = apply_fee(sol_in, fee_bps)
    if net <= 0 or curve.virtual_quote_reserves <= 0:
        return 0
    k = curve.virtual_quote_reserves * curve.virtual_token_reserves
    new_quote = curve.virtual_quote_reserves + net
    new_token = k // new_quote
    out = curve.virtual_token_reserves - new_token
    return max(0, min(out, curve.real_token_reserves or out))


def sol_out_for_tokens_in(curve: BondingCurve, token_in: int, fee_bps: int) -> int:
    if token_in <= 0 or curve.virtual_token_reserves <= 0:
        return 0
    k = curve.virtual_quote_reserves * curve.virtual_token_reserves
    new_token = curve.virtual_token_reserves + token_in
    new_quote = k // new_token
    gross = curve.virtual_quote_reserves - new_quote
    return max(0, apply_fee(gross, fee_bps))


def _buy_sell_accounts(
    *,
    mint: Pubkey,
    bonding_curve: Pubkey,
    associated_curve: Pubkey,
    associated_user: Pubkey,
    user: Pubkey,
    fee_recipient: Pubkey,
    creator: Pubkey,
    token_program: Pubkey,
) -> list[AccountMeta]:
    return [
        AccountMeta(GLOBAL_PDA, False, False),
        AccountMeta(fee_recipient, False, True),
        AccountMeta(mint, False, False),
        AccountMeta(bonding_curve, False, True),
        AccountMeta(associated_curve, False, True),
        AccountMeta(associated_user, False, True),
        AccountMeta(user, True, True),
        AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        AccountMeta(token_program, False, False),
        AccountMeta(creator_vault_pda(creator), False, True),
        AccountMeta(EVENT_AUTHORITY_PDA, False, False),
        AccountMeta(PUMP_FUN_PROGRAM_ID, False, False),
        AccountMeta(GLOBAL_VOLUME_PDA, False, False),
        AccountMeta(user_volume_pda(user), False, True),
        AccountMeta(FEE_CONFIG_PDA, False, False),
        AccountMeta(FEE_PROGRAM_ID, False, False),
    ]


def build_buy_exact_sol_in(
    *,
    mint: Pubkey,
    bonding_curve: Pubkey,
    associated_curve: Pubkey,
    associated_user: Pubkey,
    user: Pubkey,
    fee_recipient: Pubkey,
    creator: Pubkey,
    spendable_sol: int,
    min_tokens_out: int,
    token_program: Pubkey = TOKEN_PROGRAM_ID,
) -> Instruction:
    data = (
        BUY_EXACT_SOL_IN_DISC
        + struct.pack("<Q", spendable_sol)
        + struct.pack("<Q", min_tokens_out)
        + encode_option_bool(None)
        + encode_option_bool(None)
    )
    accounts = _buy_sell_accounts(
        mint=mint,
        bonding_curve=bonding_curve,
        associated_curve=associated_curve,
        associated_user=associated_user,
        user=user,
        fee_recipient=fee_recipient,
        creator=creator,
        token_program=token_program,
    )
    return Instruction(PUMP_FUN_PROGRAM_ID, data, accounts)


def build_sell(
    *,
    mint: Pubkey,
    bonding_curve: Pubkey,
    associated_curve: Pubkey,
    associated_user: Pubkey,
    user: Pubkey,
    fee_recipient: Pubkey,
    creator: Pubkey,
    token_amount: int,
    min_sol_output: int,
    token_program: Pubkey = TOKEN_PROGRAM_ID,
) -> Instruction:
    data = SELL_DISC + struct.pack("<Q", token_amount) + struct.pack("<Q", min_sol_output)
    # Sell account order: token_program comes after creator_vault (IDL).
    accounts = [
        AccountMeta(GLOBAL_PDA, False, False),
        AccountMeta(fee_recipient, False, True),
        AccountMeta(mint, False, False),
        AccountMeta(bonding_curve, False, True),
        AccountMeta(associated_curve, False, True),
        AccountMeta(associated_user, False, True),
        AccountMeta(user, True, True),
        AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        AccountMeta(creator_vault_pda(creator), False, True),
        AccountMeta(token_program, False, False),
        AccountMeta(EVENT_AUTHORITY_PDA, False, False),
        AccountMeta(PUMP_FUN_PROGRAM_ID, False, False),
        AccountMeta(FEE_CONFIG_PDA, False, False),
        AccountMeta(FEE_PROGRAM_ID, False, False),
    ]
    return Instruction(PUMP_FUN_PROGRAM_ID, data, accounts)


def build_init_user_volume(payer: Pubkey, user: Pubkey) -> Instruction:
    accounts = [
        AccountMeta(payer, True, True),
        AccountMeta(user, False, False),
        AccountMeta(user_volume_pda(user), False, True),
        AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        AccountMeta(EVENT_AUTHORITY_PDA, False, False),
        AccountMeta(PUMP_FUN_PROGRAM_ID, False, False),
    ]
    return Instruction(PUMP_FUN_PROGRAM_ID, INIT_USER_VOLUME_DISC, accounts)


def build_create_ata(payer: Pubkey, owner: Pubkey, mint: Pubkey) -> Instruction:
    return create_associated_token_account(payer, owner, mint, TOKEN_PROGRAM_ID)
