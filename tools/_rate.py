"""How many trades does an arm actually produce per block? Sizes the final run."""
import json
from collections import defaultdict
from pathlib import Path

rows = []
for ln in Path("reports/experiments.jsonl").read_text(encoding="utf-8").splitlines():
    ln = ln.strip()
    if ln:
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            pass

agg: dict[str, list] = defaultdict(lambda: [0, 0.0])
for r in rows:
    if r.get("n"):
        agg[r.get("label", "")][0] += r["n"]
        agg[r.get("label", "")][1] += r.get("duration_s") or 480

print(f"{'arm':<22}{'trades':>8}{'mins':>8}{'per 480s':>10}")
print("-" * 48)
for k, v in sorted(agg.items(), key=lambda x: -x[1][0]):
    if v[1]:
        print(f"{k:<22}{v[0]:>8}{v[1]/60:>8.0f}{v[0]/v[1]*480:>10.1f}")

# Focus on the filtered arms, which are what the final test compares.
filt = {k: v for k, v in agg.items() if any(t in k for t in ("entry-1", "entry-2", "entry-3", "press"))}
if filt:
    tot_n = sum(v[0] for v in filt.values())
    tot_s = sum(v[1] for v in filt.values())
    print(f"\nfiltered arms combined: {tot_n} trades over {tot_s/60:.0f} min")
    print(f"  -> {tot_n/tot_s*60:.1f} trades per minute of arm-time")
    for target in (150, 200, 300):
        mins = target / (tot_n / tot_s * 60)
        print(f"  -> {target} trades needs {mins:.0f} min of arm-time")
