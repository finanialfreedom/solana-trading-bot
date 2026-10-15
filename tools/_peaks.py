"""Do modest peaks leak because nothing protects them?

Trailing arms at a +30% peak and the first ratchet tier is at +50%, so a position
peaking in [+10%, +30%) has no rule holding its gain. Check what actually exits them.
"""
import csv
import re
import statistics as st
from collections import Counter
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


def band(lo, hi):
    out = []
    for r in rows:
        m, p = num(r, "mfe_pct"), num(r, "pnl_pct")
        if m is None or p is None or m == "" or p == "":
            continue
        if lo <= m < hi:
            out.append((p, m, r.get("reason", "")))
    return out


for lo, hi in ((10, 30), (30, 50), (50, 100)):
    grp = band(lo, hi)
    if not grp:
        continue
    mp = st.mean([p for p, _, _ in grp])
    mm = st.mean([m for _, m, _ in grp])
    print(f"\n=== MFE {lo}-{hi}%   n={len(grp)}   mean peak +{mm:.1f}%   "
          f"mean booked {mp:+.1f}%   capture {100*mp/mm:.0f}% ===")
    c: Counter[str] = Counter()
    for _, _, reason in grp:
        key = re.sub(r"[-+]?\d+\.?\d*%?", "", (reason or "").split("(")[0]).strip()
        c[key] += 1
    for k, v in c.most_common(7):
        print(f"   {v:>5}  {k[:46]}")
    # How many of these were booked at a loss despite a decent peak?
    lost = sum(1 for p, _, _ in grp if p < 0)
    print(f"   booked a LOSS anyway: {lost} ({100*lost/len(grp):.0f}%)")

# Would a simple floor have helped? Replay: lock (peak-8) once peak >= 12.
print("\n=== replay: once peak >= 12%, never book below +5% ===")
lock_peak, lock_floor = 12.0, 5.0
helped = hurt = 0
delta = 0.0
for r in rows:
    m, p = num(r, "mfe_pct"), num(r, "pnl_pct")
    if m is None or p is None:
        continue
    if m >= lock_peak and p < lock_floor:
        delta += lock_floor - p
        helped += 1
    elif m < lock_peak and p > lock_floor:
        # a coin that never reached the arm level cannot be affected
        pass
print(f"  trades where the floor would have raised the booked result: {helped}")
print(f"  pnl-points added (upper bound, ignores coins that would then run): {delta:+,.0f}")
