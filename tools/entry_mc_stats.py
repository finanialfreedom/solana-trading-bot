"""Summarise the market caps the bot actually enters at, from CONFIRM log lines."""
import glob
import re
import statistics as st

import config

print("ladder:", len(config.MC_SCALE_OUT), "levels")
for mc, pct in config.MC_SCALE_OUT:
    print(f"   ${int(mc):>10,}  sell {pct:.0f}% of remaining")

vals: list[int] = []
for path in glob.glob("reports/logs/exp_*.log"):
    for line in open(path, encoding="utf-8", errors="ignore"):
        m = re.search(r"CONFIRM\b.*?mc=\$([0-9,]+)", line)
        if m:
            vals.append(int(m.group(1).replace(",", "")))

print("\n--- entry market caps seen at probe time ---")
if not vals:
    print("no CONFIRM samples found")
else:
    vals.sort()
    print(f"samples: {len(vals)}")
    print(f"min:     ${vals[0]:,}")
    print(f"median:  ${int(st.median(vals)):,}")
    print(f"mean:    ${int(st.mean(vals)):,}")
    print(f"p90:     ${vals[int(len(vals) * 0.9)]:,}")
    print(f"max:     ${vals[-1]:,}")
    for cap in (5_000, 10_000, 20_000, 69_000):
        pct = 100 * sum(1 for v in vals if v < cap) / len(vals)
        print(f"under ${cap:,}: {pct:.0f}%")
