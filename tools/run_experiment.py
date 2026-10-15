"""Run one bot experiment and record its result.

Runs the bot in dry-run for a fixed duration, then scores only the trades that were
*bought during this experiment* (matched on the recorded buy timestamp), so previous
runs cannot contaminate the sample. Appends a JSON line to reports/experiments.jsonl.

Usage:
    python tools/run_experiment.py <label> <seconds> [key=value ...]

Example:
    python tools/run_experiment.py entry-buysol-0.3 2700 MIN_CONFIRM_BUY_SOL=0.3
"""
from __future__ import annotations

import csv
import json
import os
import statistics as st
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
TRADES_CSV = REPO / "reports" / "trades.csv"
RESULTS = REPO / "reports" / "experiments.jsonl"
MIN_SAMPLE = 200
TARGET_EXPECTANCY = 0.0


def load_instrumented(since_iso: str) -> list[dict[str, str]]:
    if not TRADES_CSV.exists():
        return []
    with TRADES_CSV.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        r for r in rows
        if r["result"] in ("WON", "LOST")
        and (r.get("mfe_pct") or "").strip() != ""
        and (r.get("mae_pct") or "").strip() != ""
        and r["bought_at"] >= since_iso
    ]


def score(rows: list[dict[str, str]]) -> dict[str, float]:
    if not rows:
        return {}
    pnl = [float(r["pnl_pct"]) for r in rows]
    mfe = [float(r["mfe_pct"]) for r in rows]
    mae = [float(r["mae_pct"]) for r in rows]
    wins = [p for p in pnl if p >= 0]
    losses = [p for p in pnl if p < 0]
    invested = sum(float(r["invested"] or 0) for r in rows)
    returned = sum(float(r["exit_sol"] or 0) for r in rows)
    top_decile = sorted(mfe)[int(len(mfe) * 0.9):]
    return {
        "n": len(rows),
        "expectancy_pct": round(st.mean(pnl), 3),
        "median_pct": round(st.median(pnl), 3),
        "net_sol": round(returned - invested, 4),
        "win_rate_pct": round(len(wins) / len(rows) * 100, 1),
        "avg_win_pct": round(st.mean(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(st.mean(losses), 2) if losses else 0.0,
        "mean_mfe_pct": round(st.mean(mfe), 2),
        "mean_mae_pct": round(st.mean(mae), 2),
        "mfe_ge_10_pct": round(sum(1 for m in mfe if m >= 10) / len(mfe) * 100, 1),
        "top_decile_mfe": round(st.mean(top_decile), 1) if top_decile else 0.0,
    }


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    label = sys.argv[1]
    duration = float(sys.argv[2])
    overrides = dict(arg.split("=", 1) for arg in sys.argv[3:])

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    env.update(overrides)

    started = datetime.now()
    start_iso = started.strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== experiment {label} | {duration:.0f}s | overrides={overrides or 'none'} ===")
    print(f"started {start_iso}")

    log = REPO / "reports" / "logs"
    log.mkdir(parents=True, exist_ok=True)
    out_path = log / f"exp_{label}.log"
    with out_path.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [str(PYTHON), "-c", (
                "import asyncio, bot\n"
                f"async def r():\n"
                f"    t = asyncio.create_task(bot.main())\n"
                f"    await asyncio.sleep({duration})\n"
                f"    t.cancel()\n"
                f"    try:\n        await t\n    except asyncio.CancelledError:\n        pass\n"
                "asyncio.run(r())\n"
            )],
            cwd=str(REPO), env=env, stdout=handle, stderr=subprocess.STDOUT,
        )
        print(f"bot pid={proc.pid}; waiting {duration:.0f}s")
        try:
            proc.wait(timeout=duration + 120)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    print(f"finished at {datetime.now():%Y-%m-%d %H:%M:%S}; log={out_path.name}")

    rows = load_instrumented(start_iso)
    metrics = score(rows)
    result = {
        "label": label,
        "started": start_iso,
        "duration_s": duration,
        "overrides": overrides,
        **metrics,
    }
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result) + "\n")

    print("\n== result ==")
    for key, value in metrics.items():
        print(f"  {key:18} {value}")
    if metrics.get("n", 0) >= MIN_SAMPLE:
        verdict = "TARGET MET" if metrics["expectancy_pct"] > TARGET_EXPECTANCY else "below target"
        print(f"  sample OK ({metrics['n']} >= {MIN_SAMPLE}) -> {verdict}")
    else:
        print(f"  sample too small ({metrics.get('n', 0)} < {MIN_SAMPLE}); extend the run")


if __name__ == "__main__":
    main()
