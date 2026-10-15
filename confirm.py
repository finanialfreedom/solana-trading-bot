"""Wait for real buyers and MC expansion before paper-buying a new launch."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from time import time
from typing import TYPE_CHECKING

from config import (
    BUY_ADD_SOL,
    BUY_PROBE_SOL,
    ADD_AFTER_SECONDS,
    CONFIRM_EXPIRE_SECONDS,
    CONFIRM_SECONDS,
    CONFIRM_TICK_SECONDS,
    LAMPORTS_PER_SOL,
    MAX_SOL_PER_TRADE,
    MIN_ADD_MC_USD,
    MIN_ADD_UNIQUE_BUYERS,
    MIN_CONFIRM_BUY_SOL,
    MIN_CONFIRM_MC_USD,
    MIN_CONFIRM_NET_SOL,
    MAX_CONFIRM_SELL_RATIO,
    MIN_UNIQUE_BUYERS,
    PUMP_TOKEN_SUPPLY,
    SOL_USD_FALLBACK,
)
from pumpfun import CreateInfo, TradeInfo

if TYPE_CHECKING:
    from trader import Trader

logger = logging.getLogger("pump-confirm")


def market_cap_usd(virtual_quote: int, virtual_token: int, sol_usd: float) -> float:
    if virtual_token <= 0 or sol_usd <= 0:
        return 0.0
    price = virtual_quote / virtual_token
    mc_sol = price * PUMP_TOKEN_SUPPLY / LAMPORTS_PER_SOL
    return mc_sol * sol_usd


@dataclass(slots=True)
class Candidate:
    info: CreateInfo
    seen_at: float = field(default_factory=time)
    buyers: set[str] = field(default_factory=set)
    buy_sol: int = 0
    sell_sol: int = 0
    last_quote: int = 0
    last_token: int = 0
    trades: int = 0
    probed: bool = False
    scaled: bool = False
    decided: bool = False

    @property
    def unique_buyers(self) -> int:
        return len(self.buyers)

    @property
    def net_buy_sol(self) -> int:
        """Lamports bought minus lamports sold during the confirm window.

        Gross buys alone are easily produced by the deployer and a couple of bots;
        what separates a real launch from a dump is whether demand survives the
        selling that follows it.
        """
        return self.buy_sol - self.sell_sol

    @property
    def sell_ratio(self) -> float:
        if self.buy_sol <= 0:
            return 0.0
        return self.sell_sol / self.buy_sol

    def mc_usd(self, sol_usd: float) -> float:
        return market_cap_usd(self.last_quote, self.last_token, sol_usd)


class Confirmer:
    def __init__(self, trader: Trader) -> None:
        self.trader = trader
        self._cands: dict[str, Candidate] = {}
        self._sol_usd = SOL_USD_FALLBACK
        self._sol_usd_ts = 0.0

    def watch(self, info: CreateInfo) -> None:
        key = str(info.mint)
        if key in self._cands or self.trader.book.get(key) is not None:
            return
        cand = Candidate(
            info=info,
            last_quote=info.virtual_quote_reserves,
            last_token=info.virtual_token_reserves,
        )
        self._cands[key] = cand
        logger.info(
            "WATCH  %s  mint=%s  probe=%.2f after %.0fs  add=%.2f after %.0fs if mc>=$%.0f",
            info.symbol,
            info.mint,
            BUY_PROBE_SOL,
            CONFIRM_SECONDS,
            BUY_ADD_SOL,
            ADD_AFTER_SECONDS,
            MIN_ADD_MC_USD,
        )

    def observe_trade(self, trade: TradeInfo) -> None:
        cand = self._cands.get(str(trade.mint))
        if cand is None:
            return
        cand.trades += 1
        cand.last_quote = trade.virtual_quote_reserves
        cand.last_token = trade.virtual_token_reserves
        if trade.is_buy:
            cand.buy_sol += trade.sol_amount
            if trade.user is not None:
                user = str(trade.user)
                creator = str(cand.info.creator) if cand.info.creator else ""
                if user and user != creator:
                    cand.buyers.add(user)
        else:
            cand.sell_sol += trade.sol_amount

    async def run(self, stop: asyncio.Event) -> None:
        logger.info(
            "Confirm window started  probe=%.2f after %.0fs  "
            "add=%.2f after %.0fs if mc>=$%.0f",
            BUY_PROBE_SOL,
            CONFIRM_SECONDS,
            BUY_ADD_SOL,
            ADD_AFTER_SECONDS,
            MIN_ADD_MC_USD,
        )
        try:
            while not stop.is_set():
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Confirm tick failed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=CONFIRM_TICK_SECONDS)
                except asyncio.TimeoutError:
                    continue
        finally:
            logger.info("Confirm window stopped")

    async def tick(self) -> None:
        if CONFIRM_SECONDS <= 0:
            return
        now = time()
        if now - self._sol_usd_ts >= 30:
            try:
                self._sol_usd = await self.trader.security.sol_usd()
            except Exception:
                pass
            self._sol_usd_ts = now
        sol_usd = self._sol_usd
        for key, cand in list(self._cands.items()):
            age = now - cand.seen_at
            if not cand.probed and not cand.decided and age >= CONFIRM_SECONDS:
                await self._maybe_probe(cand, sol_usd)
            if cand.probed and not cand.scaled:
                await self._maybe_add(cand, sol_usd)
            if not cand.probed and cand.decided and age >= CONFIRM_EXPIRE_SECONDS:
                self._cands.pop(key, None)
            elif cand.probed and age >= 180:
                self._cands.pop(key, None)
            elif not cand.probed and not cand.decided and age >= CONFIRM_EXPIRE_SECONDS:
                self._skip(cand, "confirm-timeout", sol_usd)
                self._cands.pop(key, None)
        if len(self._cands) > 80:
            oldest = sorted(self._cands.items(), key=lambda item: item[1].seen_at)
            for key, _cand in oldest[: len(self._cands) - 80]:
                self._cands.pop(key, None)

    async def _maybe_probe(self, cand: Candidate, sol_usd: float) -> None:
        reason = self._probe_block_reason(cand, sol_usd)
        if reason:
            self._skip(cand, reason, sol_usd)
            return
        spend = min(BUY_PROBE_SOL, MAX_SOL_PER_TRADE)
        logger.info(
            "CONFIRM  %s  buyers=%s  buy_sol=%.2f  mc=$%.0f  probe=%.2f  mint=%s",
            cand.info.symbol,
            cand.unique_buyers,
            cand.buy_sol / LAMPORTS_PER_SOL,
            cand.mc_usd(sol_usd),
            spend,
            cand.info.mint,
        )
        cand.probed = True
        await self.trader.buy_confirmed(cand.info, spend_sol=spend, tag="probe")

    async def _maybe_add(self, cand: Candidate, sol_usd: float) -> None:
        if time() - cand.seen_at < ADD_AFTER_SECONDS:
            return
        position = self.trader._open_position(cand.info.mint)
        if position is None or position.scaled:
            cand.scaled = True
            return
        if MIN_ADD_UNIQUE_BUYERS > 0 and cand.unique_buyers < MIN_ADD_UNIQUE_BUYERS:
            return
        if MIN_ADD_MC_USD > 0 and cand.mc_usd(sol_usd) < MIN_ADD_MC_USD:
            return
        spend = min(BUY_ADD_SOL, max(0.0, MAX_SOL_PER_TRADE - position.sol_spent / LAMPORTS_PER_SOL))
        if spend <= 0:
            cand.scaled = True
            return
        logger.info(
            "SCALE-IN  %s  buyers=%s  mc=$%.0f  add=%.2f  mint=%s",
            cand.info.symbol,
            cand.unique_buyers,
            cand.mc_usd(sol_usd),
            spend,
            cand.info.mint,
        )
        cand.scaled = True
        await self.trader.scale_in(position, cand.info, spend_sol=spend)

    def _probe_block_reason(self, cand: Candidate, sol_usd: float) -> str | None:
        if MIN_UNIQUE_BUYERS > 0 and cand.unique_buyers < MIN_UNIQUE_BUYERS:
            return f"few-buyers {cand.unique_buyers} < {MIN_UNIQUE_BUYERS}"
        if (
            MIN_CONFIRM_BUY_SOL > 0
            and cand.buy_sol < int(MIN_CONFIRM_BUY_SOL * LAMPORTS_PER_SOL)
        ):
            return (
                f"low-buy-sol {cand.buy_sol / LAMPORTS_PER_SOL:.2f} "
                f"< {MIN_CONFIRM_BUY_SOL:.1f}"
            )
        if (
            MIN_CONFIRM_NET_SOL > 0
            and cand.net_buy_sol < int(MIN_CONFIRM_NET_SOL * LAMPORTS_PER_SOL)
        ):
            return (
                f"low-net-buy {cand.net_buy_sol / LAMPORTS_PER_SOL:+.2f} "
                f"(bought {cand.buy_sol / LAMPORTS_PER_SOL:.2f}, "
                f"sold {cand.sell_sol / LAMPORTS_PER_SOL:.2f}) "
                f"< {MIN_CONFIRM_NET_SOL:.1f}"
            )
        if (
            MAX_CONFIRM_SELL_RATIO > 0
            and cand.buy_sol > 0
            and cand.sell_ratio > MAX_CONFIRM_SELL_RATIO
        ):
            return (
                f"sell-pressure {cand.sell_ratio:.2f}x "
                f"(sold {cand.sell_sol / LAMPORTS_PER_SOL:.2f} vs "
                f"bought {cand.buy_sol / LAMPORTS_PER_SOL:.2f}) "
                f"> {MAX_CONFIRM_SELL_RATIO:.2f}x"
            )
        mc = cand.mc_usd(sol_usd)
        if MIN_CONFIRM_MC_USD > 0 and mc < MIN_CONFIRM_MC_USD:
            return f"low-mc ${mc:,.0f} < ${MIN_CONFIRM_MC_USD:,.0f}"
        return None

    def _skip(self, cand: Candidate, reason: str, sol_usd: float) -> None:
        cand.decided = True
        logger.info(
            "SKIP CONFIRM  %s  %s  buyers=%s  buy_sol=%.2f  mc=$%.0f  mint=%s",
            cand.info.symbol,
            reason,
            cand.unique_buyers,
            cand.buy_sol / LAMPORTS_PER_SOL,
            cand.mc_usd(sol_usd),
            cand.info.mint,
        )
