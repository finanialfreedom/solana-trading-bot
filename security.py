"""On-chain safety filters. These run in dry-run and live mode."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

try:  # Prefer the OS trust store so TLS works behind AV/proxy root certs.
    import truststore

    _SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
except Exception:  # pragma: no cover - fall back to httpx's default bundle
    _SSL_CONTEXT = True

# One pooled HTTP client for every price lookup. Building a client per call forces a
# fresh TCP + TLS handshake each time, which costs several round trips; at ~20ms RTT
# that is tens of milliseconds of pure overhead on every single price read.
_HTTP_CLIENT: httpx.AsyncClient | None = None


def _http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=8.0,
            verify=_SSL_CONTEXT,
            headers={"accept": "application/json"},
            limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=60.0),
        )
    return _HTTP_CLIENT

from solana.rpc.async_api import AsyncClient
from solana.rpc.models import DataSliceOpts, MemcmpOpts
from solders.pubkey import Pubkey
from solders.token.state import Mint
from spl.token.constants import TOKEN_2022_PROGRAM_ID
from spl.token.instructions import get_associated_token_address

from config import (
    BUNDLE_WALLET_COUNT,
    HONEYPOT_SIMULATION,
    LAMPORTS_PER_SOL,
    MAX_CREATOR_HOLD_PCT,
    MAX_CREATOR_LAUNCHES,
    MAX_CREATOR_RUGS,
    MAX_HOLDER_PCT,
    MAX_INSIDER_TOKENS,
    MAX_TAX_BPS,
    MIN_HOLDERS,
    MIN_LIQUIDITY_USD,
    MIN_LP_LOCKED_PCT,
    PUMP_FUN_PROGRAM_ID,
    REJECT_CLONE_TICKERS,
    REJECT_EXTERNAL_MINT_AUTH,
    REJECT_FREEZE_AUTHORITY,
    REJECT_MUTABLE_TAX,
    REJECT_NON_TRANSFERABLE,
    REJECT_PAUSABLE,
    REJECT_PERMANENT_DELEGATE,
    REJECT_TRANSFER_FEE,
    REJECT_TRANSFER_HOOK,
    REJECT_UNKNOWN_TOKEN_PROGRAM,
    REJECT_UNVERIFIED,
    SECURITY_FILTERS_ENABLED,
    SOL_USD_FALLBACK,
    TOKEN_PROGRAM_ID,
)
from pumpfun import (
    BondingCurve,
    associated_bonding_curve,
    sol_out_for_tokens_in,
    tokens_out_for_sol_in,
)

logger = logging.getLogger("pump-security")

TOKEN_2022 = Pubkey.from_string(str(TOKEN_2022_PROGRAM_ID))
METADATA_PROGRAM = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")
PUMP_AMM = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
WSOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
SYSTEM = Pubkey.from_string("11111111111111111111111111111111")
ALLOWED_TOKEN_PROGRAMS = {str(TOKEN_PROGRAM_ID), str(TOKEN_2022)}
BURN_ADDRESSES = {
    str(SYSTEM),
    "1nc1nerator11111111111111111111111111111111",
}


def _coingecko_price(data: dict) -> float:
    return float(data["solana"]["usd"])


def _jupiter_price(data: dict) -> float:
    return float(data[str(WSOL)]["usdPrice"])


def _binance_price(data: dict) -> float:
    return float(data["price"])


# Public SOL/USD feeds, tried in order until one answers.
_PRICE_SOURCES = (
    (
        "coingecko",
        "https://api.coingecko.com/api/v3/simple/price",
        {"ids": "solana", "vs_currencies": "usd"},
        _coingecko_price,
    ),
    (
        "jupiter",
        "https://lite-api.jup.ag/price/v3",
        {"ids": str(WSOL)},
        _jupiter_price,
    ),
    (
        "binance",
        "https://api.binance.com/api/v3/ticker/price",
        {"symbol": "SOLUSDT"},
        _binance_price,
    ),
)

CLONE_TICKERS = {
    "BTC", "ETH", "SOL", "WSOL", "USDT", "USDC", "DAI", "BNB", "XRP", "DOGE",
    "SHIB", "PEPE", "BONK", "WIF", "JUP", "JTO", "PYTH", "RENDER", "WETH",
    "WBTC", "TRUMP", "MELANIA", "USDC", "USD1", "HYPE", "ADA", "AVAX", "LINK",
    "UNI", "AAVE", "SUI", "APT", "TON", "NOT", "WLD", "OP", "ARB", "MATIC",
    "POL", "NEAR", "FIL", "ATOM", "DOT", "LTC", "BCH", "XLM", "ETC",
}
CLONE_NAMES = {
    "bitcoin", "ethereum", "solana", "tether", "dogecoin", "official trump",
    "wrapped sol", "wrapped bitcoin", "usd coin",
}

EXT_TRANSFER_FEE_CONFIG = 1
EXT_DEFAULT_ACCOUNT_STATE = 6
EXT_NON_TRANSFERABLE = 9
EXT_PERMANENT_DELEGATE = 12
EXT_TRANSFER_HOOK = 14
EXT_PAUSABLE = 26
DEFAULT_ACCOUNT_FROZEN = 2

HISTORY_PATH = Path(__file__).resolve().parent / "reports" / "creator_history.json"


@dataclass(slots=True)
class SecurityReport:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    liquidity_usd: float = 0.0
    sol_usd: float = 0.0
    mint_authority: str = ""
    freeze_authority: str = ""
    tax_bps: int = 0
    top_holder_tokens: float = 0.0
    top_holder: str = ""
    lp_locked_pct: float = 0.0
    creator_hold_pct: float = 0.0
    holders: int = 0

    def lp_lines(self) -> str:
        dollars = self.liquidity_usd
        if dollars >= 1000:
            liq = f"${dollars / 1000:.1f}K".replace(".0K", "K")
        else:
            liq = f"${dollars:,.0f}"
        return (
            f"1- Liquidity (LP tokens) Locked {self.lp_locked_pct:.2f}% | "
            f"2- Total Locked Liquidity {liq}"
        )

    def summary(self) -> str:
        snap = self.lp_lines()
        if self.ok:
            return f"pass {snap}"
        return f"{snap} | BLOCK {'; '.join(self.reasons) or 'blocked'}"


def _account_bytes(account: object) -> bytes:
    data = getattr(account, "data", None)
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    if isinstance(data, (list, tuple)) and data:
        return bytes(data[0])
    return bytes(data)


def _iter_t22_extensions(data: bytes) -> list[tuple[int, bytes]]:
    if len(data) <= 83:
        return []
    offset = 166 if len(data) >= 166 else 82
    found: list[tuple[int, bytes]] = []
    while offset + 4 <= len(data):
        ext_type = int.from_bytes(data[offset : offset + 2], "little")
        length = int.from_bytes(data[offset + 2 : offset + 4], "little")
        offset += 4
        if length == 0 and ext_type == 0:
            break
        blob = data[offset : offset + length]
        found.append((ext_type, blob))
        offset += length
        if offset > len(data):
            break
    return found


def _read_borsh_string(data: bytes, offset: int) -> tuple[str, int]:
    if offset + 4 > len(data):
        raise ValueError("truncated string")
    (length,) = struct.unpack_from("<I", data, offset)
    offset += 4
    end = offset + length
    if end > len(data):
        raise ValueError("truncated string bytes")
    return data[offset:end].decode("utf-8", errors="replace"), end


def _transfer_fee_bps(blob: bytes) -> int:
    if len(blob) < 90:
        return 0
    older_bps = struct.unpack_from("<H", blob, 88)[0]
    newer_bps = struct.unpack_from("<H", blob, 104)[0] if len(blob) >= 106 else 0
    return max(int(older_bps), int(newer_bps))


def _transfer_fee_is_open(blob: bytes) -> bool:
    """True when someone can still change buy/sell tax."""
    if len(blob) < 32:
        return True
    if blob[:32] == bytes(32):
        return False
    if blob[:4] == b"\x01\x00\x00\x00":
        return len(blob) < 36 or blob[4:36] != bytes(32)
    return True


def _parse_metaplex_metadata(data: bytes) -> tuple[str, bool] | None:
    try:
        offset = 1 + 32 + 32
        _name, offset = _read_borsh_string(data, offset)
        _symbol, offset = _read_borsh_string(data, offset)
        uri, offset = _read_borsh_string(data, offset)
        offset += 2
        if offset >= len(data):
            return uri.strip().strip("\x00"), False
        has_creators = data[offset]
        offset += 1
        verified = False
        if has_creators == 1:
            if offset + 4 > len(data):
                return uri.strip().strip("\x00"), False
            (count,) = struct.unpack_from("<I", data, offset)
            offset += 4
            for _ in range(min(count, 8)):
                if offset + 34 > len(data):
                    break
                if data[offset + 32] != 0:
                    verified = True
                    break
                offset += 34
        return uri.strip().strip("\x00"), verified
    except Exception:
        return None


def _ui_amount(item: object) -> tuple[str, float]:
    address = str(getattr(item, "address", None) or getattr(item, "pubkey", "") or "")
    amount = getattr(item, "amount", None)
    ui = float(getattr(amount, "ui_amount", 0) or 0) if amount is not None else 0.0
    return address, ui


class CreatorHistory:
    def __init__(self, path: Path = HISTORY_PATH) -> None:
        self.path = path
        self._data: dict[str, dict[str, int]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def stats_for(self, creator: Pubkey | None) -> dict[str, int]:
        if creator is None:
            return {"launches": 0, "rugs": 0}
        row = self._data.get(str(creator), {})
        return {
            "launches": int(row.get("launches", 0)),
            "rugs": int(row.get("rugs", 0)),
        }

    def record_launch(self, creator: Pubkey | None) -> dict[str, int]:
        if creator is None:
            return {"launches": 0, "rugs": 0}
        key = str(creator)
        row = self._data.setdefault(key, {"launches": 0, "rugs": 0})
        row["launches"] = int(row.get("launches", 0)) + 1
        self._save()
        return row

    def record_rug(self, creator: Pubkey | None) -> None:
        if creator is None:
            return
        key = str(creator)
        row = self._data.setdefault(key, {"launches": 0, "rugs": 0})
        row["rugs"] = int(row.get("rugs", 0)) + 1
        self._save()


class SecurityScanner:
    def __init__(self, client: AsyncClient) -> None:
        self.client = client
        self._sol_usd = SOL_USD_FALLBACK
        self._sol_usd_ts = 0.0
        self._token_usd: dict[str, tuple[float, float]] = {}
        self.history = CreatorHistory()

    def note_rug(self, creator: Pubkey | None) -> None:
        self.history.record_rug(creator)

    async def sol_usd(self) -> float:
        now = time.time()
        if now - self._sol_usd_ts < 60 and self._sol_usd > 0:
            return self._sol_usd
        price = await self._fetch_sol_usd()
        if price > 0:
            self._sol_usd = price
            self._sol_usd_ts = now
        else:
            logger.warning("SOL/USD fetch failed; using %.2f", self._sol_usd)
        return self._sol_usd

    async def _fetch_sol_usd(self) -> float:
        """Try each public price feed until one returns a usable value."""
        http = _http_client()
        for name, url, params, extract in _PRICE_SOURCES:
            try:
                resp = await http.get(url, params=params)
                resp.raise_for_status()
                price = float(extract(resp.json()))
            except Exception as exc:
                logger.debug("SOL/USD via %s failed: %s", name, exc)
                continue
            if price > 0:
                logger.debug("SOL/USD via %s = %.2f", name, price)
                return price
        return 0.0

    async def token_usd_price(self, mint: str, *, max_age: float = 3.0) -> float:
        """USD price per whole token from Jupiter, for post-graduation (AMM) marks.

        The bonding curve is closed once a coin graduates, so its reserves read zero.
        Jupiter prices any liquid token, which keeps graduated positions valued.
        """
        now = time.time()
        cached = self._token_usd.get(mint)
        if cached is not None and now - cached[1] < max_age:
            return cached[0]
        price = 0.0
        try:
            http = _http_client()
            resp = await http.get(
                "https://lite-api.jup.ag/price/v3",
                params={"ids": mint},
                timeout=6.0,
            )
            resp.raise_for_status()
            entry = resp.json().get(mint) or {}
            price = float(entry.get("usdPrice") or 0.0)
        except Exception as exc:
            logger.debug("AMM price via jupiter failed for %s: %s", mint, exc)
        if price > 0:
            self._token_usd[mint] = (price, now)
        return price

    async def review(
        self,
        *,
        mint: Pubkey,
        curve: BondingCurve,
        bonding_curve: Pubkey,
        creator: Pubkey | None,
        name: str = "",
        symbol: str = "",
        uri: str = "",
        fee_bps: int = 100,
        initial_real_token_reserves: int = 793_100_000_000_000,
        existing: bool = False,
    ) -> SecurityReport:
        report = SecurityReport(ok=True, sol_usd=await self.sol_usd())
        if not SECURITY_FILTERS_ENABLED:
            return report

        pool_usd = (curve.real_quote_reserves / LAMPORTS_PER_SOL) * report.sol_usd
        report.liquidity_usd = pool_usd

        self._check_clone(name, symbol, report)
        self._check_creator_history(creator, report, existing=existing)
        if not curve.complete:
            self._check_lp_lock(curve, initial_real_token_reserves, report)
        if HONEYPOT_SIMULATION:
            self._check_honeypot(curve, fee_bps, report)

        await self._check_mint(mint, bonding_curve, report)
        await self._check_verified(mint, report, uri=uri, existing=existing)
        await self._check_holders(mint, bonding_curve, creator, report, existing=existing)
        if curve.complete:
            await self._check_pumpswap_lp_burn(mint, creator, report)

        if fee_bps > MAX_TAX_BPS:
            report.reasons.append(
                f"buy-sell-tax {fee_bps / 100:.2f}% > {MAX_TAX_BPS / 100:.2f}%"
            )

        if MIN_LIQUIDITY_USD > 0 and pool_usd < MIN_LIQUIDITY_USD:
            report.reasons.append(
                f"total-liquidity ${pool_usd:,.0f} < ${MIN_LIQUIDITY_USD:,.0f}"
            )

        report.ok = not report.reasons
        return report

    def _check_clone(self, name: str, symbol: str, report: SecurityReport) -> None:
        if not REJECT_CLONE_TICKERS:
            return
        ticker = (symbol or "").strip().upper()
        title = (name or "").strip().lower()
        if ticker in CLONE_TICKERS:
            report.reasons.append(f"clone-ticker {ticker}")
        if title in CLONE_NAMES:
            report.reasons.append(f"clone-name {name}")

    def _check_creator_history(
        self,
        creator: Pubkey | None,
        report: SecurityReport,
        *,
        existing: bool = False,
    ) -> None:
        stats = self.history.stats_for(creator) if existing else self.history.record_launch(creator)
        if stats["rugs"] >= MAX_CREATOR_RUGS:
            report.reasons.append(f"creator-rug-streak rugs={stats['rugs']}")
        if stats["launches"] > MAX_CREATOR_LAUNCHES:
            report.reasons.append(
                f"serial-deployer launches={stats['launches']} > {MAX_CREATOR_LAUNCHES}"
            )

    def _check_lp_lock(
        self,
        curve: BondingCurve,
        initial_real: int,
        report: SecurityReport,
    ) -> None:
        baseline = initial_real or curve.real_token_reserves or 1
        locked_pct = curve.real_token_reserves / baseline * 100.0
        report.lp_locked_pct = locked_pct
        if MIN_LP_LOCKED_PCT > 0 and locked_pct < MIN_LP_LOCKED_PCT:
            report.reasons.append(
                f"lp-locked {locked_pct:.1f}% < {MIN_LP_LOCKED_PCT:.0f}%"
            )

    def _check_honeypot(self, curve: BondingCurve, fee_bps: int, report: SecurityReport) -> None:
        probe = max(int(0.01 * LAMPORTS_PER_SOL), 1)
        tokens = tokens_out_for_sol_in(curve, probe, fee_bps)
        if tokens <= 0:
            report.reasons.append("honeypot-buy-zero")
            return
        after = BondingCurve(
            virtual_token_reserves=max(curve.virtual_token_reserves - tokens, 1),
            virtual_quote_reserves=curve.virtual_quote_reserves + probe,
            real_token_reserves=curve.real_token_reserves,
            real_quote_reserves=curve.real_quote_reserves,
            token_total_supply=curve.token_total_supply,
            complete=curve.complete,
            creator=curve.creator,
            creator_fee_bps=curve.creator_fee_bps,
        )
        sol_back = sol_out_for_tokens_in(after, tokens, fee_bps)
        if sol_back <= 0:
            report.reasons.append("honeypot-cannot-sell")

    async def _check_mint(self, mint: Pubkey, bonding_curve: Pubkey, report: SecurityReport) -> None:
        resp = await self.client.get_account_info(mint)
        if resp.value is None:
            # Brand-new Pump.fun mints are often not visible on public RPC yet.
            return
        owner = str(resp.value.owner)
        if REJECT_UNKNOWN_TOKEN_PROGRAM and owner not in ALLOWED_TOKEN_PROGRAMS:
            report.reasons.append(f"blacklisted-token-program {owner}")

        data = _account_bytes(resp.value)
        try:
            parsed = Mint.from_bytes(data[:82] if len(data) >= 82 else data)
        except Exception:
            report.reasons.append("mint-parse-failed")
            return

        mint_auth = parsed.mint_authority
        freeze_auth = parsed.freeze_authority
        report.mint_authority = str(mint_auth) if mint_auth else "revoked"
        report.freeze_authority = str(freeze_auth) if freeze_auth else "revoked"

        allowed_minters = {str(PUMP_FUN_PROGRAM_ID), str(bonding_curve)}
        if REJECT_EXTERNAL_MINT_AUTH and mint_auth and str(mint_auth) not in allowed_minters:
            report.reasons.append(f"mint-authority-unlocked {mint_auth}")
        if REJECT_FREEZE_AUTHORITY and freeze_auth:
            report.reasons.append(f"freeze-authority-enabled {freeze_auth}")

        if owner != str(TOKEN_2022):
            return
        for ext_type, blob in _iter_t22_extensions(data):
            if ext_type == EXT_TRANSFER_FEE_CONFIG:
                bps = _transfer_fee_bps(blob)
                report.tax_bps = bps
                if REJECT_MUTABLE_TAX and _transfer_fee_is_open(blob):
                    report.reasons.append("buy-sell-tax-open")
                if bps > MAX_TAX_BPS:
                    report.reasons.append(
                        f"buy-sell-tax {bps / 100:.2f}% > {MAX_TAX_BPS / 100:.2f}%"
                    )
                elif REJECT_TRANSFER_FEE and bps > 0:
                    report.reasons.append(f"transfer-tax {bps} bps")
            elif ext_type == EXT_PERMANENT_DELEGATE and REJECT_PERMANENT_DELEGATE:
                report.reasons.append("permanent-delegate")
            elif ext_type == EXT_TRANSFER_HOOK and REJECT_TRANSFER_HOOK:
                report.reasons.append("transfer-hook")
            elif ext_type == EXT_NON_TRANSFERABLE and REJECT_NON_TRANSFERABLE:
                report.reasons.append("non-transferable")
            elif ext_type == EXT_PAUSABLE and REJECT_PAUSABLE:
                report.reasons.append("pausable-trading")
            elif ext_type == EXT_DEFAULT_ACCOUNT_STATE and blob and blob[0] == DEFAULT_ACCOUNT_FROZEN:
                report.reasons.append("default-accounts-frozen")

    async def _check_verified(
        self,
        mint: Pubkey,
        report: SecurityReport,
        *,
        uri: str = "",
        existing: bool = False,
    ) -> None:
        if not REJECT_UNVERIFIED:
            return
        pda, _ = Pubkey.find_program_address(
            [b"metadata", bytes(METADATA_PROGRAM), bytes(mint)],
            METADATA_PROGRAM,
        )
        try:
            resp = await self.client.get_account_info(pda)
        except Exception:
            if existing and uri:
                return
            report.reasons.append("unverified-contract")
            return
        if resp.value is None or str(resp.value.owner) != str(METADATA_PROGRAM):
            if existing and uri:
                return
            report.reasons.append("unverified-contract")
            return
        parsed = _parse_metaplex_metadata(_account_bytes(resp.value))
        if parsed is None:
            if existing and uri:
                return
            report.reasons.append("unverified-contract")
            return
        onchain_uri, verified_creator = parsed
        if not (onchain_uri or uri):
            report.reasons.append("unverified-empty-uri")
            return
        if not verified_creator and not existing:
            report.reasons.append("unverified-contract")

    async def _check_holders(
        self,
        mint: Pubkey,
        bonding_curve: Pubkey,
        creator: Pubkey | None,
        report: SecurityReport,
        *,
        existing: bool = False,
    ) -> None:
        lp = associated_bonding_curve(mint, bonding_curve)
        skip = {str(lp), str(bonding_curve)}
        try:
            lp_t22 = get_associated_token_address(bonding_curve, mint, TOKEN_2022)
            skip.add(str(lp_t22))
        except Exception:
            pass
        holders = await self._largest_accounts(mint)

        supply_ui = 0.0
        try:
            supply = await self.client.get_token_supply(mint)
            supply_ui = float(supply.value.ui_amount or 0)
        except Exception:
            supply_ui = 0.0

        worst_tokens = 0.0
        worst_addr = ""
        worst_pct = 0.0
        cluster = 0
        creator_tokens = 0.0
        creator_ata = (
            str(get_associated_token_address(creator, mint, TOKEN_PROGRAM_ID)) if creator else ""
        )
        for item in holders:
            address, ui = _ui_amount(item)
            if address in skip:
                continue
            cluster += 1
            if creator and (address == str(creator) or address == creator_ata):
                creator_tokens += ui
            if ui > worst_tokens:
                worst_tokens = ui
                worst_addr = address
                worst_pct = (ui / supply_ui * 100.0) if supply_ui > 0 else 0.0

        report.top_holder_tokens = worst_tokens
        report.top_holder = worst_addr
        if MAX_INSIDER_TOKENS > 0 and worst_tokens >= MAX_INSIDER_TOKENS:
            report.reasons.append(
                f"insider-whale {worst_tokens:,.0f} tokens (>= {MAX_INSIDER_TOKENS:,.0f})"
            )
        if MAX_HOLDER_PCT > 0 and supply_ui > 0 and worst_pct >= MAX_HOLDER_PCT:
            report.reasons.append(f"concentrated-holder {worst_pct:.1f}% >= {MAX_HOLDER_PCT:.0f}%")
        if BUNDLE_WALLET_COUNT > 0 and not existing and cluster >= BUNDLE_WALLET_COUNT:
            report.reasons.append(f"sniper-bundle {cluster} wallets besides LP")

        if creator is not None:
            if creator_tokens <= 0:
                creator_tokens = await self._creator_balance(creator, mint)
            if supply_ui > 0:
                report.creator_hold_pct = creator_tokens / supply_ui * 100.0
                if report.creator_hold_pct > MAX_CREATOR_HOLD_PCT:
                    report.reasons.append(
                        f"creator-holds {report.creator_hold_pct:.1f}% > {MAX_CREATOR_HOLD_PCT:.0f}%"
                    )

        # getProgramAccounts is disabled on many providers (it returns 0), so the
        # largest-accounts count is the reliable lower bound for the holder count.
        rpc_count = await self._count_holders(mint, skip)
        counted = max(rpc_count or 0, cluster)
        report.holders = counted
        if existing and counted < MIN_HOLDERS:
            logger.info(
                "Existing %s holder count %s below MIN_HOLDERS; not blocking",
                mint,
                counted,
            )
            return
        if MIN_HOLDERS > 0 and counted < MIN_HOLDERS:
            report.reasons.append(f"too-few-holders {counted} < {MIN_HOLDERS}")

    async def _largest_accounts(self, mint: Pubkey, attempts: int = 3) -> list[Any]:
        for attempt in range(attempts):
            try:
                resp = await self.client.get_token_largest_accounts(mint)
                return list(resp.value or [])
            except Exception as exc:
                if "not a token mint" in str(exc).lower():
                    logger.debug("Skip holder read for %s: not a token mint", mint)
                    return []
                if attempt + 1 >= attempts:
                    logger.warning(
                        "get_token_largest_accounts failed for %s: %s", mint, exc
                    )
                else:
                    await asyncio.sleep(0.25 * (attempt + 1))
        return []

    async def _creator_balance(self, creator: Pubkey, mint: Pubkey) -> float:
        ata = get_associated_token_address(creator, mint, TOKEN_PROGRAM_ID)
        try:
            bal = await self.client.get_token_account_balance(ata)
            return float(bal.value.ui_amount or 0)
        except Exception:
            return 0.0

    async def _count_holders(self, mint: Pubkey, skip: set[str]) -> int | None:
        best = 0
        ok = False
        for program in (TOKEN_PROGRAM_ID, TOKEN_2022):
            try:
                resp = await self.client.get_program_accounts(
                    program,
                    encoding="base64",
                    data_slice=DataSliceOpts(offset=32, length=32),
                    filters=[165, MemcmpOpts(offset=0, bytes=str(mint))],
                )
                owners: set[str] = set()
                for item in resp.value or []:
                    data = _account_bytes(item.account)
                    if len(data) < 32:
                        continue
                    owner = str(Pubkey.from_bytes(data[:32]))
                    if str(item.pubkey) in skip or owner in skip:
                        continue
                    owners.add(owner)
                ok = True
                best = max(best, len(owners))
            except Exception as exc:
                logger.debug("holder count RPC failed for %s on %s: %s", mint, program, exc)
        return best if ok else None

    async def _check_pumpswap_lp_burn(
        self,
        mint: Pubkey,
        creator: Pubkey | None,
        report: SecurityReport,
    ) -> None:
        if creator is None:
            report.reasons.append("graduated-missing-creator")
            return
        index = (0).to_bytes(2, "little")
        pool, _ = Pubkey.find_program_address(
            [b"pool", index, bytes(creator), bytes(mint), bytes(WSOL)],
            PUMP_AMM,
        )
        resp = await self.client.get_account_info(pool)
        if resp.value is None:
            report.reasons.append("graduated-lp-pool-missing")
            return
        data = _account_bytes(resp.value)
        if len(data) < 139:
            report.reasons.append("graduated-lp-pool-parse-failed")
            return
        lp_mint = Pubkey.from_bytes(data[107:139])
        try:
            supply = await self.client.get_token_supply(lp_mint)
            total = int(supply.value.amount)
        except Exception:
            report.reasons.append("graduated-lp-supply-unknown")
            return
        if total <= 0:
            report.reasons.append("graduated-lp-supply-zero")
            return
        burned = 0
        try:
            largest = await self.client.get_token_largest_accounts(lp_mint)
            for item in largest.value or []:
                address, ui = _ui_amount(item)
                raw = int(getattr(getattr(item, "amount", None), "amount", 0) or 0)
                if address in BURN_ADDRESSES:
                    burned += raw
        except Exception:
            report.reasons.append("graduated-lp-burn-unknown")
            return
        pct = burned / total * 100.0
        report.lp_locked_pct = pct
        if MIN_LP_LOCKED_PCT > 0 and pct < MIN_LP_LOCKED_PCT:
            report.reasons.append(f"lp-burned {pct:.1f}% < {MIN_LP_LOCKED_PCT:.0f}%")
