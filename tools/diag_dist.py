"""Diagnostic: attribute closed trades to sweep arms by time window, then
quantify how much of each arm's mean comes from its top trades.

READ-ONLY. Maps reports/trades.csv rows onto reports/experiments.jsonl arm
windows using bought_at timestamps, since trades.csv has no arm column.
"""
from __future__ import annotations

import csv
import json
import pathlib
import statistics as st

EXP = pathlib.Path("reports/experiments.jsonl")
TR = pathlib.Path("reports/trades.csv")
SINCE = "2026-10-16 05:22:32"


def parse_ts(s: str):
    from datetime import datetime

    for fmt in ("%Y-%m-%d %H:%M:%S",):
        try:
            return datetime.strptime(s.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def main() -> None:
    rows = [
        json.loads(l)
        for l in EXP.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    arms = [r for r in rows if str(r.get("started", "")) >= SINCE]

    # Arm windows: started -> started + duration_s
    windows = []
    for a in arms:
        t0 = parse_ts(a["started"])
        if not t0:
            continue
        dur = float(a.get("duration_s") or 1800)
        from datetime import timedelta

        windows.append((a["label"], t0, t0 + timedelta(seconds=dur), a))

    # Trades
    trades: list[tuple[str, float]] = []
    with TR.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("result") or "").upper() == "OPEN":
                continue
            ts = parse_ts(row.get("bought_at") or "")
            raw = row.get("pnl_pct")
            if ts is None or raw in (None, ""):
                continue
            try:
                trades.append((ts, float(raw)))
            except ValueError:
                continue

    print(f"closed trades parsed: {len(trades)}")
    print(f"arms in window:      {len(windows)}")
    print()

    hdr = (
        f"{'arm':13} {'n':>4} {'mean%':>8} {'med%':>7} {'win%':>6} "
        f"{'tot%':>10} {'top1':>9} {'top1sh':>7} {'top3sh':>7} {'exTop1%':>8}"
    )
    print(hdr)
    print("-" * len(hdr))

    for label, t0, t1, a in windows:
        xs = [p for (ts, p) in trades if t0 <= ts < t1]
        if not xs:
            print(f"{label:13} {0:>4}  (no attributed trades)")
            continue
        srt = sorted(xs, reverse=True)
        tot = sum(xs)
        top1 = srt[0]
        top3 = sum(srt[:3])
        mean = st.mean(xs)
        med = st.median(xs)
        win = 100.0 * sum(1 for x in xs if x > 0) / len(xs)
        # Mean with the single best trade removed: the "is it one lottery ticket" test.
        rest = sorted(xs)[:-1]
        ex1 = st.mean(rest) if rest else 0.0
        s1 = f"{top1 / tot * 100:.0f}%" if tot > 0 else "n/a"
        s3 = f"{top3 / tot * 100:.0f}%" if tot > 0 else "n/a"
        print(
            f"{label:13} {len(xs):>4} {mean:>8.2f} {med:>7.2f} {win:>6.1f} "
            f"{tot:>10.1f} {top1:>9.1f} {s1:>7} {s3:>7} {ex1:>8.2f}"
        )

    print()
    print("=== biggest single trades in the whole run ===")
    for ts, p in sorted(trades, key=lambda t: -t[1])[:12]:
        arm = next((lb for lb, a0, a1, _ in windows if a0 <= ts < a1), "?")
        print(f"  {ts}  {p:>10.1f}%   {arm}")


if __name__ == "__main__":
    main()
