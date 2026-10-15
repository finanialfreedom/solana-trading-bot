"""Pump.fun buy/sell executor with dry-run, size limits, and stop-loss."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from solana.rpc.async_api import AsyncClient
from solana.rpc.models import TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from spl.token.instructions import get_associated_token_address

from config import (
    BUY_ON_NEW_TOKEN,
    BUY_ADD_SOL,
    BUY_PROBE_SOL,
    BUY_SOL_AMOUNT,
    CONFIRM_SECONDS,
    COMMITMENT,
    COMPUTE_UNIT_LIMIT,
    DRY_RUN,
    LAMPORTS_PER_SOL,
    MAX_OPEN_POSITIONS,
    MAX_SOL_PER_TRADE,
    MAX_TOTAL_SOL_AT_RISK,
    MAX_TRADES,
    MAX_INSIDER_TOKENS,
    MIN_LIQUIDITY_USD,
    MIN_SOL_RESERVE,
    SECURITY_FILTERS_ENABLED,
    PRIORITY_FEE_MICROLAMPORTS,
    BREAKEVEN_ARM_PCT,
    SLIPPAGE_BPS,
    STOP_LOSS_PCT,
    TAKE_PROFIT_TRIGGER_PCT,
    TRAILING_STOP_PCT,
    TOKEN_PROGRAM_ID,
    live_trading_enabled,
)
from report import REPORT
from security import SecurityScanner
from positions import Position, PositionBook
from trailing import TrailingGuard
from confirm import Confirmer
from pumpfun import (
    BondingCurve,
    CreateInfo,
    GlobalConfig,
    TradeInfo,
    apply_fee,
    associated_bonding_curve,
    build_buy_exact_sol_in,
    build_create_ata,
    build_init_user_volume,
    build_sell,
    parse_bonding_curve,
    parse_global,
    sol_out_for_tokens_in,
    tokens_out_for_sol_in,
    user_volume_pda,
    GLOBAL_PDA,
)
from wallet import get_sol_balance, lamports_to_sol

logger = logging.getLogger("pump-trader")


def _account_data(account: Any) -> bytes:
    data = account.data
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    if isinstance(data, (list, tuple)) and data:
        return bytes(data[0])
    return bytes(data)


def _clamp_buy_lamports(amount: float | None = None) -> int:
    raw = BUY_SOL_AMOUNT if amount is None else amount
    spend = min(raw, MAX_SOL_PER_TRADE)
    if spend <= 0:
        return 0
    return int(spend * LAMPORTS_PER_SOL)


class Trader:
    def __init__(self, client: AsyncClient, wallet: Keypair) -> None:
        self.client = client
        self.wallet = wallet
        self.book = PositionBook(MAX_OPEN_POSITIONS, MAX_TOTAL_SOL_AT_RISK)
        self.global_cfg: GlobalConfig | None = None
        self._volume_ready = False
        self._cap_logged = False
        self._buy_lock = asyncio.Lock()
        self._sell_lock = asyncio.Lock()
        self.security = SecurityScanner(client)
        self.trailing = TrailingGuard(self)
        self.confirm = Confirmer(self)

    async def initialize(self) -> None:
        self.global_cfg = await self._load_global()
        mode = "LIVE" if live_trading_enabled() else "DRY-RUN"
        logger.info(
            "Trader ready mode=%s probe=%.2f add=%.2f SOL stop-loss=%.1f%% "
            "breakeven-after=+%.1f%% trailing-TP arm=%.0f%% callback=%.0f%% "
            "max_open=%s max_trades=%s excel=%s",
            mode,
            BUY_PROBE_SOL if CONFIRM_SECONDS > 0 else BUY_SOL_AMOUNT,
            BUY_ADD_SOL if CONFIRM_SECONDS > 0 else 0.0,
            STOP_LOSS_PCT,
            BREAKEVEN_ARM_PCT,
            TAKE_PROFIT_TRIGGER_PCT,
            TRAILING_STOP_PCT,
            MAX_OPEN_POSITIONS,
            MAX_TRADES,
            REPORT.path,
        )
        if SECURITY_FILTERS_ENABLED:
            liq = (
                f"total coin liquidity >= ${MIN_LIQUIDITY_USD:,.0f}"
                if MIN_LIQUIDITY_USD > 0
                else "no minimum total liquidity"
            )
            logger.info(
                "Security filters: no min liquidity, holders, LP-lock, or verified | %s "
                "| tax fixed and <= 1%%",
                liq,
            )
        if DRY_RUN:
            logger.info("DRY-RUN is on: orders are simulated, no transactions are sent.")
        elif not live_trading_enabled():
            logger.warning(
                "DRY_RUN is false but ENABLE_LIVE_TRADING is not true — staying in simulate-only mode."
            )

    async def _load_global(self) -> GlobalConfig | None:
        try:
            resp = await self.client.get_account_info(GLOBAL_PDA)
            if resp.value is None:
                logger.warning("Pump.fun global account not found")
                return None
            return parse_global(_account_data(resp.value))
        except Exception:
            logger.exception("Failed to load Pump.fun global config")
            return None

    def _fee_bps(self, curve: BondingCurve | None = None) -> int:
        protocol = self.global_cfg.fee_basis_points if self.global_cfg else 100
        creator = 0
        if curve is not None and curve.creator_fee_bps:
            creator = curve.creator_fee_bps
        elif self.global_cfg:
            creator = self.global_cfg.creator_fee_basis_points
        return int(protocol + creator)

    def _fee_recipient(self) -> Pubkey | None:
        return self.global_cfg.fee_recipient if self.global_cfg else None

    async def _account_exists(self, pubkey: Pubkey) -> bool:
        resp = await self.client.get_account_info(pubkey)
        return resp.value is not None

    async def load_curve(self, curve_pk: Pubkey) -> BondingCurve | None:
        resp = await self.client.get_account_info(curve_pk)
        if resp.value is None:
            return None
        try:
            return parse_bonding_curve(_account_data(resp.value))
        except Exception:
            logger.exception("Failed to parse bonding curve %s", curve_pk)
            return None

    async def load_curves(self, keys: list[Pubkey]) -> dict[str, BondingCurve | None]:
        if not keys:
            return {}
        try:
            resp = await self.client.get_multiple_accounts(keys)
        except Exception as exc:
            logger.warning("Batch curve refresh failed: %s", exc)
            return {}
        out: dict[str, BondingCurve | None] = {}
        for pk, acc in zip(keys, resp.value or [], strict=False):
            if acc is None:
                out[str(pk)] = None
                continue
            try:
                out[str(pk)] = parse_bonding_curve(_account_data(acc))
            except Exception:
                logger.exception("Failed to parse bonding curve %s", pk)
                out[str(pk)] = None
        return out

    def _open_position(self, mint: object) -> Position | None:
        position = self.book.get(mint)
        if position is None or self.trailing.is_exiting(position.mint):
            return None
        return position

    async def scale_in(self, position: Position, info: CreateInfo, *, spend_sol: float) -> None:
        spend = _clamp_buy_lamports(spend_sol)
        if spend <= 0:
            return
        live_pos = self._open_position(position.mint)
        if live_pos is None:
            logger.info("Skip add %s: already closed or exiting", position.symbol)
            return
        blocked = self.book.open_reason(spend, adding=True)
        if blocked:
            logger.info("Skip add %s: %s", position.symbol, blocked)
            return
        curve = await self.load_curve(live_pos.bonding_curve)
        live_pos = self._open_position(position.mint)
        if live_pos is None:
            logger.info("Skip add %s: sold before scale-in", position.symbol)
            return
        if curve is None or curve.complete:
            return
        tokens_out = tokens_out_for_sol_in(curve, spend, self._fee_bps(curve))
        if tokens_out <= 0:
            return
        if live_trading_enabled():
            if self._open_position(position.mint) is None:
                logger.info("Skip add %s: sold before scale-in send", position.symbol)
                return
            min_tokens = tokens_out * (10_000 - SLIPPAGE_BPS) // 10_000
            sent = await self._send_buy(info, curve, spend, min_tokens)
            if not sent:
                return
            onchain = await self.load_curve(live_pos.bonding_curve)
            if onchain:
                curve = onchain
        async with self._sell_lock:
            live_pos = self._open_position(position.mint)
            if live_pos is None:
                logger.info("Skip add %s: sold before scale-in applied", position.symbol)
                return
            old_tokens = live_pos.token_amount
            entry = curve.price
            total = old_tokens + tokens_out
            if total > 0:
                live_pos.entry_price = (
                    live_pos.entry_price * old_tokens + entry * tokens_out
                ) / total
            live_pos.token_amount = total
            live_pos.sol_spent += spend
            live_pos.scaled = True
            live_pos.update_reserves(
                curve.virtual_quote_reserves + spend,
                max(0, curve.virtual_token_reserves - tokens_out),
            )
            extra = spend / LAMPORTS_PER_SOL
            REPORT.record_add(mint=str(live_pos.mint), extra_sol=extra)
            logger.info(
                "ADD %s  +%.4f SOL  total=%.4f SOL  mint=%s",
                live_pos.symbol,
                extra,
                live_pos.sol_spent / LAMPORTS_PER_SOL,
                live_pos.mint,
            )

    def _curve_from_create(self, info: CreateInfo) -> BondingCurve:
        return BondingCurve(
            virtual_token_reserves=info.virtual_token_reserves,
            virtual_quote_reserves=info.virtual_quote_reserves,
            real_token_reserves=info.virtual_token_reserves,
            real_quote_reserves=0,
            token_total_supply=info.virtual_token_reserves,
            complete=False,
            creator=info.creator,
        )

    async def on_new_token(self, info: CreateInfo, signature: str) -> None:
        if not BUY_ON_NEW_TOKEN:
            return
        if CONFIRM_SECONDS > 0:
            self.confirm.watch(info)
            return
        await self._consider_buy(info, signature, existing=False)

    async def buy_confirmed(self, info: CreateInfo, *, spend_sol: float, tag: str) -> None:
        await self._consider_buy(info, tag, existing=False, spend_sol=spend_sol)

    async def on_existing_token(self, info: CreateInfo) -> None:
        await self._consider_buy(info, "existing-scan", existing=True)

    async def _consider_buy(
        self,
        info: CreateInfo,
        signature: str,
        *,
        existing: bool,
        spend_sol: float | None = None,
    ) -> None:
        if REPORT.opened_count() >= MAX_TRADES:
            if not self._cap_logged:
                logger.info("Reached %s-trade cap. Excel report: %s", MAX_TRADES, REPORT.path)
                self._cap_logged = True
            return
        if not self.book.mark_pending(info.mint):
            logger.debug("Skip %s: already held or in-flight", info.mint)
            return
        try:
            async with self._buy_lock:
                await self._buy(info, signature, existing=existing, spend_sol=spend_sol)
        finally:
            self.book.clear_pending(info.mint)

    async def _buy(
        self,
        info: CreateInfo,
        signature: str,
        *,
        existing: bool = False,
        spend_sol: float | None = None,
    ) -> None:
        if REPORT.opened_count() >= MAX_TRADES:
            logger.info("Skip %s: reached %s-trade session cap", info.symbol, MAX_TRADES)
            return
        spend = _clamp_buy_lamports(spend_sol)
        if spend <= 0:
            logger.info("Skip buy: BUY_SOL_AMOUNT/MAX_SOL_PER_TRADE is 0")
            return
        if spend < int(BUY_SOL_AMOUNT * LAMPORTS_PER_SOL):
            logger.info(
                "Clamped buy from %.4f to %.4f SOL (MAX_SOL_PER_TRADE)",
                BUY_SOL_AMOUNT,
                spend / LAMPORTS_PER_SOL,
            )

        blocked = self.book.open_reason(spend)
        if blocked:
            logger.info("Skip %s (%s): %s", info.symbol, info.mint, blocked)
            return

        curve = await self.load_curve(info.bonding_curve)
        if curve is None:
            curve = self._curve_from_create(info)
        if curve.complete:
            logger.info("Skip %s: bonding curve already complete", info.symbol)
            return

        scan = await self.security.review(
            mint=info.mint,
            curve=curve,
            bonding_curve=info.bonding_curve,
            creator=curve.creator or info.creator,
            name=info.name,
            symbol=info.symbol,
            uri=info.uri,
            fee_bps=self._fee_bps(curve),
            initial_real_token_reserves=(
                self.global_cfg.initial_real_token_reserves
                if self.global_cfg
                else 793_100_000_000_000
            ),
            existing=existing,
        )
        if not scan.ok:
            logger.info("SECURITY BLOCK %s (%s)  %s", info.symbol, info.mint, scan.summary())
            REPORT.record_block(
                name=info.name,
                symbol=info.symbol,
                mint=str(info.mint),
                reasons=scan.summary(),
                liquidity_usd=scan.liquidity_usd,
                top_holder_tokens=scan.top_holder_tokens,
                lp_locked_pct=scan.lp_locked_pct,
            )
            return

        fee_bps = self._fee_bps(curve)
        tokens_out = tokens_out_for_sol_in(curve, spend, fee_bps)
        if tokens_out <= 0:
            logger.info("Skip %s: quoted token amount is 0", info.symbol)
            return

        min_tokens = tokens_out * (10_000 - SLIPPAGE_BPS) // 10_000
        entry_price = curve.price
        live = live_trading_enabled()
        sig = "dry-run"

        if live:
            sig = await self._send_buy(info, curve, spend, min_tokens)
            if not sig:
                return
            onchain = await self.load_curve(info.bonding_curve)
            if onchain:
                curve = onchain
        else:
            logger.info(
                "DRY-RUN BUY  %s (%s)  spend=%.4f SOL  tokens=%s  "
                "entry_price=%.10e  create_sig=%s",
                info.symbol,
                info.mint,
                spend / LAMPORTS_PER_SOL,
                tokens_out,
                entry_price,
                signature,
            )

        position = Position(
            mint=info.mint,
            bonding_curve=info.bonding_curve,
            associated_bonding_curve=associated_bonding_curve(info.mint, info.bonding_curve),
            creator=curve.creator or info.creator,
            name=info.name,
            symbol=info.symbol,
            token_amount=tokens_out,
            sol_spent=spend,
            entry_price=entry_price,
            virtual_token_reserves=curve.virtual_token_reserves - tokens_out,
            virtual_quote_reserves=curve.virtual_quote_reserves + spend,
            dry_run=not live,
        )
        self.book.add(position)
        invested = spend / LAMPORTS_PER_SOL
        REPORT.record_open(
            name=info.name,
            symbol=info.symbol,
            mint=str(info.mint),
            invested_sol=invested,
        )
        logger.info(
            "BUY %s  %s/%s  invested=%.4f SOL  mint=%s  %s",
            info.symbol,
            REPORT.opened_count(),
            MAX_TRADES,
            invested,
            info.mint,
            scan.lp_lines(),
        )

    async def _send_buy(
        self,
        info: CreateInfo,
        curve: BondingCurve,
        spend: int,
        min_tokens: int,
    ) -> str | None:
        owner = self.wallet.pubkey()
        sol = await get_sol_balance(self.client, owner)
        reserve = int(MIN_SOL_RESERVE * LAMPORTS_PER_SOL)
        if sol < spend + reserve:
            logger.warning(
                "Skip live buy %s: wallet %.4f SOL is below spend+reserve %.4f",
                info.symbol,
                lamports_to_sol(sol),
                lamports_to_sol(spend + reserve),
            )
            return None

        creator = curve.creator or info.creator
        fee_recipient = self._fee_recipient()
        if creator is None or fee_recipient is None:
            logger.warning("Skip live buy %s: missing creator or fee recipient", info.symbol)
            return None

        user_ata = get_associated_token_address(owner, info.mint, TOKEN_PROGRAM_ID)
        ixs: list[Any] = [
            set_compute_unit_limit(COMPUTE_UNIT_LIMIT),
            set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
        ]
        if not await self._account_exists(user_ata):
            ixs.append(build_create_ata(owner, owner, info.mint))
        if not self._volume_ready and not await self._account_exists(user_volume_pda(owner)):
            ixs.append(build_init_user_volume(owner, owner))
            self._volume_ready = True
        else:
            self._volume_ready = True

        ixs.append(
            build_buy_exact_sol_in(
                mint=info.mint,
                bonding_curve=info.bonding_curve,
                associated_curve=associated_bonding_curve(info.mint, info.bonding_curve),
                associated_user=user_ata,
                user=owner,
                fee_recipient=fee_recipient,
                creator=creator,
                spendable_sol=spend,
                min_tokens_out=min_tokens,
            )
        )
        return await self._send(ixs)

    async def on_trade_event(self, trade: TradeInfo) -> None:
        self.trailing.observe_trade(trade)

    async def poll_open_positions(self) -> None:
        await self.trailing.tick()

    async def sell_position(self, position: Position, reason: str) -> None:
        await self._sell(position, reason)

    def _quote_exit(self, position: Position, token_amount: int) -> int:
        """Lamports expected from selling token_amount, on the curve or the AMM."""
        if position.graduated and position.amm_price > 0:
            # Post-graduation the bonding curve is closed; value against the AMM mark.
            gross = int(token_amount * position.amm_price)
            return max(0, apply_fee(gross, self._fee_bps()))
        curve = BondingCurve(
            virtual_token_reserves=position.virtual_token_reserves,
            virtual_quote_reserves=position.virtual_quote_reserves,
            real_token_reserves=0,
            real_quote_reserves=0,
            token_total_supply=0,
            complete=False,
            creator=position.creator,
        )
        return sol_out_for_tokens_in(curve, token_amount, self._fee_bps(curve))

    def reserve_scale_out(self, position: Position, token_amount: int) -> int:
        """Reserve tokens for a partial sale.

        Called synchronously before the (async) sale so that two milestones crossed
        in the same tick size off the remaining amount instead of both sizing off
        the original position.
        """
        held = position.tokens_held
        tokens = min(int(token_amount), held)
        if tokens <= 0:
            return 0
        if position.remaining_tokens < 0:
            position.remaining_tokens = held
        position.remaining_tokens -= tokens
        return tokens

    async def scale_out(self, position: Position, token_amount: int, reason: str) -> None:
        """Sell already-reserved tokens and bank the proceeds; the rest stays open."""
        if token_amount <= 0:
            return
        sol_out = self._quote_exit(position, token_amount)
        live = live_trading_enabled() and not position.dry_run
        if live:
            sent = await self._send_sell(position, sol_out, token_amount=token_amount)
            if not sent:
                # Nothing was sold, so hand the reserved tokens back to the position.
                if position.remaining_tokens >= 0:
                    position.remaining_tokens += token_amount
                return
        async with self._sell_lock:
            if self.book.get(position.mint) is None:
                return
            position.realized_sol += sol_out
            left = position.tokens_held
            if left <= 0:
                # The ladder sold the whole position: book it as a completed trade
                # instead of leaving a zero-token position occupying a slot.
                pnl = (
                    (position.realized_sol - position.sol_spent) / position.sol_spent * 100.0
                    if position.sol_spent > 0
                    else 0.0
                )
                result = REPORT.record_close(
                    mint=str(position.mint),
                    pnl_pct=pnl,
                    exit_sol=position.realized_sol / LAMPORTS_PER_SOL,
                    reason=f"scale-out complete ({reason})",
                    mfe_pct=position.mfe_pct,
                    mae_pct=position.mae_pct,
                )
                self.book.close(position.mint)
                logger.info(
                    "%s  %s  %+0.1f%% (fully scaled out)  in=%.4f SOL  out=%.4f SOL  mint=%s",
                    result,
                    position.symbol,
                    pnl,
                    position.sol_spent / LAMPORTS_PER_SOL,
                    position.realized_sol / LAMPORTS_PER_SOL,
                    position.mint,
                )
                return
        logger.info(
            "SCALE-OUT  %s  sold=%s tokens  got=%.4f SOL  left=%s (%.0f%%)  %s  mint=%s",
            position.symbol,
            token_amount,
            sol_out / LAMPORTS_PER_SOL,
            left,
            100.0 * left / max(1, position.token_amount),
            reason,
            position.mint,
        )

    async def _sell(self, position: Position, reason: str) -> None:
        async with self._sell_lock:
            if self.book.get(position.mint) is None:
                return
        held = position.tokens_held
        sol_out = self._quote_exit(position, held)
        total_out = sol_out + position.realized_sol
        pnl = (
            (total_out - position.sol_spent) / position.sol_spent * 100.0
            if position.sol_spent > 0
            else 0.0
        )
        live = live_trading_enabled() and not position.dry_run
        if live:
            sent = await self._send_sell(position, sol_out, token_amount=held)
            if not sent:
                return
        exit_sol = total_out / LAMPORTS_PER_SOL
        async with self._sell_lock:
            if self.book.get(position.mint) is None:
                return
            result = REPORT.record_close(
                mint=str(position.mint),
                pnl_pct=pnl,
                exit_sol=exit_sol,
                reason=reason,
                mfe_pct=position.mfe_pct,
                mae_pct=position.mae_pct,
            )
            self.book.close(position.mint)
        if pnl <= -80:
            self.security.note_rug(position.creator)
        logger.info(
            "%s  %s  %+0.1f%%  in=%.4f SOL  out=%.4f SOL  mint=%s  %s",
            result,
            position.symbol,
            pnl,
            position.sol_spent / LAMPORTS_PER_SOL,
            exit_sol,
            position.mint,
            reason,
        )

    async def _send_sell(
        self,
        position: Position,
        quoted_sol: int,
        *,
        token_amount: int | None = None,
    ) -> str | None:
        creator = position.creator
        fee_recipient = self._fee_recipient()
        if creator is None or fee_recipient is None:
            logger.warning("Skip live sell %s: missing creator or fee recipient", position.symbol)
            return None
        if position.graduated:
            logger.warning(
                "Live sell of graduated %s uses the bonding-curve program, which no longer "
                "holds liquidity; an AMM (PumpSwap) swap path is still required for live exits.",
                position.symbol,
            )
        owner = self.wallet.pubkey()
        sell_tokens = position.tokens_held if token_amount is None else token_amount
        min_sol = quoted_sol * (10_000 - SLIPPAGE_BPS) // 10_000
        user_ata = get_associated_token_address(owner, position.mint, TOKEN_PROGRAM_ID)
        ixs = [
            set_compute_unit_limit(COMPUTE_UNIT_LIMIT),
            set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
            build_sell(
                mint=position.mint,
                bonding_curve=position.bonding_curve,
                associated_curve=position.associated_bonding_curve,
                associated_user=user_ata,
                user=owner,
                fee_recipient=fee_recipient,
                creator=creator,
                token_amount=sell_tokens,
                min_sol_output=min_sol,
            ),
        ]
        return await self._send(ixs)

    async def _send(self, instructions: list[Any]) -> str | None:
        try:
            blockhash = (await self.client.get_latest_blockhash()).value.blockhash
            msg = MessageV0.try_compile(
                payer=self.wallet.pubkey(),
                instructions=instructions,
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            tx = VersionedTransaction(msg, [self.wallet])
            resp = await self.client.send_transaction(
                tx,
                opts=TxOpts(skip_preflight=True, skip_confirmation=True),
            )
            return str(resp.value)
        except Exception:
            logger.exception("Transaction send failed")
            return None
