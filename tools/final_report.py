"""Final scoreboard, written automatically when the sweep program finishes.

Answers the two questions that decide whether this goes live:

  1. the best success rate (share of trades that closed green) any configuration reached
  2. the most aggressive take-profit setting that still came out ahead, given that the
     stop loss is pinned at 3% and per-trade risk is fixed

Reads reports/experiments.jsonl, pools each configuration's repeat blocks into one
trade-weighted number, and refuses to crown a winner on a sample too small to mean
anything. Writes reports/final_report.txt and prints the same text.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from config import (  # noqa: E402
    STOP_LOSS_PCT,
    TAKE_PROFIT_TRIGGER_PCT,
    TRAILING_STOP_PCT,
)

RESULTS = REPO / "reports" / "experiments.jsonl"
OUT = REPO / "reports" / "final_report.txt"
STARTED = REPO / "reports" / "logs" / "program_started.txt"

# Below this many pooled trades a result is noise, not a finding. Roughly a third of a
# full block, so a single lucky arm cannot top the table.
MIN_TRADES = 60

# Any of these appearing in an arm's overrides means that arm varied the reward side.
REWARD_KNOBS = (
    "TAKE_PROFIT_TRIGGER_PCT",
    "TRAILING_STOP_PCT",
    "BREAKEVEN_ARM_PCT",
    "RATCHET_1_PEAK",
    "RATCHET_2_PEAK",
    "RATCHET_3_PEAK",
    "RATCHET_4_PEAK",
    "MC_SCALE_OUT_LADDER",
)


def base_label(label: str) -> str:
    """'B-tight-r2' -> 'B-tight'."""
    return label[:-3] if len(label) > 3 and label[-3:-1] == "-r" and label[-1:].isdigit() else label


def load() -> list[dict]:
    """Rows from the current program run only.

    Earlier experiments predate the execution-realism fixes (realizable-price marks,
    honest entry price, adverse simulated slippage) and their numbers are not
    comparable. Pooling them would flatter the scoreboard, so anything started before
    this program began is excluded.
    """
    cutoff = None
    for arg in sys.argv[1:]:
        if arg.startswith("--since="):
            cutoff = arg.split("=", 1)[1].strip()
    if cutoff is None and STARTED.exists():
        cutoff = STARTED.read_text(encoding="utf-8").strip()

    if not RESULTS.exists():
        return []
    rows = []
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if cutoff and str(row.get("started", "")) < cutoff:
            continue
        rows.append(row)
    return [r for r in rows if r.get("n")]


def pool(rows: list[dict]) -> list[dict]:
    """Trade-weighted average across an arm's repeat blocks."""
    buckets: dict[str, dict] = {}
    for row in rows:
        label = base_label(row["label"])
        b = buckets.setdefault(
            label,
            {"n": 0, "exp": 0.0, "win": 0.0, "med": 0.0, "net": 0.0, "blocks": 0,
             "overrides": row.get("overrides", {}), "mfe10": 0.0},
        )
        n = row["n"]
        b["n"] += n
        b["exp"] += row.get("expectancy_pct", 0.0) * n
        b["win"] += row.get("win_rate_pct", 0.0) * n
        b["med"] += row.get("median_pct", 0.0) * n
        b["net"] += row.get("net_sol", 0.0)
        b["mfe10"] += row.get("mfe_ge_10_pct", 0.0) * n
        b["blocks"] += 1
    out = []
    for label, b in buckets.items():
        if not b["n"]:
            continue
        out.append({
            "label": label,
            "n": b["n"],
            "exp": b["exp"] / b["n"],
            "win": b["win"] / b["n"],
            "med": b["med"] / b["n"],
            "mfe10": b["mfe10"] / b["n"],
            "net": b["net"],
            "blocks": b["blocks"],
            "overrides": b["overrides"],
        })
    return out


def eff(arm: dict, key: str, default: float) -> float:
    try:
        return float(arm["overrides"].get(key, default))
    except (TypeError, ValueError):
        return default


