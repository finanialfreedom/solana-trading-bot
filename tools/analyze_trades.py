"""Analyse dry-run trades and evaluate entry filters.

The bot records MFE (best PnL reached) and MAE (worst PnL reached) per trade, so we
can separate two questions:

  * entry quality  -> mean MFE, and how often a coin ever moves up
  * exit quality   -> how much of that peak the exit rule actually captures

Only rows that carry MFE/MAE are used, so a stale process running older code cannot
pollute the sample.

Usage:
    python tools/analyze_trades.py trades
    python tools/analyze_trades.py filters <logfile> <since "YYYY-MM-DD HH:MM">
"""
from __future__ import annotations

import csv
import re
import statistics as st
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CSV_PATH = REPO / "reports" / "trades.csv"

CONFIRM_RE = re.compile(
    r"INFO CONFIRM\s+(?P<sym>.+?)\s+buyers=(?P<buyers>\d+)\s+buy_sol=(?P<buy_sol>[\d.]+)\s+"
    r"mc=\$(?P<mc>[\d,]+)\s+probe=(?P<probe>[\d.]+)\s+mint=(?P<mint>\S+)"
)

ARM_PCT = 12.0
CALLBACK_PCT = 10.0


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def load_instrumented() -> list[dict[str, str]]:
    with CSV_PATH.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        r for r in rows
        if r["result"] in ("WON", "LOST")
        and (r.get("mfe_pct") or "").strip() != ""
        and (r.get("mae_pct") or "").strip() != ""
    ]


def analyse_trades() -> None:
    closed = load_instrumented()
    if len(closed) < 20:
        print(f"only {len(closed)} instrumented closed trades; run longer")
        return

    pnl = [num(r["pnl_pct"]) for r in closed]
    mfe = [num(r["mfe_pct"]) for r in closed]
    mae = [num(r["mae_pct"]) for r in closed]
    wins = [r for r in closed if r["result"] == "WON"]
    losers = [r for r in closed if r["result"] == "LOST"]
    invested = sum(num(r["invested"]) or 0 for r in closed)
    returned = sum(num(r["exit_sol"]) or 0 for r in closed)

    print(f"== sample: {len(closed)} closed trades ==")
    print(f"win rate      {len(wins)/len(closed)*100:5.1f}%  ({len(wins)}W / {len(losers)}L)")
    print(f"expectancy    {st.mean(pnl):+6.2f}% per trade   <-- the number that matters")
    print(f"median         {st.median(pnl):+6.2f}%")
    print(f"net P&L       {returned-invested:+.4f} SOL on {invested:.2f} invested")
    print(f"avg win       {st.mean([num(r['pnl_pct']) for r in wins]):+6.1f}%")
    if losers:
        print(f"avg loss      {st.mean([num(r['pnl_pct']) for r in losers]):+6.1f}%")

    print("\n== MFE / MAE ==")
    print(f"MFE  mean {st.mean(mfe):+7.1f}%  p50 {percentile(mfe,50):+7.1f}%  "
          f"p75 {percentile(mfe,75):+7.1f}%  max {max(mfe):+7.1f}%")
    print(f"MAE  mean {st.mean(mae):+7.1f}%  p50 {percentile(mae,50):+7.1f}%  "
          f"p75 {percentile(mae,75):+7.1f}%  min {min(mae):+7.1f}%")

    print("\n  share of trades reaching:")
    for threshold in (5, 10, 15, 20, 30, 50):
        n = sum(1 for m in mfe if m >= threshold)
        print(f"    MFE >= {threshold:3}%  {n:4} ({n/len(mfe)*100:4.0f}%)")

    give_back = [m - num(r["pnl_pct"]) for r, m in zip(closed, mfe)]
    print(f"\navg give-back from peak {st.mean(give_back):+.1f} pts")

    print("\n== exit reasons ==")
    from collections import Counter
    counts = Counter((r["reason"] or "?").split()[0] for r in closed)
    for reason, n in counts.most_common():
        print(f"  {reason:16} {n:4}")


FILTERS = {
    "baseline (no filter)":                  lambda f: True,
    "buy_sol >= 0.3":                        lambda f: f["buy_sol"] >= 0.3,
    "buy_sol >= 0.5":                        lambda f: f["buy_sol"] >= 0.5,
    "buyers >= 3":                           lambda f: f["buyers"] >= 3,
    "mc >= 2500":                            lambda f: f["mc"] >= 2500,
    "mc >= 3000":                            lambda f: f["mc"] >= 3000,
    "mc >= 3000 & buy_sol >= 0.3":           lambda f: f["mc"] >= 3000 and f["buy_sol"] >= 0.3,
    "mc >= 3000 & buy_sol >= 0.5":           lambda f: f["mc"] >= 3000 and f["buy_sol"] >= 0.5,
    "mc >= 3000 & buyers >= 3":              lambda f: f["mc"] >= 3000 and f["buyers"] >= 3,
    "mc>=3000 & buy_sol>=0.3 & buyers>=3":   lambda f: (
        f["mc"] >= 3000 and f["buy_sol"] >= 0.3 and f["buyers"] >= 3
    ),
}


def evaluate_filters(log_path: Path, since: str) -> None:
    features: dict[str, dict[str, float]] = {}
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = CONFIRM_RE.search(line)
        if m:
            features[m.group("mint")] = {
                "buyers": int(m.group("buyers")),
                "buy_sol": float(m.group("buy_sol")),
                "mc": float(m.group("mc").replace(",", "")),
            }

    closed = [
        r for r in load_instrumented()
        if r["bought_at"] >= since and r["mint"] in features
    ]
    print(f"matched {len(closed)} instrumented closed trades since {since}\n")
    if not closed:
        return

    def expectancy(selection):
        total = 0.0
        for r in selection:
            mfe = float(r["mfe_pct"])
            total += (mfe - CALLBACK_PCT) if mfe >= ARM_PCT else float(r["pnl_pct"])
        return total / len(selection) if selection else 0.0

    print(f"{'filter':<40} {'n':>5} {'meanMFE':>8} {'expect':>8} {'win':>5} {'totPnL':>8}")
    for name, predicate in FILTERS.items():
        selection = [r for r in closed if predicate(features[r["mint"]])]
        if not selection:
            print(f"{name:<40} {0:>5}  (blocks everything)")
            continue
        mean_mfe = st.mean([float(r["mfe_pct"]) for r in selection])
        pnls = [float(r["pnl_pct"]) for r in selection]
        win = sum(1 for p in pnls if p >= 0) / len(pnls) * 100
        print(f"{name:<40} {len(selection):>5} {mean_mfe:>+8.1f} {expectancy(selection):>+8.1f} "
              f"{win:>4.0f}% {sum(pnls):>+8.1f}")
    print(f"\nexpectancy assumes arm={ARM_PCT:.0f}% callback={CALLBACK_PCT:.0f}% and exact peak capture")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "trades"
    if mode == "filters":
        if len(sys.argv) < 4:
            sys.exit("usage: analyze_trades.py filters <logfile> <since>")
        evaluate_filters(Path(sys.argv[2]), sys.argv[3])
    else:
        analyse_trades()


if __name__ == "__main__":
    main()
