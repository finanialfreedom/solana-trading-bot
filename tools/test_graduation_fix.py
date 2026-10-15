"""Regression test for the graduation-exit bug.

Before the fix, a coin completing its bonding curve had its curve account closed,
which read back as zero reserves. The position was marked to zero, "sold" for 0 SOL
and booked as a -100% loss -- destroying exactly the coins that won.

Run: python tools/test_graduation_fix.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solana.rpc.async_api import AsyncClient  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from config import LAMPORTS_PER_SOL  # noqa: E402
from positions import Position  # noqa: E402
from trader import Trader  # noqa: E402

WSOL = "So11111111111111111111111111111111111111112"
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def make_position(entry_price: float) -> Position:
    # Entry inside the bonding curve: 0.2 SOL for ~2.3e10 raw tokens.
    tokens = int(0.2 * LAMPORTS_PER_SOL / entry_price)
    return Position(
        mint=Pubkey.from_string(WSOL),
        bonding_curve=Pubkey.from_string(WSOL),
        associated_bonding_curve=Pubkey.from_string(WSOL),
        creator=None,
        name="Test",
        symbol="TEST",
        token_amount=tokens,
        sol_spent=int(0.2 * LAMPORTS_PER_SOL),
        entry_price=entry_price,
        virtual_token_reserves=int(1.073e15) - tokens,
        virtual_quote_reserves=int(30 * LAMPORTS_PER_SOL) + int(0.2 * LAMPORTS_PER_SOL),
    )


def test_zero_reserves_do_not_liquidate() -> None:
    print("\n1. A closed curve reporting zero reserves must not mark the position to zero")
    pos = make_position(2.7958993476e-05)
    pos.update_reserves(int(30 * LAMPORTS_PER_SOL), int(1.073e15))
    before = pos.pnl_pct
    pos.update_reserves(0, 0)  # what a graduated/closed curve reads back
    check("mark survives a zero read", pos.mark_price > 0, f"mark={pos.mark_price:.3e}")
    check("pnl is not -100%", pos.pnl_pct > -99.0, f"pnl={pos.pnl_pct:+.1f}%")
    check("last good price retained", pos.last_good_price > 0, f"{pos.last_good_price:.3e}")
    check("pnl unchanged by the zero read", abs(pos.pnl_pct - before) < 1e-6)


def test_graduation_is_priced_from_amm() -> None:
    print("\n2. After graduation the position is priced from the AMM, not zero")
    entry = 2.7958993476e-05
    pos = make_position(entry)
    pos.update_reserves(int(30 * LAMPORTS_PER_SOL), int(1.073e15))
    pos.update_reserves(0, 0)
    pos.mark_graduated(entry * 15)  # 15x at the AMM after graduation
    check("marked as graduated", pos.graduated)
    check("pnl reflects the AMM price", pos.pnl_pct > 1000.0, f"pnl={pos.pnl_pct:+.0f}%")
    check("mfe updated on graduation", pos.mfe_pct > 1000.0, f"mfe={pos.mfe_pct:+.0f}%")


def test_scale_out_accounting() -> None:
    print("\n3. Hybrid scale-out banks partial profit and keeps the rest open")
    entry = 2.7958993476e-05
    pos = make_position(entry)
    pos.update_reserves(int(30 * LAMPORTS_PER_SOL), int(1.073e15))
    pos.mark_graduated(entry * 15)
    check("mc_usd is positive", pos.mc_usd(102.0) > 0, f"mc=${pos.mc_usd(102.0):,.0f}")

    trader = Trader(AsyncClient("http://127.0.0.1:8899"), Keypair())
    held_before = pos.tokens_held
    sol_before = pos.realized_sol
    trader.book.add(pos)
    reserved = trader.reserve_scale_out(pos, held_before // 2)
    check("reserved half the tokens", reserved == held_before // 2)
    check("reservation applied immediately", pos.tokens_held == held_before - reserved)
    asyncio.run(trader.scale_out(pos, reserved, "test scale-out"))
    check("proceeds banked", pos.realized_sol > sol_before, f"{pos.realized_sol} lamports")
    check("position still open", trader.book.get(pos.mint) is not None)
    check(
        "quote uses the AMM after graduation",
        trader._quote_exit(pos, 1_000) == int(1_000 * entry * 15 * 0.99),
    )


def test_scale_out_actually_runs_from_watchdog() -> None:
    print("\n4. Milestones crossed at once must both sell, sizing off the remainder")
    entry = 2.7958993476e-05
    pos = make_position(entry)
    pos.update_reserves(int(30 * LAMPORTS_PER_SOL), int(1.073e15))
    pos.mark_graduated(entry * 200)  # deep past both configured milestones

    trader = Trader(AsyncClient("http://127.0.0.1:8899"), Keypair())
    trader.book.add(pos)
    trader.trailing._sol_usd = 102.0
    held_before = pos.tokens_held

    async def run() -> None:
        pos.mark_graduated(entry * 200)
        trader.trailing._after_mark(pos, source="test")
        if trader.trailing._scale_tasks:
            await asyncio.gather(*list(trader.trailing._scale_tasks), return_exceptions=True)

    asyncio.run(run())
    check("both milestones taken", pos.milestones_taken == 2, f"{pos.milestones_taken}")
    check("proceeds banked by the scheduled task", pos.realized_sol > 0, f"{pos.realized_sol}")
    # 50% then 50% of the remainder leaves a quarter of the position open.
    check(
        "remainder is a quarter of the original",
        abs(pos.tokens_held - held_before // 4) <= 1,
        f"held={pos.tokens_held} of {held_before}",
    )


def test_trailing_tolerance_widens() -> None:
    print("\n5. A runner must not be cut by the same tolerance as a non-runner")
    from trailing import TrailingGuard

    entry = 2.7958993476e-05
    small = make_position(entry)
    small.update_reserves(int(30 * LAMPORTS_PER_SOL), int(1.073e15))
    small.mark_graduated(entry * 1.4)  # peaked around +40%
    small.track_peak()
    wide = make_position(entry)
    wide.update_reserves(int(30 * LAMPORTS_PER_SOL), int(1.073e15))
    wide.mark_graduated(entry * 20)  # peaked around +1900%
    wide.track_peak()

    check("small peak uses the tight tolerance", small.trail_callback_pct() <= 18.0,
          f"{small.trail_callback_pct():.0f}%")
    check("big peak uses a wider tolerance",
          wide.trail_callback_pct() > small.trail_callback_pct(),
          f"{wide.trail_callback_pct():.0f}% vs {small.trail_callback_pct():.0f}%")

    # A 15% dip from a 10x peak must NOT trigger an exit, but the same dip on a
    # top-of-range position should be tolerated too. Then a 60% collapse must fire.
    wide.mark_graduated(entry * 20 * 0.85)
    reason = TrailingGuard.decide_exit(wide, source="test")
    check("15% dip from a big peak is tolerated", reason is None, f"{reason}")
    wide.mark_graduated(entry * 20 * 0.35)
    reason = TrailingGuard.decide_exit(wide, source="test")
    check("a collapse well past tolerance exits", reason is not None, f"{reason}")


def main() -> None:
    test_zero_reserves_do_not_liquidate()
    test_graduation_is_priced_from_amm()
    test_scale_out_accounting()
    test_scale_out_actually_runs_from_watchdog()
    test_trailing_tolerance_widens()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        raise SystemExit(1)
    print("All graduation-fix checks passed.")


if __name__ == "__main__":
    main()
