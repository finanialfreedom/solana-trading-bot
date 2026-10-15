"""Why do positions that never rise still lose far more than the 3% stop?

MFE < 10% should mean the position barely moved, and STOP_LOSS_PCT = 3.0 should cap
the damage near -3%. The observed mean is about -14%. Find the leak.
"""
import csv
import statistics as st
from collections import Counter, defaultdict
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


flat = []
for r in rows:
    p, m = num(r, "pnl_pct"), num(r, "mfe_pct")
    if p is None or m is None:
        continue
    if 0 <= m < 10:
        flat.append((p, m, num(r, "mae_pct") or 0.0, r.get("reason", ""), num(r, "invested") or 0.0))

print(f"flat band (0<=MFE<10): {len(flat)} trades")

# Distribution of realised PnL -- where is the mass?
buckets = [
    ("<= -90%", lambda p: p <= -90),
    ("-90..-50%", lambda p: -90 < p <= -50),
    ("-50..-20%", lambda p: -50 < p <= -20),
    ("-20..-10%", lambda p: -20 < p <= -10),
    ("-10..-5%", lambda p: -10 < p <= -5),
    ("-5..-3%", lambda p: -5 < p <= -3),
    ("-3..0%", lambda p: -3 < p <= 0),
    (">= 0%", lambda p: p > 0),
]
print(f"\n{'pnl bucket':<12}{'n':>6}{'share':>8}{'mean':>9}{'total pts':>12}{'mean SOL in':>12}")
print("-" * 59)
for label, test in buckets:
    grp = [x for x in flat if test(x[0])]
    if not grp:
        continue
    tot = sum(x[0] for x in grp)
    print(f"{label:<12}{len(grp):>6}{100*len(grp)/len(flat):>7.0f}%"
          f"{st.mean([x[0] for x in grp]):>8.1f}%{tot:>+12,.0f}"
          f"{st.mean([x[4] for x in grp])/1e9:>12.3f}")

# Mean MAE vs mean PnL: are we realising much worse than the worst tick we saw?
print(f"\nmean realised PnL : {st.mean([x[0] for x in flat]):+.2f}%")
print(f"mean MAE seen     : {st.mean([x[2] for x in flat]):+.2f}%")
print("  -> if PnL is far worse than MAE, the loss is happening at exit, not in the mark")

# Which exits produce the deep losses?
print("\n=== exit reasons within the flat band, by mean PnL ===")
byreason = defaultdict(list)
for p, _, _, reason, _ in flat:
    key = (reason or "").split("(")[0].strip()
    # collapse trailing numbers so reasons group
    import re
    key = re.sub(r"[-+]?\d+\.?\d*%?", "", key).strip()
    byreason[key].append(p)
print(f"{'reason':<38}{'n':>6}{'mean':>9}{'total':>12}")
print("-" * 65)
for k, v in sorted(byreason.items(), key=lambda x: sum(x[1]))[:14]:
    print(f"{k[:37]:<38}{len(v):>6}{st.mean(v):>8.1f}%{sum(v):>+12,.0f}")

# Worst single contributors
print("\n=== the 12 worst flat-band trades ===")
for p, m, mae, reason, inv in sorted(flat, key=lambda x: x[0])[:12]:
    print(f"  {p:>8.1f}%  mfe={m:>5.1f}%  mae={mae:>6.1f}%  in={inv/1e9:.3f}  {reason[:52]}")
