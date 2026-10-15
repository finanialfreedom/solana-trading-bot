"""Regime gate: stop opening new positions when launches stop following through.

Why this exists
---------------
The strategy's losses are not spread evenly through time. Walk-forward replay over
2,946 closed trades showed the bot made money while launches had follow-through and
bled steadily once they stopped: from 16h onward the win rate collapsed and ~51% of
entries never ticked above their entry price at all. No exit rule can profit from a
position that never goes up, so the only fix is to not open it.

The gate tracks a simple, observable property of recent closed trades -- the share
that reached at least +10% maximum favourable excursion -- and refuses new entries
while that share is below a floor. It uses only trades that have *already closed*, so
there is no lookahead and no dependence on a price forecast.

Warm start: the history is seeded from reports/trades.csv so the gate is useful
immediately in a short run rather than after 50 in-process closes.
"""
from __future__ import annotations

import csv
import logging
from collections import deque
from pathlib import Path

from config import (
    REGIME_FILTER_ENABLED,
    REGIME_MIN_SAMPLES,
    REGIME_MIN_UP10_PCT,
    REGIME_UP10_THRESHOLD_PCT,
    REGIME_WINDOW,
)

logger = logging.getLogger("pump-regime")

TRADES_CSV = Path(__file__).resolve().parent / "reports" / "trades.csv"


class RegimeGate:
    """Decide whether the current market is worth trading."""

    def __init__(
        self,
        *,
        window: int = REGIME_WINDOW,
        min_up10_pct: float = REGIME_MIN_UP10_PCT,
        up10_threshold_pct: float = REGIME_UP10_THRESHOLD_PCT,
        min_samples: int = REGIME_MIN_SAMPLES,
        enabled: bool = REGIME_FILTER_ENABLED,
        seed: bool = True,
    ) -> None:
        self.enabled = enabled
        self.window = max(1, int(window))
        self.min_up10_pct = float(min_up10_pct)
        self.up10_threshold_pct = float(up10_threshold_pct)
        self.min_samples = max(1, int(min_samples))
        # Most recent outcomes last. `None` means "closed without a usable mark".
        self._mfe: deque[float] = deque(maxlen=self.window)
        if seed:
            self._seed_from_csv()
        if self.enabled:
            logger.info(
                "Regime gate ON  window=%s  need >=%.0f%% reaching +%.0f%% MFE  seeded=%s",
                self.window,
                self.min_up10_pct,
                self.up10_threshold_pct,
                len(self._mfe),
            )

    def _seed_from_csv(self) -> None:
        if not TRADES_CSV.exists():
            return
        try:
            with TRADES_CSV.open("r", encoding="utf-8", newline="") as handle:
                rows = [
                    r for r in csv.DictReader(handle)
                    if r.get("result") in ("WON", "LOST")
                ]
        except OSError:
            return
        for row in rows[-self.window:]:
            try:
                self._mfe.append(float(row["mfe_pct"]))
            except (TypeError, ValueError, KeyError):
                continue

    def observe(self, mfe_pct: float | None) -> None:
        """Record one closed trade. Safe to call when the gate is disabled."""
        if mfe_pct is None:
            return
        self._mfe.append(float(mfe_pct))

    @property
    def samples(self) -> int:
        return len(self._mfe)

    @property
    def health(self) -> float:
        """Share of the recent window that reached the follow-through threshold."""
        if not self._mfe:
            return 0.0
        hits = sum(1 for m in self._mfe if m >= self.up10_threshold_pct)
        return hits / len(self._mfe)

    def allows_entry(self) -> bool:
        if not self.enabled:
            return True
        # Not enough evidence either way: let it trade rather than stall on warmup.
        if self.samples < self.min_samples:
            return True
        return self.health * 100.0 >= self.min_up10_pct

    def block_reason(self) -> str | None:
        if self.allows_entry():
            return None
        return (
            f"cold-regime health {self.health * 100:.0f}% < "
            f"{self.min_up10_pct:.0f}% (last {self.samples} closes)"
        )
