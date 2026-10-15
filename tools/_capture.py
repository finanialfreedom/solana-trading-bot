"""How much of each winner's peak do we actually keep?

For every closed trade we know the maximum favourable excursion (mfe_pct, the highest
the position ever traded) and what we finally got (pnl_pct). The gap is what the exit
machinery gave back. If big runners give back most of their peak, the trailing ladder
and ratchet floor are leaving money on the table; if capture is already high, exits are
fine and the remaining edge is on the entry side.
"""
import csv
import statistics as st
from pathlib import Path

rows = [
    r for r in csv.DictReader(Path("reports/trades.csv").open(encoding="utf-8", errors="replace"))
    if r.get("result") in ("WON", "LOST")
]


def num(r, k):
    try:
        return float(r[k])
    except (TypeError, ValueError):
        return None


closed = []
for r in rows:
    p, m = num(r, "pnl_pct"), num(r, "mfe_pct")
    if p is None:
        continue
    closed.append((p, m if m is not None else 0.0, r.get("reason", "")))

print(f"closed trades: {len(closed)}")
print(f"overall mean pnl: {st.mean([p for p, _, _ in closed]):+.2f}%\n")

# Bucket by how far the coin actually ran.
bands = [
    ("never rose (<0)", lambda m: m < 0),
    ("flat 0-10%", lambda m: 0 <= m < 10),
    ("+10-50%", lambda m: 10 <= m < 50),
    ("+50-100%", lambda m: 50 <= m < 100),
    ("+100-300%", lambda m: 100 <= m < 300),
    ("+300-1000%", lambda m: 300 <= m < 1000),
    ("rocket >=1000%", lambda m: m >= 1000),
]

print(f"{'band':<18}{'n':>6}{'mean MFE':>10}{'mean PnL':>10}{'capture':>9}{'negative':>10}")
print("-" * 63)
for label, test in bands:
    grp = [(p, m) for p, m, _ in closed if test(m)]
    if not grp:
        continue
    mp = st.mean([p for p, _ in grp])
    mm = st.mean([m for _, m in grp])
    neg = sum(1 for p, _ in grp if p < 0)
    cap = (mp / mm * 100) if mm > 0 else float("nan")
    print(f"{label:<18}{len(grp):>6}{mm:>9.1f}%{mp:>9.1f}%{cap:>8.0f}%{neg:>7} ({100*neg/len(grp):.0f}%)")

# The expensive failure mode: the coin ran hard, then we still lost money.
print("\n=== round-tripped runners (MFE >= 50% but booked a loss) ===")
bad = [(p, m, r) for p, m, r in closed if m >= 50 and p < 0]
print(f"count: {len(bad)}")
if bad:
    print(f"  mean PnL {st.mean([p for p, _, _ in bad]):+.1f}%   "
          f"mean peak was +{st.mean([m for _, m, _ in bad]):.0f}%")
    print(f"  worst: {min(p for p, _, _ in bad):+.0f}% after peaking "
          f"+{max(m for _, m, _ in bad):.0f}%")
    reasons: dict[str, int] = {}
    for _, _, reason in bad:
        key = (reason or "").split("(")[0].strip()[:34]
        reasons[key] = reasons.get(key, 0) + 1
    print("  exit reasons behind them:")
    for k, v in sorted(reasons.items(), key=lambda x: -x[1])[:10]:
        print(f"    {v:>5}  {k}")

# How often does a coin that peaks above +100% still end negative?
print("\n=== of coins that peaked >= +100%, how many still lost? ===")
hi = [(p, m) for p, m, _ in closed if m >= 100]
if hi:
    lost = sum(1 for p, _ in hi if p < 0)
    print(f"  n={len(hi)}  lost={lost} ({100*lost/len(hi):.0f}%)  "
          f"mean PnL {st.mean([p for p, _ in hi]):+.1f}%")

# Right-tail contribution: how concentrated is the profit?
print("\n=== profit concentration ===")
pnls = sorted((p for p, _, _ in closed), reverse=True)
total = sum(pnls)
for k in (10, 50, 100, 300):
    if k <= len(pnls):
        print(f"  top {k:>4} trades = {sum(pnls[:k]):>+10,.0f} points "
              f"({100*sum(pnls[:k])/total if total else 0:>5.0f}% of net)")
