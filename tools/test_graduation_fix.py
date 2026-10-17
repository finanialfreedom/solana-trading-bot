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
from time import time

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


def test_curve_closing_before_first_mark_is_not_a_writeoff() -> None:
    """The -100% outliers: a curve that closes before the position's first successful
    mark left last_good_price at 0, so the position marked to zero and was dumped for
    0 SOL. 49 such trades wrote off 16.2 SOL against a net of +1.28."""
    print("\n1b. A curve closing before the first mark must not write the position off")
    from trailing import TrailingGuard

    pos = make_position(2.7958993476e-05)
    # Never marked: reserves are already zero and last_good_price was never set.
    pos.virtual_token_reserves = 0
    pos.virtual_quote_reserves = 0
    pos.last_good_price = 0.0

    check("mark falls back to the entry price", pos.mark_price == pos.entry_price,
          f"mark={pos.mark_price:.3e}")
    check("not marked as a -100% loss", pos.pnl_pct > -99.0, f"pnl={pos.pnl_pct:+.1f}%")

    reason = TrailingGuard.decide_exit(pos, "test")
    check("no emergency-flat write-off", "emergency-flat" not in (reason or ""),
          f"reason={reason}")

    # And the exit quote must price it at the known mark, not on the zeroed curve.
    trader = Trader.__new__(Trader)
    trader.global_cfg = None
    quote = Trader._quote_exit(trader, pos, pos.tokens_held)
    check("exit quote is not zero", quote > 0, f"{quote} lamports")


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
    import trailing as trailing_mod

    # Milestones are disabled in the pure-ride default, so enable them just for this
    # test rather than depending on whatever .env happens to contain.
    original = trailing_mod.MC_SCALE_OUT
    trailing_mod.MC_SCALE_OUT = ((250_000.0, 50.0), (500_000.0, 50.0))
    try:
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
    finally:
        trailing_mod.MC_SCALE_OUT = original


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


def test_ratchet_floor_blocks_round_trip() -> None:
    print("\n6. A winner must not round-trip into an overall loss")
    from trailing import TrailingGuard

    entry = 2.7958993476e-05
    pos = make_position(entry)
    pos.update_reserves(int(30 * LAMPORTS_PER_SOL), int(1.073e15))
    pos.mark_graduated(entry * 21)  # peaked around +2000%
    pos.track_peak()
    check("peak recorded", pos.peak_gain_pct >= 2000.0, f"+{pos.peak_gain_pct:.0f}%")
    check("floor locked at the top tier", pos.ratchet_floor_pct() >= 1400.0,
          f"+{pos.ratchet_floor_pct():.0f}%")

    # Must bind *before* the wide trailing tolerance, otherwise it would never matter.
    trailing_implied = (1 - pos.trail_callback_pct() / 100.0) * (1 + pos.peak_gain_pct / 100.0) - 1
    check("floor is tighter than the trailing stop",
          pos.ratchet_floor_pct() / 100.0 > trailing_implied,
          f"floor +{pos.ratchet_floor_pct():.0f}% vs trailing +{trailing_implied * 100:.0f}%")

    # A slide to +1500% is still above the +1400% floor, so it holds.
    pos.mark_graduated(entry * 16)
    check("above the floor is held", TrailingGuard.decide_exit(pos, source="test") is None)
    pos.mark_graduated(entry * 9)  # +800%, below the +1400% floor
    reason = TrailingGuard.decide_exit(pos, source="test")
    check("below the floor exits at a locked gain",
          reason is not None and "ratchet" in reason, f"{reason}")

    # The failure this guards against: a full round trip back toward entry.
    worst = make_position(entry)
    worst.update_reserves(int(30 * LAMPORTS_PER_SOL), int(1.073e15))
    worst.mark_graduated(entry * 21)
    worst.track_peak()
    worst.mark_graduated(entry * 0.1)  # all the way under water
    reason = TrailingGuard.decide_exit(worst, source="test")
    check("a collapsed winner still reports a locked floor", reason is not None, f"{reason}")


