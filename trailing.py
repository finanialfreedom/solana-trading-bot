"""Background trailing take-profit and stop-loss watchdog.

Runs as its own asyncio task so peak tracking and callback math never block
the WebSocket buy loop. Exit sends are also spawned off this loop.
"""

from __future__ import annotations

import asyncio
import logging
from time import time
from typing import TYPE_CHECKING, Any

from config import (
    BREAKEVEN_ARM_PCT,
    DEAD_COIN_MIN_PNL_PCT,
    DEAD_COIN_SECONDS,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    TAKE_PROFIT_TRIGGER_PCT,
    TRAILING_ARMED_TICK_SECONDS,
    TRAILING_LOCK_BREAKEVEN,
    TRAILING_STALE_SECONDS,
    TRAILING_STOP_PCT,
    TRAILING_TICK_SECONDS,
)
from positions import Position
from pumpfun import BondingCurve, TradeInfo

if TYPE_CHECKING:
    from trader import Trader

logger = logging.getLogger("pump-trailing")


class TrailingGuard:
    def __init__(self, trader: Trader) -> None:
        self.trader = trader
        self._exiting: set[str] = set()
        self._exit_tasks: set[asyncio.Task[Any]] = set()

    def is_exiting(self, mint: object) -> bool:
        return str(mint) in self._exiting

    def observe_trade(self, trade: TradeInfo) -> None:
        """Apply a live trade mark without awaiting RPC or a sell."""
        position = self.trader.book.get(trade.mint)
        if position is None or self.is_exiting(position.mint):
            return
        position.update_reserves(trade.virtual_quote_reserves, trade.virtual_token_reserves)
        self._after_mark(position, source="trade-event")

    async def run(self, stop: asyncio.Event) -> None:
        logger.info(
            "Trailing TP/SL watchdog started  tick=%.2fs armed=%.2fs stale=%.2fs  "
            "SL floor=%.1f%%  breakeven after +%.1f%%  arm=%.0f%%  callback=%.0f%%",
            TRAILING_TICK_SECONDS,
            TRAILING_ARMED_TICK_SECONDS,
            TRAILING_STALE_SECONDS,
            STOP_LOSS_PCT,
            BREAKEVEN_ARM_PCT,
            TAKE_PROFIT_TRIGGER_PCT,
            TRAILING_STOP_PCT,
        )
        try:
            while not stop.is_set():
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Trailing watchdog tick failed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._tick_seconds())
                except asyncio.TimeoutError:
                    continue
        finally:
            pending = list(self._exit_tasks)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            logger.info("Trailing TP/SL watchdog stopped")

    async def tick(self) -> None:
        positions = [p for p in self.trader.book.all() if not self.is_exiting(p.mint)]
        if not positions:
            return
        now = time()
        stale = [p for p in positions if now - p.last_mark_at >= TRAILING_STALE_SECONDS]
        for position in positions:
            if position not in stale:
                self._after_mark(position, source="watchdog")
        if not stale:
            return
        curves = await self.trader.load_curves([p.bonding_curve for p in stale])
        if not curves and stale:
            await asyncio.sleep(1.0)
            return
        for position in stale:
            if self.trader.book.get(position.mint) is None or self.is_exiting(position.mint):
                continue
            curve = curves.get(str(position.bonding_curve))
            if curve is None:
                self._after_mark(position, source="watchdog")
                continue
            position.update_reserves(curve.virtual_quote_reserves, curve.virtual_token_reserves)
            self._after_mark(position, source="watchdog", complete=curve.complete)

    def _tick_seconds(self) -> float:
        if any(p.trailing_armed for p in self.trader.book.all()):
            return TRAILING_ARMED_TICK_SECONDS
        return TRAILING_TICK_SECONDS

    def _after_mark(
        self,
        position: Position,
        *,
        source: str,
        complete: bool = False,
    ) -> None:
        if complete:
            self.schedule_exit(position, "curve-complete")
            return
        if position.lock_breakeven():
            logger.info(
                "SL TO BREAKEVEN  %s  at %+0.1f%%  floor=entry  mint=%s",
                position.symbol,
                position.pnl_pct,
                position.mint,
            )
        if position.track_peak():
            logger.info(
                "TRAILING ON  %s  armed at %+0.1f%%  peak=%.10e  callback=%.0f%%  mint=%s",
                position.symbol,
                position.pnl_pct,
                position.peak_price,
                TRAILING_STOP_PCT,
                position.mint,
            )
        reason = self.decide_exit(position, source)
        if reason:
            self.schedule_exit(position, reason)

    @staticmethod
    def decide_exit(position: Position, source: str) -> str | None:
        pnl = position.pnl_pct
        if position.mark_price <= 0 or position.virtual_quote_reserves <= 0:
            return f"{source} emergency-flat {pnl:.1f}%"
        if position.breakeven_locked and pnl <= 0:
            return f"{source} breakeven {pnl:.1f}%"
        if pnl <= -STOP_LOSS_PCT:
            return f"{source} stop-loss {pnl:.1f}%"
        if (
            DEAD_COIN_SECONDS > 0
            and not position.trailing_armed
            and (time() - position.opened_at) >= DEAD_COIN_SECONDS
            and pnl < DEAD_COIN_MIN_PNL_PCT
        ):
            return (
                f"{source} dead-coin {pnl:.1f}% "
                f"after {DEAD_COIN_SECONDS:.0f}s"
            )
        if position.trailing_armed:
            if TRAILING_LOCK_BREAKEVEN and pnl <= 0:
                return f"{source} breakeven {pnl:.1f}%"
            if TRAILING_STOP_PCT > 0 and position.peak_price > 0:
                drop = position.drop_from_peak_pct
                if drop >= TRAILING_STOP_PCT:
                    return (
                        f"{source} trailing-stop {pnl:.1f}% "
                        f"(-{drop:.1f}% from peak)"
                    )
            return None
        if TRAILING_STOP_PCT <= 0 and TAKE_PROFIT_PCT > 0 and pnl >= TAKE_PROFIT_PCT:
            return f"{source} take-profit {pnl:.1f}%"
        return None

    def schedule_exit(self, position: Position, reason: str) -> None:
        key = str(position.mint)
        if key in self._exiting or self.trader.book.get(key) is None:
            return
        self._exiting.add(key)
        task = asyncio.create_task(
            self._run_exit(position, reason),
            name=f"exit-{position.symbol}-{key[:8]}",
        )
        self._exit_tasks.add(task)
        task.add_done_callback(self._exit_done)

    async def _run_exit(self, position: Position, reason: str) -> None:
        try:
            await self.trader.sell_position(position, reason)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Exit failed for %s", position.symbol)
        finally:
            self._exiting.discard(str(position.mint))

    def _exit_done(self, task: asyncio.Task[Any]) -> None:
        self._exit_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.exception("Exit task %s crashed", task.get_name(), exc_info=exc)
