"""In-memory open positions and risk limits."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time

from solders.pubkey import Pubkey

from config import (
    BREAKEVEN_ARM_PCT,
    LAMPORTS_PER_SOL,
    PUMP_TOKEN_SUPPLY,
    RATCHET_LEVELS,
    TAKE_PROFIT_TRIGGER_PCT,
    TRAILING_STOP_PCT,
    TRAIL_TIERS,
)

# pump.fun's trade fee, used only to mark open positions at what they could actually
# be sold for. Kept local to avoid a config round-trip in a hot property.
_MARK_FEE_BPS = 100


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
    # Post-graduation state. When the bonding curve completes it is closed and
    # reads back zero reserves; the position is re-priced from the PumpSwap AMM.
    graduated: bool = False
    graduated_at: float = 0.0
    amm_price: float = 0.0
    last_good_price: float = 0.0
    # Scale-out bookkeeping: tokens still held (-1 means "all of token_amount")
    # and lamports already banked from partial profit taking.
    remaining_tokens: int = -1
    realized_sol: int = 0
    milestones_taken: int = 0

    @property
    def tokens_held(self) -> int:
        return self.token_amount if self.remaining_tokens < 0 else self.remaining_tokens

    @property
    def capital_at_risk(self) -> int:
        """Initial SOL not yet recovered through completed scale-outs."""
        return max(0, self.sol_spent - self.realized_sol)

    @property
    def mark_price(self) -> float:
        if self.graduated and self.amm_price > 0:
            return self.amm_price
        if self.virtual_token_reserves <= 0 or self.virtual_quote_reserves <= 0:
            # Never fall back to zero: a closed/graduated curve must not mark the
            # position to -100%. Retain the last price we actually observed; if we
            # have not observed one yet, use the price we paid to enter, because we
            # always observed that one.
            return self.last_good_price or self.entry_price
        return self.virtual_quote_reserves / self.virtual_token_reserves

    @property
    def realizable_price(self) -> float:
        """Average SOL per token if this position were sold right now.

        The marginal pool price (mark_price) is what the next tiny trade would pay; it
        is not what an open position is worth, because selling walks the price down the
        curve. Marking against the marginal price flatters every position -- most of all
        right after entry, when the position's own size is largest relative to the pool.
        PnL and every exit rule read this instead, so they see the price we could
        actually get.
        """
        held = self.tokens_held
        quote = self.virtual_quote_reserves
        reserves = self.virtual_token_reserves
        if self.graduated and self.amm_price > 0:
            return self.amm_price
        if held <= 0 or quote <= 0 or reserves <= 0:
            return self.mark_price
        gross = quote * held / (reserves + held)
        net = gross * (1.0 - _MARK_FEE_BPS / 10_000.0)
        return net / held

    @property
    def pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (self.realizable_price - self.entry_price) / self.entry_price * 100.0

    def mc_usd(self, sol_usd: float) -> float:
        """Market cap implied by the current mark, in USD."""
        price = self.mark_price
        if price <= 0 or sol_usd <= 0:
            return 0.0
        return price * PUMP_TOKEN_SUPPLY / LAMPORTS_PER_SOL * sol_usd

    @property
    def peak_gain_pct(self) -> float:
        if self.entry_price <= 0 or self.peak_price <= 0:
            return 0.0
        return (self.peak_price - self.entry_price) / self.entry_price * 100.0

    def trail_callback_pct(self) -> float:
        """Trailing tolerance, widened as the peak gain grows.

        Keeps the stop tight for a coin that is going nowhere while giving a genuine
        runner room to survive normal volatility instead of being cut at +15%.
        """
        callback = TRAILING_STOP_PCT
        for peak, tier_callback in TRAIL_TIERS:
            if self.peak_gain_pct >= peak:
                callback = tier_callback
        return callback

    def ratchet_floor_pct(self) -> float:
        """Minimum gain this position may be sold at, given how high it has been.

        A percent-below-peak stop scales with the peak, so a big winner can still
        round-trip into a loss. This locks in an absolute gain once the peak passes a
        level; returns 0.0 when no level has been reached.
        """
        floor = 0.0
        for peak, locked in RATCHET_LEVELS:
            if self.peak_gain_pct >= peak:
                floor = locked
        return floor

    @property
    def drop_from_peak_pct(self) -> float:
        if self.peak_price <= 0:
            return 0.0
        return (self.peak_price - self.realizable_price) / self.peak_price * 100.0

    def _observe(self) -> None:
        pnl = self.pnl_pct
        if pnl > self.mfe_pct:
            self.mfe_pct = pnl
        if pnl < self.mae_pct:
            self.mae_pct = pnl
        price = self.mark_price
        if price > 0:
            self.last_good_price = price

    def update_reserves(self, virtual_quote: int, virtual_token: int) -> None:
        # A graduated curve reports zeros; ignore those so we keep the last real mark.
        if virtual_quote <= 0 or virtual_token <= 0:
            self.last_mark_at = time()
            return
        self.virtual_quote_reserves = virtual_quote
        self.virtual_token_reserves = virtual_token
        self.last_mark_at = time()
        self._observe()

    def mark_graduated(self, amm_price: float) -> None:
        """Re-price the position from the AMM after the bonding curve completes."""
        if amm_price > 0:
            self.amm_price = amm_price
        if not self.graduated:
            self.graduated = True
            self.graduated_at = time()
        self.last_mark_at = time()
        self._observe()

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
        price = self.realizable_price
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

    @property
    def deployed_lamports(self) -> int:
        return sum(position.capital_at_risk for position in self._positions.values())

    @property
    def remaining_risk_lamports(self) -> int:
        return max(0, self.max_total_lamports - self.deployed_lamports)

    def clamp_to_remaining_risk(self, requested_lamports: int) -> int:
        """Size an entry/add so combined open capital never exceeds the account cap."""
        return max(0, min(int(requested_lamports), self.remaining_risk_lamports))

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
        deployed = self.deployed_lamports
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
