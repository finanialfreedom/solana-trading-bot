"""Interleaved multi-config sweep.

Why this exists
---------------
Running config A for an hour, then config B for an hour, confounds the comparison
with market conditions. Two control runs of the identical config gave +5.1% and
-6.2% expectancy -- an 11 point swing -- so a single sequential block per config
cannot separate a real improvement from regime drift.

This runner instead interleaves SHORT blocks of every config in rotation:

    rep 1:  A  B  C  D
    rep 2:  B  C  D  A
    rep 3:  C  D  A  B   ...

Every config therefore samples the same span of market conditions, and each config's
blocks are spread across the whole session. Aggregation pools all trades per config
and also reports the spread across blocks so the noise floor is visible.

Usage:
    python tools/interleaved_sweep.py <block_seconds> <reps>

Note: only ONE bot may run at a time because every run writes reports/trades.csv.
"""
from __future__ import annotations

import json
import re
import statistics as st
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
RESULTS = REPO / "reports" / "experiments.jsonl"
MIN_SAMPLE = 200
TARGET_EXPECTANCY = 0.0
MAX_TRADES = "1500"

PURE_RIDE = {"MC_SCALE_OUT_LADDER": "off"}

# Diagnostic A/B/C: is the recent collapse from our own changes, or the market?
# The two prime suspects land at the same hour as the flip:
#   - MAX_OPEN_POSITIONS raised 4 -> 16 at 17:01 (trade volume tripled: 687 in 17h
#     vs ~150-300/hour all morning)
#   - trailing tolerance started widening, and exits went pure-ride, at 16:04-16:15
# All three arms share the same entry and the same code; only these knobs differ.
#
# A-legacy reproduces the morning's shape: few slots, no ladder, flat trailing
# tolerance (every tier callback = TRAILING_STOP_PCT = 15).
#   A vs B  -> isolates the slot cap + trailing widening
#   B vs C  -> isolates the take-profit ladder
FLAT_TRAIL = {
    "TRAIL_TIER_1_CALLBACK": "15",
    "TRAIL_TIER_2_CALLBACK": "15",
    "TRAIL_TIER_3_CALLBACK": "15",
    "TRAIL_TIER_4_CALLBACK": "15",
}

SCHEDULE: list[tuple[str, dict[str, str]]] = [
    ("A-legacy", {"MAX_OPEN_POSITIONS": "4", **PURE_RIDE, **FLAT_TRAIL}),
    ("B-cap16", {"MAX_OPEN_POSITIONS": "16", **PURE_RIDE}),
    ("C-ladder", {"MAX_OPEN_POSITIONS": "16"}),
]

# Did the anti-rug filters we enabled at 18:00 block the coins that would have run?
# The runner count collapsed to zero at exactly that hour, so this is unresolved.
# C-sec-morning restores the pre-change threshold values (looser insider cap and
# bundle count, no liquidity floor, transfer tax tolerated), which separates "the new
# thresholds are too tight" from "any security filtering at all hurts".
SECURITY_SCHEDULE: list[tuple[str, dict[str, str]]] = [
    ("A-sec-on", {}),
    ("B-sec-off", {"SECURITY_FILTERS_ENABLED": "false"}),
    (
        "C-sec-morning",
        {
            "MIN_LIQUIDITY_USD": "0",
            "MAX_INSIDER_TOKENS": "10000000",
            "BUNDLE_WALLET_COUNT": "5",
            "REJECT_TRANSFER_FEE": "false",
        },
    ),
]

# Entry-momentum filters. These three have been at zero all along, so the bot buys
# every launch including ones with no buying interest -- which is exactly the ~54%
# of cold-regime trades that never tick above entry. Joining the logged entry-time
# momentum (buyers / buy_sol) to outcomes showed a large, monotonic improvement
# *within both regimes*, and it does not cut the big runners (identical max MFE):
#   evening win rate  9.3% -> 30.8% at buyers>=5, 32.7% at buy_sol>=3
#   morning win rate 84.8% -> 95.6% at buy_sol>=1
# This sweep re-tests that live, on current market conditions.
ENTRY_SCHEDULE: list[tuple[str, dict[str, str]]] = [
    ("A-entry-none", {"MIN_CONFIRM_BUY_SOL": "0", "MIN_UNIQUE_BUYERS": "0"}),
    ("B-entry-1", {"MIN_CONFIRM_BUY_SOL": "1"}),
    ("C-entry-2", {"MIN_CONFIRM_BUY_SOL": "2"}),
    ("D-entry-3", {"MIN_CONFIRM_BUY_SOL": "3"}),
]

