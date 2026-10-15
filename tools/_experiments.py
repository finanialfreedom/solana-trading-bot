"""Every sweep result so far, grouped by experiment family."""
import json
import re
import statistics as st
from collections import defaultdict
from pathlib import Path

rows = []
for ln in Path("reports/experiments.jsonl").read_text(encoding="utf-8").splitlines():
    ln = ln.strip()
    if not ln:
        continue
    try:
        rows.append(json.loads(ln))
    except json.JSONDecodeError:
        continue

print(f"experiment records: {len(rows)}")


def base(label: str) -> str:
    return re.sub(r"-r\d+$", "", label or "")


agg: dict[str, dict] = defaultdict(
    lambda: {"n": 0, "w": 0.0, "blocks": [], "net": 0.0, "mfe": [], "wr": [], "inv": 0.0}
)
for r in rows:
    if not r.get("n"):
        continue
    a = agg[base(r.get("label", ""))]
    a["n"] += r["n"]
    a["w"] += r.get("expectancy_pct", 0.0) * r["n"]
    a["blocks"].append(r.get("expectancy_pct", 0.0))
    a["net"] += r.get("net_sol", 0.0)
    a["inv"] += r.get("invested_sol", 0.0)
    a["wr"].append(r.get("win_rate_pct", 0.0))
    a["mfe"].append(r.get("mean_mfe_pct", 0.0))

print(f"\n{'config':<22}{'n':>6}{'exp%':>9}{'win%':>7}{'netSOL':>9}{'std':>7}{'blocks':>8}")
print("-" * 68)
for label in sorted(agg, key=lambda k: -agg[k]["n"] / max(1, len(agg[k]["blocks"])) if agg[k]["blocks"] else 0):
    a = agg[label]
    if a["n"] < 30:
        continue
    exp = a["w"] / a["n"]
    wr = st.mean(a["wr"]) if a["wr"] else 0.0
    sd = st.pstdev(a["blocks"]) if len(a["blocks"]) > 1 else 0.0
    print(f"{label[:21]:<22}{a['n']:>6}{exp:>+8.2f}%{wr:>6.1f}%{a['net']:>+9.2f}"
          f"{sd:>7.2f}{len(a['blocks']):>8}")