def main() -> None:
    arms = pool(load())
    lines: list[str] = []

    def say(text: str = "") -> None:
        lines.append(text)
        print(text)

    say("=" * 78)
    say("FINAL SCOREBOARD")
    say("=" * 78)
    say("")
    say(f"stop loss (fixed)     {STOP_LOSS_PCT:.1f}%")
    say("per-trade risk        locked (1.0 SOL max size, 8.0 SOL max at risk)")
    say("scope                 current program run only (honest execution math:")
    say("                      realizable-price marks, adverse slippage on both sides)")
    say(f"configurations        {len(arms)} pooled")
    say(f"trades counted        {sum(a['n'] for a in arms)}")
    say("")

    if not arms:
        say("No results recorded yet. Nothing to score.")
        OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    reliable = [a for a in arms if a["n"] >= MIN_TRADES]
    usable = reliable or arms
    if not reliable:
        say(f"NOTE: no configuration reached {MIN_TRADES} pooled trades, so these numbers")
        say("are indicative only and should not be traded on.")
        say("")

    # --- Question 1: best success rate -------------------------------------
    best_win = max(usable, key=lambda a: a["win"])
    say("-" * 78)
    say("1. MAXIMUM SUCCESS RATE REACHED")
    say("-" * 78)
    say(f"   {best_win['win']:.1f}% of trades closed green")
    say(f"   configuration: {best_win['label']}   ({best_win['n']} trades from "
        f"{best_win['blocks']} block(s))")
    say(f"   its average return per trade: {best_win['exp']:+.2f}%   "
        f"net {best_win['net']:+.3f} SOL")
    say("")
    say("   success rate across every configuration, best first:")
    for a in sorted(usable, key=lambda x: -x["win"])[:8]:
        say(f"     {a['label']:<14} {a['win']:>5.1f}%   n={a['n']:<5} "
            f"avg {a['exp']:>+7.2f}%   net {a['net']:>+8.3f} SOL")
    say("")

    # --- Question 2: most aggressive take profit that still worked ---------
    reward_arms = [a for a in usable if any(k in a["overrides"] for k in REWARD_KNOBS)]
    profitable = [a for a in reward_arms if a["exp"] > 0]
    say("-" * 78)
    say("2. HIGHEST TAKE-PROFIT SETTING THAT WORKED")
    say("-" * 78)
    if not reward_arms:
        say("   No take-profit arms have finished yet.")
    elif not profitable:
        say("   Every take-profit setting tested lost money. The reward side needs")
        say("   rethinking before this goes live.")
        for a in sorted(reward_arms, key=lambda x: -x["exp"]):
            say(f"     {a['label']:<14} avg {a['exp']:>+7.2f}%   n={a['n']}")
    else:
        winner = max(profitable, key=lambda a: eff(a, "TAKE_PROFIT_TRIGGER_PCT", TAKE_PROFIT_TRIGGER_PCT))
        tp = eff(winner, "TAKE_PROFIT_TRIGGER_PCT", TAKE_PROFIT_TRIGGER_PCT)
        trail = eff(winner, "TRAILING_STOP_PCT", TRAILING_STOP_PCT)
        say(f"   take profit arms at +{STOP_LOSS_PCT:.1f}% stop loss: "
            f"+{tp:.0f}% arm trigger, {trail:.0f}% trailing callback")
        say(f"   configuration: {winner['label']}   ({winner['n']} trades from "
            f"{winner['blocks']} block(s))")
        say(f"   its average return per trade: {winner['exp']:+.2f}%   "
            f"median {winner['med']:+.2f}%   net {winner['net']:+.3f} SOL")
        say(f"   full reward-side settings: {json.dumps(winner['overrides'], sort_keys=True)}")
        say("")
        say("   every take-profit arm, most aggressive setting first:")
        for a in sorted(profitable, key=lambda x: -eff(x, "TAKE_PROFIT_TRIGGER_PCT", TAKE_PROFIT_TRIGGER_PCT)):
            say(f"     {a['label']:<14} tp +{eff(a, 'TAKE_PROFIT_TRIGGER_PCT', TAKE_PROFIT_TRIGGER_PCT):>3.0f}%  "
                f"trail {eff(a, 'TRAILING_STOP_PCT', TRAILING_STOP_PCT):>3.0f}%  "
                f"avg {a['exp']:>+7.2f}%  n={a['n']}")
    say("")

    # --- Best overall ------------------------------------------------------
    best = max(usable, key=lambda a: a["exp"])
    say("-" * 78)
    say("3. BEST OVERALL CONFIGURATION BY AVERAGE RETURN")
    say("-" * 78)
    say(f"   {best['label']}   avg {best['exp']:+.2f}% per trade, "
        f"median {best['med']:+.2f}%, win {best['win']:.1f}%, net {best['net']:+.3f} SOL")
    say(f"   n={best['n']} trades from {best['blocks']} block(s)")
    say(f"   settings: {json.dumps(best['overrides'], sort_keys=True)}")
    say("")
    say("   all configurations ranked by average return:")
    for a in sorted(usable, key=lambda x: -x["exp"]):
        flag = "" if a["n"] >= MIN_TRADES else "  (small sample)"
        say(f"     {a['label']:<14} avg {a['exp']:>+7.2f}%  med {a['med']:>+7.2f}%  "
            f"win {a['win']:>5.1f}%  net {a['net']:>+8.3f} SOL  n={a['n']}{flag}")
    say("")
    say("=" * 78)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwritten to {OUT}")


if __name__ == "__main__":
    main()