# Regime gate. Walk-forward replay over 2,946 closed trades said gating entries on
# "share of the last 50 closed trades that reached +10% MFE" would have lifted total
# PnL from +12,466 to +29,974 pnl-points, by shutting off completely from 16h onward
# (health 0.42 -> 0.07) while allowing 100% of the two best hours. That replay has no
# lookahead, but it is one day of one regime, so this sweep tests it live:
#   A-gate-off   control
#   B-gate-40    the replay's optimum (threshold 45% is the peak; 40% is one step safer)
#   C-gate-30    looser, trades more of the shoulder hours
GATE_SCHEDULE: list[tuple[str, dict[str, str]]] = [
    ("A-gate-off", {"REGIME_FILTER_ENABLED": "false"}),
    ("B-gate-40", {"REGIME_FILTER_ENABLED": "true", "REGIME_MIN_UP10_PCT": "40"}),
    ("C-gate-30", {"REGIME_FILTER_ENABLED": "true", "REGIME_MIN_UP10_PCT": "30"}),
]

# Dead-coin patience. The rule currently gives up on a position if it is still flat
# (< +10%, trailing not yet armed) 30 seconds after entry. It fires ~800 times in the
# log and every firing exited at roughly 0% MFE -- so on the trades it touches it is
# not throwing away gains that had already appeared. What it cannot show is the trade
# it never allowed to develop: the counterfactual price path after 30s is not in the
# log. So test patience directly.
#   A-dead30   current behaviour (control)
#   B-dead60   twice the patience
#   C-dead90   three times
#   D-dead-off rule disabled -- the strict upper bound on the opportunity cost, but
#              flat positions then occupy slots until another rule fires
DEADCOIN_SCHEDULE: list[tuple[str, dict[str, str]]] = [
    ("A-dead30", {"DEAD_COIN_SECONDS": "30"}),
    ("B-dead60", {"DEAD_COIN_SECONDS": "60"}),
    ("C-dead90", {"DEAD_COIN_SECONDS": "90"}),
    ("D-dead-off", {"DEAD_COIN_SECONDS": "0"}),
]

# Confirm-window sell pressure. The gate has only ever looked at gross buy volume,
# which the deployer and a couple of bots can manufacture on their own. Track sells
# too and reject launches that are being dumped into. MIN_CONFIRM_BUY_SOL stays at its
# current 2.0 in every arm so this measures the increment, not the level.
#   A-press-off   control
#   B-press-net1  require buys - sells >= 1 SOL in the window
#   C-press-r50   require sells <= 0.5x buys
#   D-press-both  both guards together
PRESSURE_SCHEDULE: list[tuple[str, dict[str, str]]] = [
    ("A-press-off", {"MIN_CONFIRM_NET_SOL": "0", "MAX_CONFIRM_SELL_RATIO": "0"}),
    ("B-press-net1", {"MIN_CONFIRM_NET_SOL": "1"}),
    ("C-press-r50", {"MAX_CONFIRM_SELL_RATIO": "0.5"}),
    ("D-press-both", {"MIN_CONFIRM_NET_SOL": "1", "MAX_CONFIRM_SELL_RATIO": "0.5"}),
]