def test_gradual_ladder_banks_proportionally() -> None:
    print("\n7. A gradual ladder banks at each level and still leaves a tail riding")
    import config as config_mod
    import trailing as trailing_mod
    from trailing import TrailingGuard

    original = trailing_mod.MC_SCALE_OUT
    try:
        ladder = config_mod._parse_scale_out(config_mod.os.getenv("MC_SCALE_OUT_LADDER", ""))
        trailing_mod.MC_SCALE_OUT = ladder
        check("default ladder starts at $50k", ladder and ladder[0][0] == 50_000.0,
              f"first rung ${ladder[0][0]:,.0f}" if ladder else "empty")
        check("first rung banks half the position", ladder and ladder[0][1] == 50.0,
              f"{ladder[0][1]:.0f}%" if ladder else "-")
        check("final rung closes everything", ladder and ladder[-1][1] == 100.0,
              f"last rung {ladder[-1][1]:.0f}% at ${ladder[-1][0]:,.0f}" if ladder else "-")
        check("ladder is ascending",
              all(ladder[i][0] < ladder[i + 1][0] for i in range(len(ladder) - 1)))

        entry = 2.7958993476e-05
        pos = make_position(entry)
        pos.update_reserves(int(30 * LAMPORTS_PER_SOL), int(1.073e15))
        trader = Trader(AsyncClient("http://127.0.0.1:8899"), Keypair())
        trader.book.add(pos)
        trader.trailing._sol_usd = 102.0
        held = pos.tokens_held
        # mc_usd = price * PUMP_TOKEN_SUPPLY / LAMPORTS_PER_SOL * sol_usd, i.e. ×1e6×102.
        divisor = 102.0 * 1_000_000.0
        retained_after: dict[int, float] = {}

        async def ride_all() -> None:
            for level, (level_mc, _pct) in enumerate(ladder, start=1):
                pos.mark_graduated(level_mc / divisor)
                trader.trailing._after_mark(pos, source="test")
                if trader.trailing._scale_tasks:
                    await asyncio.gather(*list(trader.trailing._scale_tasks),
                                         return_exceptions=True)
                retained_after[level] = pos.tokens_held / held

        asyncio.run(ride_all())
        check("banked once per level", pos.milestones_taken == len(ladder),
              f"{pos.milestones_taken}/{len(ladder)}")
        check("proceeds banked", pos.realized_sol > 0, f"{pos.realized_sol} lamports")
        check("half banked after the first rung",
              abs(retained_after.get(1, 0) - 0.5) < 0.01,
              f"{retained_after.get(1, 0) * 100:.0f}% retained")
        check("a tail is still riding mid-ladder",
              retained_after.get(len(ladder) - 1, 0) > 0.0,
              f"{retained_after.get(len(ladder) - 1, 0) * 100:.0f}% retained before the last rung")
        check("fully closed at the final rung", pos.tokens_held == 0,
              f"{pos.tokens_held} tokens left")
        check("the completed trade is closed out of the book",
              trader.book.get(pos.mint) is None)
    finally:
        trailing_mod.MC_SCALE_OUT = original


def test_regime_gate_blocks_cold_market() -> None:
    """The gate must permit a hot market, refuse a cold one, and never look ahead."""
    from regime import RegimeGate

    print("\n[regime gate]")
    try:
        disabled = RegimeGate(enabled=False, seed=False)
        check("disabled gate never blocks", disabled.allows_entry())

        gate = RegimeGate(
            enabled=True, seed=False, window=20, min_up10_pct=45.0, min_samples=10
        )
        check("warmup does not stall trading", gate.allows_entry())

        for _ in range(15):
            gate.observe(14.0)
        check("hot regime is allowed", gate.allows_entry(), f"health={gate.health:.2f}")

        for _ in range(15):
            gate.observe(-4.0)
        check("cold regime blocks entries", not gate.allows_entry(),
              f"health={gate.health:.2f}")
        check("block reason names the health", "cold-regime" in (gate.block_reason() or ""))

        # A trade that only barely rose must not count as follow-through.
        edge = RegimeGate(
            enabled=True, seed=False, window=10, min_up10_pct=50.0, min_samples=5
        )
        for _ in range(10):
            edge.observe(9.9)
        check("+9.9% MFE does not count as follow-through", not edge.allows_entry(),
              f"health={edge.health:.2f}")

        # The window must stay bounded so the gate tracks the market, not all history.
        rolling = RegimeGate(
            enabled=True, seed=False, window=10, min_up10_pct=50.0, min_samples=5
        )
        for _ in range(50):
            rolling.observe(-1.0)
        check("window stays bounded", rolling.samples == 10, f"{rolling.samples} kept")
    finally:
        pass


