"""In-memory open positions and risk limits."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time

from solders.pubkey import Pubkey

from config import BREAKEVEN_ARM_PCT, LAMPORTS_PER_SOL, TAKE_PROFIT_TRIGGER_PCT


@dataclass(slots=True)
class Position:
    mint: Pubkey
    bonding_curve: Pubkey
    associated_bonding_curve: Pubkey
    creator: Pubkey | None
    name: str
    symbol: str
    token_amount: int
    sol_spent: int
    entry_price: float
    virtual_token_reserves: int
    virtual_quote_reserves: int
    opened_at: float = field(default_factory=time)
    dry_run: bool = True
    trailing_armed: bool = False
    breakeven_locked: bool = False
    peak_price: float = 0.0
    last_mark_at: float = field(default_factory=time)
    scaled: bool = False
    # Best/worst PnL seen while the position was open (max favorable / adverse excursion).
    mfe_pct: float = 0.0
    mae_pct: float = 0.0

    @property
    def mark_price(self) -> float:
        if self.virtual_token_reserves <= 0:
            return 0.0
        return self.virtual_quote_reserves / self.virtual_token_reserves

    @property
    def pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (self.mark_price - self.entry_price) / self.entry_price * 100.0

    @property
    def drop_from_peak_pct(self) -> float:
        if self.peak_price <= 0:
            return 0.0
        return (self.peak_price - self.mark_price) / self.peak_price * 100.0

    def update_reserves(self, virtual_quote: int, virtual_token: int) -> None:
        self.virtual_quote_reserves = virtual_quote
        self.virtual_token_reserves = virtual_token
        self.last_mark_at = time()
        pnl = self.pnl_pct
        if pnl > self.mfe_pct:
            self.mfe_pct = pnl
        if pnl < self.mae_pct:
            self.mae_pct = pnl

    def lock_breakeven(self) -> bool:
        """Move the stop to entry once price has risen BREAKEVEN_ARM_PCT."""
        if BREAKEVEN_ARM_PCT <= 0 or self.breakeven_locked:
            return False
        if self.pnl_pct < BREAKEVEN_ARM_PCT:
            return False
        self.breakeven_locked = True
        return True

    def track_peak(self) -> bool:
        """Arm trailing TP and raise the peak. Returns True when newly armed."""
        price = self.mark_price
        newly_armed = False
        if (
            TAKE_PROFIT_TRIGGER_PCT > 0
            and not self.trailing_armed
            and self.pnl_pct >= TAKE_PROFIT_TRIGGER_PCT
        ):
            self.trailing_armed = True
            self.peak_price = price
            newly_armed = True
        elif self.trailing_armed and price > self.peak_price:
            self.peak_price = price
        return newly_armed


class PositionBook:
    def __init__(self, max_open: int, max_total_sol: float) -> None:
        self.max_open = max_open
        self.max_total_lamports = int(max_total_sol * LAMPORTS_PER_SOL)
        self._positions: dict[str, Position] = {}
        self._pending: set[str] = set()

    def __len__(self) -> int:
        return len(self._positions)

    def get(self, mint: Pubkey | str) -> Position | None:
        return self._positions.get(str(mint))

    def all(self) -> list[Position]:
        return list(self._positions.values())

    def mark_pending(self, mint: Pubkey) -> bool:
        key = str(mint)
        if key in self._positions or key in self._pending:
            return False
        self._pending.add(key)
        return True

    def clear_pending(self, mint: Pubkey) -> None:
        self._pending.discard(str(mint))

    def open_reason(self, extra_sol_lamports: int, *, adding: bool = False) -> str | None:
        if not adding and len(self._positions) + len(self._pending) >= self.max_open:
            return f"max open positions ({self.max_open})"
        deployed = sum(p.sol_spent for p in self._positions.values())
        if deployed + extra_sol_lamports > self.max_total_lamports:
            return (
                f"max total SOL at risk "
                f"({(deployed + extra_sol_lamports) / LAMPORTS_PER_SOL:.4f} > "
                f"{self.max_total_lamports / LAMPORTS_PER_SOL:.4f})"
            )
        return None

    def add(self, position: Position) -> None:
        key = str(position.mint)
        self._pending.discard(key)
        self._positions[key] = position

    def close(self, mint: Pubkey | str) -> Position | None:
        return self._positions.pop(str(mint), None)
