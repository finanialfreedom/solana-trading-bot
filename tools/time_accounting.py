"""Compute total research time: hours spent so far and hours left until the report."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOGS = REPO / "reports" / "logs"

FMT = "%Y-%m-%d %H:%M:%S"


def parse(s: str) -> datetime | None:
    try:
        return datetime.strptime(s.strip(), FMT)
    except Exception:
        return None


now = datetime.now()

# ---- hours spent ----------------------------------------------------------
rows = []
for line in (REPO / "reports" / "experiments.jsonl").read_text(encoding="utf-8").splitlines():
    if line.strip():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass

starts = [parse(r.get("started", "")) for r in rows]
starts = [s for s in starts if s]
block_hours = sum(r.get("duration_s", 0) for r in rows) / 3600.0

# Earliest evidence of the research campaign: oldest experiment log or experiment row.
candidates = [s for s in starts]
for p in LOGS.glob("exp_*.log"):
    candidates.append(datetime.fromtimestamp(p.stat().st_ctime))
first_work = min(candidates) if candidates else now

elapsed_wall = (now - first_work).total_seconds() / 3600.0

# ---- hours left in the current program ------------------------------------
plan = [
    ("slippage", 3, 2, 1800),
    ("threshold", 4, 2, 1800),
    ("protect2", 4, 2, 1800),
    ("ladder", 3, 2, 1800),
    ("final", 3, 2, 1800),
]
total_plan_h = sum(arms * reps * secs / 3600.0 for _, arms, reps, secs in plan)

done = [ln.strip() for ln in (LOGS / "program_state.txt").read_text(encoding="utf-8").splitlines() if ln.strip()]
started_marker = parse((LOGS / "program_started.txt").read_text(encoding="utf-8")) if (LOGS / "program_started.txt").exists() else None

elapsed_this_run = (now - started_marker).total_seconds() / 3600.0 if started_marker else 0.0
remaining = max(0.0, total_plan_h - elapsed_this_run)

# ---- report ---------------------------------------------------------------
print("=" * 66)
print("RESEARCH TIME ACCOUNTING")
print("=" * 66)
print(f"now                        {now:%Y-%m-%d %H:%M:%S}")
print()
print(f"first research activity    {first_work:%Y-%m-%d %H:%M:%S}")
print(f"experiment blocks recorded {len(rows)}")
print(f"sum of block durations     {block_hours:.2f} h")
print(f"wall-clock so far          {elapsed_wall:.2f} h")
print()
print(f"current program started    {started_marker}")
print(f"elapsed in current program {elapsed_this_run:.2f} h")
print(f"total plan length          {total_plan_h:.2f} h")
for name, arms, reps, secs in plan:
    h = arms * reps * secs / 3600.0
    state = "DONE" if name in done else ("FAILED" if f"{name}-ABANDONED" in done else "pending")
    print(f"    {name:<10} {arms} arms x {reps} reps x {secs//60} min = {h:>5.1f} h   {state}")
print(f"stages completed           {done if done else 'none yet'}")
print()
print(f"remaining                  {remaining:.2f} h")
print()
print(f"TOTAL SPENT {elapsed_wall:.1f} h  +  REMAINING {remaining:.1f} h"
      f"  =  {elapsed_wall + remaining:.1f} h END TO END")
if started_marker:
    finish = started_marker.timestamp() + total_plan_h * 3600
    print(f"estimated finish    {datetime.fromtimestamp(finish):%Y-%m-%d %H:%M}")
print("=" * 66)