def test_confirm_sell_pressure() -> None:
    """Gross buys are gameable; the filter must also see money leaving."""
    print("\n[confirm sell pressure]")
    import confirm as confirm_mod
    from confirm import Candidate, Confirmer
    from pumpfun import CreateInfo

    info = CreateInfo(
        name="T", symbol="T", uri="", mint=Pubkey.from_string(WSOL),
        bonding_curve=Pubkey.from_string(WSOL), creator=None,
        virtual_token_reserves=int(1.073e15), virtual_quote_reserves=int(30 * LAMPORTS_PER_SOL),
    )

    # 2 SOL bought, 1.8 SOL sold: passes a gross-buy gate, but is being dumped into.
    heavy_sell = Candidate(info=info, buy_sol=int(2 * LAMPORTS_PER_SOL),
                           sell_sol=int(1.8 * LAMPORTS_PER_SOL))
    check("net buy math", heavy_sell.net_buy_sol == int(0.2 * LAMPORTS_PER_SOL))
    check("sell ratio math", abs(heavy_sell.sell_ratio - 0.9) < 1e-9,
          f"{heavy_sell.sell_ratio:.2f}")

    healthy = Candidate(info=info, buy_sol=int(4 * LAMPORTS_PER_SOL),
                        sell_sol=int(1 * LAMPORTS_PER_SOL))

    gate = Confirmer.__new__(Confirmer)
    orig = (confirm_mod.MIN_CONFIRM_NET_SOL, confirm_mod.MAX_CONFIRM_SELL_RATIO,
            confirm_mod.MIN_CONFIRM_BUY_SOL, confirm_mod.MIN_UNIQUE_BUYERS,
            confirm_mod.MIN_CONFIRM_MC_USD)
    try:
        confirm_mod.MIN_CONFIRM_BUY_SOL, confirm_mod.MIN_UNIQUE_BUYERS = 2.0, 0
        confirm_mod.MIN_CONFIRM_MC_USD = 0.0
        confirm_mod.MIN_CONFIRM_NET_SOL, confirm_mod.MAX_CONFIRM_SELL_RATIO = 0.0, 0.0
        check("off by default: dump passes a gross-buy-only gate",
              Confirmer._probe_block_reason(gate, heavy_sell, 0.0) is None)

        confirm_mod.MIN_CONFIRM_NET_SOL = 1.0
        r = Confirmer._probe_block_reason(gate, heavy_sell, 0.0)
        check("net-buy guard rejects the dump", r is not None and "net-buy" in r, f"{r}")

        confirm_mod.MIN_CONFIRM_NET_SOL = 0.0
        confirm_mod.MAX_CONFIRM_SELL_RATIO = 0.5
        r = Confirmer._probe_block_reason(gate, heavy_sell, 0.0)
        check("sell-ratio guard rejects the dump", r is not None and "sell-pressure" in r, f"{r}")
        check("sell-ratio guard passes real demand",
              Confirmer._probe_block_reason(gate, healthy, 0.0) is None)

        confirm_mod.MIN_CONFIRM_NET_SOL = 1.0
        check("both guards pass real demand",
              Confirmer._probe_block_reason(gate, healthy, 0.0) is None)
    finally:
        (confirm_mod.MIN_CONFIRM_NET_SOL, confirm_mod.MAX_CONFIRM_SELL_RATIO,
         confirm_mod.MIN_CONFIRM_BUY_SOL, confirm_mod.MIN_UNIQUE_BUYERS,
         confirm_mod.MIN_CONFIRM_MC_USD) = orig


