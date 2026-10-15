"""Run a ladder of bot experiments unattended, stopping early when the target is met.

Target = at least MIN_SAMPLE closed trades AND positive expectancy per trade.
Each experiment runs the bot in dry-run for DURATION seconds, scores only the trades
bought during that window, and appends the result to reports/experiments.jsonl.

The ladder is ordered by how promising the change looked in the earlier
feature study (entry gate = minimum buy SOL was the strongest single signal).

Usage:
    python tools/experiment_ladder.py [seconds_per_test]
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
RESULTS = REPO / "reports" / "experiments.jsonl"
MIN_SAMPLE = 200
MAX_TRADES = "600"  # keep the session cap well above the sample requirement

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 2400

# (label, env overrides). Order matters: most promising first.
LADDER: list[tuple[str, dict[str, str]]] = [
    ("01-control", {}),
    ("02-buysol-0.3", {"MIN_CONFIRM_BUY_SOL": "0.3"}),
    ("03-buysol-0.5", {"MIN_CONFIRM_BUY_SOL": "0.5"}),
    ("04-mc3000", {"MIN_CONFIRM_MC_USD": "3000"}),
    ("05-buysol-0.3-mc3000", {"MIN_CONFIRM_BUY_SOL": "0.3", "MIN_CONFIRM_MC_USD": "3000"}),
    ("06-buysol-0.3-buyers3", {"MIN_CONFIRM_BUY_SOL": "0.3", "MIN_UNIQUE_BUYERS": "3"}),
    ("07-buysol-0.3-tight", {"MIN_CONFIRM_BUY_SOL": "0.3",
                             "TAKE_PROFIT_TRIGGER_PCT": "10", "TRAILING_STOP_PCT": "8"}),
    ("08-buysol-0.3-breakeven2", {"MIN_CONFIRM_BUY_SOL": "0.3", "BREAKEVEN_ARM_PCT": "2"}),
]


def last_result() -> dict | None:
    if not RESULTS.exists():
        return None
    lines = [ln for ln in RESULTS.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else None


def main() -> None:
    print(f"ladder: {len(LADDER)} experiments x {DURATION}s "
          f"({len(LADDER) * DURATION / 3600:.1f}h max)", flush=True)
    for label, overrides in LADDER:
        env_overrides = {"MAX_TRADES": MAX_TRADES, **overrides}
        cmd = [str(PYTHON), "tools/run_experiment.py", label, str(DURATION)]
        cmd += [f"{k}={v}" for k, v in env_overrides.items()]
        print(f"\n>>> launching {label} ({overrides or 'no overrides'})", flush=True)
        subprocess.run(cmd, cwd=str(REPO))

        result = last_result()
        if not result or result.get("label") != label:
            print(f"!!! no result recorded for {label}; continuing", flush=True)
            continue

        n = result.get("n", 0)
        exp = result.get("expectancy_pct", 0.0)
        print(f"<<< {label}: n={n} expectancy={exp:+.2f}% net={result.get('net_sol')} SOL", flush=True)
        if n >= MIN_SAMPLE and exp > 0:
            print(f"\n*** TARGET MET by {label}: {exp:+.2f}%/trade over {n} trades ***", flush=True)
            return

    print("\n*** ladder finished without meeting the target ***", flush=True)


if __name__ == "__main__":
    main()
