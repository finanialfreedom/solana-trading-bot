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

PURE_RIDE = {"MC_MILESTONE_1_USD": "0", "MC_MILESTONE_2_USD": "0", "MC_MILESTONE_3_USD": "0"}

SCHEDULE: list[tuple[str, dict[str, str]]] = [
    # Pure-ride arms must zero the milestones explicitly: the shipped default now
    # banks partial profit at $250k/$1M, so relying on defaults would make these
    # arms identical to D.
    ("A-pure-ride", dict(PURE_RIDE)),
    ("B-wide-tiers", {**PURE_RIDE,
                      "TRAIL_TIER_1_PEAK": "50", "TRAIL_TIER_1_CALLBACK": "25",
                      "TRAIL_TIER_2_PEAK": "150", "TRAIL_TIER_2_CALLBACK": "40",
                      "TRAIL_TIER_3_PEAK": "500", "TRAIL_TIER_3_CALLBACK": "55"}),
    ("C-tight-tiers", {**PURE_RIDE,
                       "TRAIL_TIER_1_PEAK": "30", "TRAIL_TIER_1_CALLBACK": "14",
                       "TRAIL_TIER_2_PEAK": "120", "TRAIL_TIER_2_CALLBACK": "22",
                       "TRAIL_TIER_3_PEAK": "400", "TRAIL_TIER_3_CALLBACK": "30"}),
    ("D-light-bank", {
        "MC_MILESTONE_1_USD": "250000", "MC_MILESTONE_1_SELL_PCT": "25",
        "MC_MILESTONE_2_USD": "1000000", "MC_MILESTONE_2_SELL_PCT": "25",
    }),
]


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
        bucket = agg.setdefault(label, {"n": 0, "weighted_exp": 0.0, "blocks": [], "net": 0.0})
        bucket["n"] += row["n"]
        bucket["weighted_exp"] += row["expectancy_pct"] * row["n"]
        bucket["net"] += row.get("net_sol", 0.0)
        bucket["blocks"].append(row["expectancy_pct"])
    for bucket in agg.values():
        bucket["pooled_exp"] = bucket["weighted_exp"] / bucket["n"] if bucket["n"] else 0.0
        bucket["block_std"] = st.pstdev(bucket["blocks"]) if len(bucket["blocks"]) > 1 else 0.0
    return agg


def report(agg: dict[str, dict]) -> None:
    if not agg:
        return
    print("\n=== pooled results (trade-weighted across interleaved blocks) ===")
    print(f"{'config':<22} {'blocks':>6} {'n':>6} {'pooledExp':>10} {'blockStd':>9} {'netSOL':>9}")
    for label in sorted(agg, key=lambda k: -agg[k]["pooled_exp"]):
        b = agg[label]
        print(f"{label:<22} {len(b['blocks']):>6} {b['n']:>6} {b['pooled_exp']:>+10.2f} "
              f"{b['block_std']:>9.2f} {b['net']:>+9.3f}")


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    block_seconds = int(sys.argv[1])
    reps = int(sys.argv[2])

    print(f"interleaved sweep: {len(SCHEDULE)} configs x {reps} reps x "
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