def test_confirm_quality_gate() -> None:
    """Only broad, strong demand should be allowed to consume a position slot."""
    print("\n[confirm quality gate]")
    import confirm as confirm_mod
    from confirm import Candidate, Confirmer
    from pumpfun import CreateInfo

    info = CreateInfo(
        name="T", symbol="T", uri="", mint=Pubkey.from_string(WSOL),
        bonding_curve=Pubkey.from_string(WSOL), creator=None,
        virtual_token_reserves=int(1.073e15), virtual_quote_reserves=int(30 * LAMPORTS_PER_SOL),
    )
    gate = Confirmer.__new__(Confirmer)
    original = (
        confirm_mod.MIN_UNIQUE_BUYERS,
        confirm_mod.MIN_CONFIRM_BUY_SOL,
        confirm_mod.MIN_CONFIRM_NET_SOL,
        confirm_mod.MAX_CONFIRM_SELL_RATIO,
        confirm_mod.MIN_CONFIRM_MC_USD,
    )
    try:
        confirm_mod.MIN_UNIQUE_BUYERS = 15
        confirm_mod.MIN_CONFIRM_BUY_SOL = 25.0
        confirm_mod.MIN_CONFIRM_NET_SOL = 1.0
        confirm_mod.MAX_CONFIRM_SELL_RATIO = 0.5
        confirm_mod.MIN_CONFIRM_MC_USD = 5_000.0

        healthy = Candidate(
            info=info,
            buyers={f"buyer-{i}" for i in range(15)},
            buy_sol=int(25 * LAMPORTS_PER_SOL),
            sell_sol=int(5 * LAMPORTS_PER_SOL),
            # At $100/SOL this is a $5k market cap.
            last_quote=int(50 * LAMPORTS_PER_SOL),
            last_token=int(1e15),
        )
        check("strict quality gate passes broad demand",
              Confirmer._probe_block_reason(gate, healthy, 100.0) is None)

        few = Candidate(
            info=info, buyers={f"buyer-{i}" for i in range(14)},
            buy_sol=healthy.buy_sol, sell_sol=healthy.sell_sol,
            last_quote=healthy.last_quote, last_token=healthy.last_token,
        )
        reason = Confirmer._probe_block_reason(gate, few, 100.0)
        check("quality gate rejects fewer than 15 buyers",
              reason is not None and "few-buyers" in reason, f"{reason}")

        weak = Candidate(
            info=info, buyers=set(healthy.buyers),
            buy_sol=int(24.99 * LAMPORTS_PER_SOL), sell_sol=healthy.sell_sol,
            last_quote=healthy.last_quote, last_token=healthy.last_token,
        )
        reason = Confirmer._probe_block_reason(gate, weak, 100.0)
        check("quality gate rejects less than 25 SOL",
              reason is not None and "low-buy-sol" in reason, f"{reason}")

        small = Candidate(
            info=info, buyers=set(healthy.buyers),
            buy_sol=healthy.buy_sol, sell_sol=healthy.sell_sol,
            last_quote=int(49 * LAMPORTS_PER_SOL), last_token=int(1e15),
        )
        reason = Confirmer._probe_block_reason(gate, small, 100.0)
        check("quality gate rejects market cap below $5k",
              reason is not None and "low-mc" in reason, f"{reason}")
    finally:
        (
            confirm_mod.MIN_UNIQUE_BUYERS,
            confirm_mod.MIN_CONFIRM_BUY_SOL,
            confirm_mod.MIN_CONFIRM_NET_SOL,
            confirm_mod.MAX_CONFIRM_SELL_RATIO,
            confirm_mod.MIN_CONFIRM_MC_USD,
        ) = original


def test_dry_run_execution_is_honest() -> None:
    """A dry-run fill must not be booked better than a real one.

    Old behaviour: entry_price was the price *before* our own buy, while the position
    was sized at tokens_out (which already includes our own impact). A 1 SOL order on a
    fresh curve therefore opened showing +6.7% profit that did not exist, inflating
    every peak and distorting the exit rules that read them.
    """
    print("\n[dry-run execution realism]")
    from pumpfun import BondingCurve, apply_fee, tokens_out_for_sol_in

    curve = BondingCurve(
        virtual_token_reserves=1_073_000_000_000_000,
        virtual_quote_reserves=30 * LAMPORTS_PER_SOL,
        real_token_reserves=793_100_000_000_000,
        real_quote_reserves=0,
        token_total_supply=1_000_000_000_000_000,
        complete=False,
        creator=None,
    )
    spend = 1 * LAMPORTS_PER_SOL
    fee_bps = 100
    tokens = tokens_out_for_sol_in(curve, spend, fee_bps)
    entry = spend / tokens                       # what we now record
    pre_trade = curve.price                      # what the old code recorded

    check("entry price reflects our own impact", entry > pre_trade,
          f"paid {entry:.6e} vs pre-trade {pre_trade:.6e}")
    phantom = (entry - pre_trade) / pre_trade * 100.0
    check("the old entry understated cost by several percent",
          phantom > 3.0, f"was +{phantom:.2f}% 'free'")
    check("entry is not the pre-trade price", abs(entry - pre_trade) > 1e-9)

    # Build the position exactly the way the buy path does, then ask it what it is
    # worth: it should be down roughly the round-trip cost, never up.
    from positions import Position

    pos = Position(
        mint=Pubkey.from_string(WSOL),
        bonding_curve=Pubkey.from_string(WSOL),
        associated_bonding_curve=Pubkey.from_string(WSOL),
        creator=None,
        name="T",
        symbol="T",
        token_amount=tokens,
        sol_spent=spend,
        entry_price=entry,
        virtual_token_reserves=curve.virtual_token_reserves - tokens,
        virtual_quote_reserves=curve.virtual_quote_reserves + apply_fee(spend, fee_bps),
    )
    instant = pos.pnl_pct
    check("no phantom profit on entry", instant <= 0.0, f"instant pnl {instant:+.2f}%")
    check("immediate round-trip cost is the fee plus our own impact",
          -3.0 < instant < -1.0, f"{instant:+.2f}%")
    check("realizable price is below the marginal pool price",
          pos.realizable_price < pos.mark_price,
          f"{pos.realizable_price:.6e} < {pos.mark_price:.6e}")

    # Simulated slippage must make a dry-run fill strictly worse, both ways.
    import config

    original = config.SIM_SLIPPAGE_BPS
    try:
        config.SIM_SLIPPAGE_BPS = 200
        worse = entry * (1.0 / (1.0 - 0.02))
        check("simulated slippage worsens the entry price", worse > entry,
              f"{worse:.6e} vs {entry:.6e}")
    finally:
        config.SIM_SLIPPAGE_BPS = original