# Trailing callback width. This is the biggest measured leak on the exit side: with
# the current 10% callback, positions that peaked in [+10%,+30%) booked -2.9% on
# average -- about ten points worse than the callback should imply, because the mark
# jumps past the trigger between ticks. The exit lands wherever the overshoot stops,
# so a tighter callback should land higher. Four settings, both directions, so the
# sweep can also tell us if we are already cutting runners too early.
PROTECT_SCHEDULE: list[tuple[str, dict[str, str]]] = [
    ("A-trail10", {"TRAILING_STOP_PCT": "10"}),
    ("B-trail7", {"TRAILING_STOP_PCT": "7"}),
    ("C-trail5", {"TRAILING_STOP_PCT": "5"}),
    ("D-trail13", {"TRAILING_STOP_PCT": "13"}),
]

# The overnight validation. Every arm above ran 8-minute blocks, which yields 10-40
# trades -- far too few to judge fat-tailed memecoin returns, where a single 1000%
# trade can swing a small sample's mean by hundreds of points. Measured trade rate is
# 2 trades per minute of arm-time for a filtered arm, so 30-minute blocks x 3 reps
# gives each arm roughly 180 trades and three independent blocks for variance.
#
#   A-stack-off  the settings that are live today (entry 2 SOL, no sell pressure)
#   B-stack-press  A plus the sell-pressure guards
#   C-stack-full   B plus the regime gate at its live-tested threshold
#
# B isolates the pressure filter; C isolates the gate on top of it.
FINAL_SCHEDULE: list[tuple[str, dict[str, str]]] = [
    (
        "A-stack-off",
        {
            "MIN_CONFIRM_BUY_SOL": "2",
            "MIN_CONFIRM_NET_SOL": "0",
            "MAX_CONFIRM_SELL_RATIO": "0",
            "REGIME_FILTER_ENABLED": "false",
        },
    ),
    (
        "B-stack-press",
        {
            "MIN_CONFIRM_BUY_SOL": "2",
            "MIN_CONFIRM_NET_SOL": "1",
            "MAX_CONFIRM_SELL_RATIO": "0.5",
            "REGIME_FILTER_ENABLED": "false",
        },
    ),
    (
        "C-stack-full",
        {
            "MIN_CONFIRM_BUY_SOL": "2",
            "MIN_CONFIRM_NET_SOL": "1",
            "MAX_CONFIRM_SELL_RATIO": "0.5",
            "REGIME_FILTER_ENABLED": "true",
            "REGIME_MIN_UP10_PCT": "30",
        },
    ),
]

# Capacity is the binding constraint, so the bar should be higher than it is.
#
# Only ~8 positions fit inside MAX_TOTAL_SOL_AT_RISK=8 at 1 SOL each, and the logs show
# 'max open positions' rejecting 2,750 entries and 'max total SOL at risk' rejecting
# 1,221 -- all of which had already passed the entry filter. Slots are therefore scarce
# and are currently filled first-come-first-served, so a coin with 2 SOL of buying takes
# the same slot as one with 20. Raising the bar spends scarce slots on stronger coins.
#
# Total profit is what matters here, not expectancy: a stricter filter can raise
# expectancy while lowering profit if it rejects too much. The earlier entry sweep
# peaked at 2 SOL on net SOL, but its arms only saw 36-43 trades; this re-tests with
# much higher thresholds and real sample sizes.
THRESHOLD_SCHEDULE: list[tuple[str, dict[str, str]]] = [
    ("A-thr2", {"MIN_CONFIRM_BUY_SOL": "2"}),
    ("B-thr4", {"MIN_CONFIRM_BUY_SOL": "4"}),
    ("C-thr8", {"MIN_CONFIRM_BUY_SOL": "8"}),
    ("D-thr16", {"MIN_CONFIRM_BUY_SOL": "16"}),
]

