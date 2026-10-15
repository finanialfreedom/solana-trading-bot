"""What are the security/entry filters actually rejecting, and does it correlate
with what would have lost money?"""
import csv
import re
from collections import Counter
from pathlib import Path

p = Path("reports/blocked.csv")
print("header:", p.open(encoding="utf-8", errors="replace").readline().strip())
rows = list(csv.DictReader(p.open(encoding="utf-8", errors="replace")))
print(f"blocked rows: {len(rows)}\n")

# Block reasons
key = None
for cand in ("reason", "reasons", "blocked_reason", "detail"):
    if rows and cand in rows[0]:
        key = cand
        break
print("reason column:", key)
if key:
    c: Counter[str] = Counter()
    for r in rows:
        raw = r.get(key) or ""
        # The column is "1- Liquidity ... | 2- Total Locked Liquidity ... | BLOCK <why>".
        after = raw.split("BLOCK", 1)[1] if "BLOCK" in raw else raw
        for part in after.split(";"):
            base = re.sub(r"[-+]?\d[\d,\.]*", "N", part).strip()
            if base:
                c[base[:56]] += 1
    print("\nreal block reasons:")
    for k, v in c.most_common(20):
        print(f"  {v:>6}  {k}")

print("\nsample rows:")
for r in rows[-3:]:
    print("  ", {k: (v[:40] if isinstance(v, str) else v) for k, v in list(r.items())[:8]})

# Creator history: how often does the rug filter have teeth?
h = Path("reports/creator_history.json")
if h.exists():
    import json
    d = json.load(h.open())
    ruggy = [k for k, v in d.items() if v.get("rugs", 0) >= 1]
    serial = [k for k, v in d.items() if v.get("launches", 0) > 5]
    multirug = [k for k, v in d.items() if v.get("rugs", 0) >= 2]
    print(f"\ncreator history: {len(d)} creators")
    print(f"  with >=1 rug     : {len(ruggy)} ({100*len(ruggy)/len(d):.1f}%)")
    print(f"  with >=2 rugs    : {len(multirug)} ({100*len(multirug)/len(d):.1f}%)")
    print(f"  with >5 launches : {len(serial)} ({100*len(serial)/len(d):.1f}%)")
    tot_rugs = sum(v.get("rugs", 0) for v in d.values())
    print(f"  total rug events : {tot_rugs}")
