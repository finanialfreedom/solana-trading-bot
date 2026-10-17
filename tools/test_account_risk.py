"""Regression checks for the account-level combined open-risk cap."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solders.keypair import Keypair  # noqa: E402

from config import (  # noqa: E402
    LAMPORTS_PER_SOL,
    MAX_ACCOUNT_RISK_PCT,
    MAX_COMBINED_OPEN_RISK_SOL,
    PAPER_ACCOUNT_SOL,
)
from positions import Position, PositionBook  # noqa: E402


def position(spent_sol: float, realized_sol: float = 0.0) -> Position:
    mint = Keypair().pubkey()
    curve = Keypair().pubkey()
    return Position(
        mint=mint,
        bonding_curve=curve,
        associated_bonding_curve=curve,
        creator=None,
        name="Risk test",
        symbol="RISK",
        token_amount=1_000_000,
        sol_spent=int(spent_sol * LAMPORTS_PER_SOL),
        entry_price=1.0,
        virtual_token_reserves=1_000_000,
        virtual_quote_reserves=1_000_000,
        realized_sol=int(realized_sol * LAMPORTS_PER_SOL),
    )


def check(label: str, condition: bool) -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        raise AssertionError(label)


def main() -> None:
    check("paper account is 100 SOL", PAPER_ACCOUNT_SOL == 100.0)
    check("combined account risk is 10%", MAX_ACCOUNT_RISK_PCT == 10.0)
    check("derived combined cap is 10 SOL", MAX_COMBINED_OPEN_RISK_SOL == 10.0)

    book = PositionBook(max_open=16, max_total_sol=MAX_COMBINED_OPEN_RISK_SOL)
    for _ in range(10):
        book.add(position(1.0))
    check("ten 1-SOL positions use the full cap",
          book.deployed_lamports == 10 * LAMPORTS_PER_SOL)
    check("no combined risk remains", book.remaining_risk_lamports == 0)
    check("an eleventh 1-SOL position is rejected",
          "max total SOL at risk" in (book.open_reason(LAMPORTS_PER_SOL) or ""))

    fractional = PositionBook(max_open=16, max_total_sol=10.0)
    fractional.add(position(9.75))
    check("last entry is clamped to remaining 0.25 SOL",
          fractional.clamp_to_remaining_risk(LAMPORTS_PER_SOL)
          == int(0.25 * LAMPORTS_PER_SOL))

    recovered = PositionBook(max_open=16, max_total_sol=10.0)
    recovered.add(position(1.0, realized_sol=0.4))
    check("banked proceeds reduce capital still at risk",
          recovered.deployed_lamports == int(0.6 * LAMPORTS_PER_SOL))
    check("banked proceeds restore available account risk",
          recovered.remaining_risk_lamports == int(9.4 * LAMPORTS_PER_SOL))

    print("All account-risk checks passed.")


if __name__ == "__main__":
    main()
