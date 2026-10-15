"""Quantify the zero-proceeds exits.

A sell that returns nothing is not a market outcome, it is an execution or a
price-read failure that got booked as a total loss. Measure how much that costs.
"""
import csv
import re
from collections import Counter
from pathlib import Path

rows = [
    r for r in csv.DictReader(Path("reports/trades.csv").open(encoding="utf-8", errors="replace"))
    if r.get("result") in ("WON", "LOST")
]


def f(r, k):
    try:
        return float(r[k] or 0)
    except (TypeError, ValueError):
        return 0.0


zero = [r for r in rows if f(r, "exit_sol") == 0.0]
net_all = sum(f(r, "pnl_sol") for r in rows)
big_loss = [r for r in rows if f(r, "pnl_pct") <= -90 and f(r, "exit_sol") != 0.0]

print(f"closed trades             : {len(rows)}")
print(f"exited for EXACTLY 0 SOL  : {len(zero)}  ({100*len(zero)/len(rows):.1f}%)")
print(f"SOL written off on them   : {sum(f(r, 'pnl_sol') for r in zero):+.2f} SOL")
print(f"net SOL across all trades : {net_all:+.2f} SOL")
print(f"deep losses NOT from 0 SOL: {len(big_loss)}")
print()

c: Counter[str] = Counter()
for r in zero:
    key = re.sub(r"[-+]?\d+\.?\d*%?", "", (r.get("reason") or "").split("(")[0]).strip()
    c[key] += 1
print("why they hit 0 SOL:")
for k, v in c.most_common(12):
    print(f"  {v:>5}  {k[:62]}")

print()
print("=== impact of booking these at a -3% stop instead of -100% ===")
invested = sum(f(r, "invested") for r in zero)
booked = sum(f(r, "pnl_sol") for r in zero)
print(f"  capital at stake       : {invested:.2f} SOL")
print(f"  currently booked       : {booked:+.2f} SOL")
print(f"  at a -3% stop          : {-0.03*invested:+.2f} SOL")
print(f"  swing if fully fixed   : {-0.03*invested - booked:+.2f} SOL")
print(f"  net SOL would become   : {net_all - booked - 0.03*invested:+.2f} SOL "
      f"(from {net_all:+.2f})")
print()

# Time pattern: is it worse recently?
print("=== zero-SOL exits per hour of day ===")
per_hour: Counter[str] = Counter()
tot_hour: Counter[str] = Counter()
for r in rows:
    h = (r.get("sold_at") or "")[11:13]
    tot_hour[h] += 1
    if f(r, "exit_sol") == 0.0:
        per_hour[h] += 1
for h in sorted(tot_hour):
    if tot_hour[h] >= 20:
        print(f"  {h}h  {per_hour[h]:>4}/{tot_hour[h]:<4} ({100*per_hour[h]/tot_hour[h]:>5.1f}%)")

print()
print("=== the 10 most recent zero-SOL exits ===")
for r in zero[-10:]:
    print(f"  {(r.get('sold_at') or '')[:19]}  {r.get('symbol','')[:10]:<11} "
          f"{r.get('invested'):>4} SOL  {(r.get('reason') or '')[:46]}")