# Reward-side packages. These knobs interact (a tight callback is only safe behind a
# floor), so they are tested as coherent packages rather than one knob at a time.
# Stop loss and per-trade risk are deliberately untouched.
#   A-cur    what .env does today: arm +12%, trail 10%, first floor at peak 50%
#   B-tight  arm +10%, trail 6%, floors from peak 20%
#   C-mid    arm +12%, trail 8%, floors from peak 25%
#   D-loose  arm +18%, trail 14%, floors unchanged
# B/C protect modest winners that currently round-trip; D checks whether we are
# cutting runners early, which would be the opposite mistake.
PROTECT2_SCHEDULE: list[tuple[str, dict[str, str]]] = [
    ("A-cur", {}),
    (
        "B-tight",
        {
            "TAKE_PROFIT_TRIGGER_PCT": "10", "TRAILING_STOP_PCT": "6",
            "RATCHET_1_PEAK": "20", "RATCHET_1_FLOOR": "10",
            "RATCHET_2_PEAK": "100", "RATCHET_2_FLOOR": "60",
        },
    ),
    (
        "C-mid",
        {
            "TAKE_PROFIT_TRIGGER_PCT": "12", "TRAILING_STOP_PCT": "8",
            "RATCHET_1_PEAK": "25", "RATCHET_1_FLOOR": "12",
            "RATCHET_2_PEAK": "100", "RATCHET_2_FLOOR": "60",
        },
    ),
    (
        "D-loose",
        {
            "TAKE_PROFIT_TRIGGER_PCT": "18", "TRAILING_STOP_PCT": "14",
        },
    ),
]

# Scale-out ladder. Banks partial profit as market cap rises. Moving it earlier banks
# more from coins that stall before $50k; moving it later keeps more riding.
LADDER_SCHEDULE: list[tuple[str, dict[str, str]]] = [
    ("A-lad-cur", {}),
    (
        "B-lad-early",
        {"MC_SCALE_OUT_LADDER": "25000:40,60000:30,150000:30,300000:40,600000:40,1000000:100"},
    ),
    (
        "C-lad-late",
        {"MC_SCALE_OUT_LADDER": "100000:50,250000:30,500000:30,1000000:100"},
    ),
]

SCHEDULES: dict[str, list[tuple[str, dict[str, str]]]] = {
    "regime": SCHEDULE,
    "security": SECURITY_SCHEDULE,
    "entry": ENTRY_SCHEDULE,
    "gate": GATE_SCHEDULE,
    "deadcoin": DEADCOIN_SCHEDULE,
    "pressure": PRESSURE_SCHEDULE,
    "protect": PROTECT_SCHEDULE,
    "final": FINAL_SCHEDULE,
    "threshold": THRESHOLD_SCHEDULE,
    "protect2": PROTECT2_SCHEDULE,
    "ladder": LADDER_SCHEDULE,
}


def base_label(label: str) -> str:
    """Collapse a per-rep label ('B-buysol-0.3-r2') back to its config ('B-buysol-0.3')."""
    return re.sub(r"-r\d+$", "", label)


def read_results() -> list[dict]:
    if not RESULTS.exists():
        return []
    lines = [ln for ln in RESULTS.read_text(encoding="utf-8").splitlines() if ln.strip()]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def pooled_by_label() -> dict[str, dict]:
    """Pool every block of a config into one trade-weighted estimate."""
    known = {label for label, _ in SCHEDULE}
    agg: dict[str, dict] = {}
    for row in read_results():
        label = base_label(row.get("label", ""))
        if label not in known or not row.get("n"):
            continue
        bucket = agg.setdefault(
            label,
            {"n": 0, "weighted_exp": 0.0, "blocks": [], "net": 0.0,
             "weighted_med": 0.0, "weighted_wr": 0.0, "weighted_mfe10": 0.0},
        )
        bucket["n"] += row["n"]
        bucket["weighted_exp"] += row["expectancy_pct"] * row["n"]
        bucket["net"] += row.get("net_sol", 0.0)
        bucket["blocks"].append(row["expectancy_pct"])
        # Robust companions to the mean. Memecoin returns are fat-tailed: a single
        # 1000% trade can carry a small block, so the median and the win rate must
        # agree with the mean before a result is believed.
        bucket["weighted_med"] += (row.get("median_pct") or 0.0) * row["n"]
        bucket["weighted_wr"] += (row.get("win_rate_pct") or 0.0) * row["n"]
        bucket["weighted_mfe10"] += (row.get("mfe_ge_10_pct") or 0.0) * row["n"]
    for bucket in agg.values():
        n = bucket["n"]
        bucket["pooled_exp"] = bucket["weighted_exp"] / n if n else 0.0
        bucket["pooled_med"] = bucket["weighted_med"] / n if n else 0.0
        bucket["pooled_wr"] = bucket["weighted_wr"] / n if n else 0.0
        bucket["pooled_mfe10"] = bucket["weighted_mfe10"] / n if n else 0.0
        bucket["block_std"] = st.pstdev(bucket["blocks"]) if len(bucket["blocks"]) > 1 else 0.0
    return agg


