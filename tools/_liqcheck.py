"""Are the liquidity blocks stale, or firing right now despite MIN_LIQUIDITY_USD=0?"""
import csv
import re
from collections import Counter
from pathlib import Path

rows = list(csv.DictReader(Path("reports/blocked.csv").open(encoding="utf-8", errors="replace")))
print(f"total blocked rows: {len(rows)}")
times = sorted(r.get("time", "") for r in rows if r.get("time"))
print(f"first: {times[0]}   last: {times[-1]}")


def reasons(r):
    raw = r.get("reasons") or ""
    after = raw.split("BLOCK", 1)[1] if "BLOCK" in raw else ""
    return [p.strip() for p in after.split(";") if p.strip()]


print("\n=== blocks in the last 2 hours of data ===")
recent = [r for r in rows if (r.get("time") or "") >= times[-1][:11] + "20:00:00"]
print(f"rows after 20:00: {len(recent)}")
c: Counter[str] = Counter()
for r in recent:
    for part in reasons(r):
        c[re.sub(r"[-+]?\d[\d,\.]*", "N", part)[:50]] += 1
for k, v in c.most_common(15):
    print(f"  {v:>6}  {k}")

print("\n=== any liquidity-based block after 20:00? ===")
liq = [r for r in recent if "liquidity" in (r.get("reasons") or "").lower()
       and "BLOCK" in (r.get("reasons") or "") and "liquidity" in (r.get("reasons") or "").split("BLOCK", 1)[1].lower()]
print(f"  {len(liq)}")
for r in liq[:3]:
    print("   ", r.get("time"), (r.get("reasons") or "")[:150])