def test_trailing_idle_exit_frees_risk() -> None:
    print("\n[trailing idle exit]")
    import trailing as trailing_mod

    entry = 2.7958993476e-05
    pos = make_position(entry)
    pos.mark_graduated(entry * 2)
    check("trailing arms on a profitable position", pos.track_peak())

    original = trailing_mod.TRAILING_IDLE_EXIT_SECONDS
    try:
        trailing_mod.TRAILING_IDLE_EXIT_SECONDS = 10.0
        pos.peak_updated_at = time() - 11.0
        reason = trailing_mod.TrailingGuard.decide_exit(pos, "test")
        check("idle trailing winner exits", "trailing-idle" in (reason or ""), str(reason))

        old_peak_at = pos.peak_updated_at
        pos.mark_graduated(entry * 2.1)
        pos.track_peak()
        check("a fresh high resets the idle clock", pos.peak_updated_at > old_peak_at)
        check(
            "fresh runner is not exited as idle",
            trailing_mod.TrailingGuard.decide_exit(pos, "test") is None,
        )
    finally:
        trailing_mod.TRAILING_IDLE_EXIT_SECONDS = original


def test_curve_refresh_failure_pauses_entries() -> None:
    print("\n[RPC price-refresh entry pause]")
    from types import SimpleNamespace

    class FailingClient:
        async def get_multiple_accounts(self, _keys: object) -> object:
            raise ConnectionError("offline")

    class RecoveredClient:
        async def get_multiple_accounts(self, _keys: object) -> object:
            return SimpleNamespace(value=[None])

    trader = Trader.__new__(Trader)
    trader.client = FailingClient()
    trader._curve_refresh_available = True
    trader._curve_refresh_retry_at = 0.0
    trader._curve_pause_logged = False
    key = Pubkey.from_string(WSOL)

    asyncio.run(trader.load_curves([key]))
    check("failed batch refresh marks entries unsafe", not trader._curve_refresh_available)

    trader.client = RecoveredClient()
    rows = asyncio.run(trader.load_curves([key]))
    check("successful refresh re-enables entries", trader._curve_refresh_available)
    check("missing curve is still represented after recovery", str(key) in rows)


def main() -> None:
    test_zero_reserves_do_not_liquidate()
    test_curve_closing_before_first_mark_is_not_a_writeoff()
    test_graduation_is_priced_from_amm()
    test_scale_out_accounting()
    test_scale_out_actually_runs_from_watchdog()
    test_trailing_tolerance_widens()
    test_ratchet_floor_blocks_round_trip()
    test_gradual_ladder_banks_proportionally()
    test_regime_gate_blocks_cold_market()
    test_confirm_sell_pressure()
    test_confirm_quality_gate()
    test_dry_run_execution_is_honest()
    test_trailing_idle_exit_frees_risk()
    test_curve_refresh_failure_pauses_entries()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        raise SystemExit(1)
    print("All graduation-fix checks passed.")


if __name__ == "__main__":
    main()