def report(agg: dict[str, dict]) -> None:
    if not agg:
        return
    print("\n=== pooled results (trade-weighted across interleaved blocks) ===")
    print(f"{'config':<18} {'blk':>4} {'n':>6} {'mean%':>9} {'med%':>8} {'win%':>7} "
          f"{'mfe10%':>7} {'std':>8} {'netSOL':>9}")
    print("-" * 84)
    for label in sorted(agg, key=lambda k: -agg[k]["pooled_exp"]):
        b = agg[label]
        print(f"{label:<18} {len(b['blocks']):>4} {b['n']:>6} {b['pooled_exp']:>+9.2f} "
              f"{b['pooled_med']:>+8.2f} {b['pooled_wr']:>7.1f} "
              f"{b['pooled_mfe10']:>7.1f} {b['block_std']:>8.2f} {b['net']:>+9.3f}")
    print("  read: mean is what we earn; med/win%/mfe10% must agree before trusting it")


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    global SCHEDULE
    block_seconds = int(sys.argv[1])
    reps = int(sys.argv[2])
    schedule_name = sys.argv[3] if len(sys.argv) > 3 else "regime"
    if schedule_name not in SCHEDULES:
        sys.exit(f"unknown schedule {schedule_name!r}; pick one of {sorted(SCHEDULES)}")
    SCHEDULE = SCHEDULES[schedule_name]

    print(f"interleaved sweep [{schedule_name}]: {len(SCHEDULE)} configs x {reps} reps x "
          f"{block_seconds}s = {len(SCHEDULE) * reps * block_seconds / 3600:.1f}h max", flush=True)

    for rep in range(reps):
        # rotate the order each rep so no config always gets the same slot
        order = SCHEDULE[rep % len(SCHEDULE):] + SCHEDULE[:rep % len(SCHEDULE)]
        print(f"\n--- rep {rep + 1}/{reps} order: {[lbl for lbl, _ in order]} ---", flush=True)
        for label, overrides in order:
            run_label = f"{label}-r{rep + 1}"
            env_overrides = {"MAX_TRADES": MAX_TRADES, **overrides}
            cmd = [str(PYTHON), "tools/run_experiment.py", run_label, str(block_seconds)]
            cmd += [f"{k}={v}" for k, v in env_overrides.items()]
            print(f">>> {run_label}", flush=True)
            subprocess.run(cmd, cwd=str(REPO))
            report(pooled_by_label())

        agg = pooled_by_label()
        winner = None
        for label, bucket in agg.items():
            if label.startswith("A-control"):
                continue
            if bucket["n"] >= MIN_SAMPLE and bucket["pooled_exp"] > TARGET_EXPECTANCY:
                if winner is None or bucket["pooled_exp"] > agg[winner]["pooled_exp"]:
                    winner = label
        control = agg.get(next((k for k in agg if k.startswith("A-control")), ""), None)
        if winner and control:
            # only accept a win that beats control by more than one block std of noise
            noise = max(control["block_std"], agg[winner]["block_std"], 0.5)
            delta = agg[winner]["pooled_exp"] - control["pooled_exp"]
            print(f"\nbest={winner} ({agg[winner]['pooled_exp']:+.2f}%) vs "
                  f"control ({control['pooled_exp']:+.2f}%); delta {delta:+.2f} vs noise {noise:.2f}",
                  flush=True)
            if delta > noise:
                print(f"\n*** TARGET MET: {winner} beats control beyond the noise floor ***", flush=True)
                return

    print("\n*** sweep finished; see pooled results above ***", flush=True)


if __name__ == "__main__":
    main()
