"""Resumably rerun official sweep arms until each has enough closed trades.

Existing valid counts come from the compact stage summaries. New 30-minute blocks
use unique labels in experiments.jsonl, so an interrupted process can reconstruct
progress without double-counting or starting over.

Usage:
    python tools/run_to_sample.py [target_trades] [block_seconds]
    python tools/run_to_sample.py --status
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from interleaved_sweep import (
    ENTRY_SCHEDULE,
    MAX_TRADES,
    PRESSURE_SCHEDULE,
    PYTHON,
    QUALITY_SCHEDULE,
)

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "reports" / "experiments.jsonl"
STATE = REPO / "reports" / "sample100_state.json"
SUMMARY = REPO / "reports" / "sample100_summary.json"
LOG = REPO / "reports" / "logs" / "sample100_runner.log"

STAGES = {
    "quality": QUALITY_SCHEDULE,
    "entry": ENTRY_SCHEDULE,
    "pressure": PRESSURE_SCHEDULE,
}


def log(message: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_results() -> list[dict]:
    if not RESULTS.exists():
        return []
    rows = []
    for line in RESULTS.read_text(encoding="utf-8-sig").splitlines():
        try:
            rows.append(json.loads(line))
        except (json.JSONDecodeError, TypeError):
            continue
    return rows


def initial_state(target: int, block_seconds: int) -> dict:
    arms: dict[str, dict] = {}
    for stage, schedule in STAGES.items():
        summary_path = REPO / "reports" / "ai" / f"{stage}_summary.json"
        summary = load_json(summary_path)
        counts = {arm["label"]: int(arm.get("trades") or 0) for arm in summary["arms"]}
        for label, overrides in schedule:
            if label not in counts:
                raise RuntimeError(f"{summary_path} has no valid seed count for {label}")
            key = f"{stage}/{label}"
            arms[key] = {
                "stage": stage,
                "label": label,
                "seed_trades": counts[label],
                "overrides": overrides,
            }
    return {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "target_trades": target,
        "block_seconds": block_seconds,
        "arms": arms,
    }


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def load_or_create_state(target: int, block_seconds: int) -> dict:
    if STATE.exists():
        state = load_json(STATE)
        if int(state["target_trades"]) != target:
            raise RuntimeError(
                f"existing state targets {state['target_trades']} trades, not {target}"
            )
        if int(state["block_seconds"]) != block_seconds:
            raise RuntimeError(
                f"existing state uses {state['block_seconds']}s blocks, not {block_seconds}s"
            )
        expected = {
            f"{stage}/{label}"
            for stage, schedule in STAGES.items()
            for label, _overrides in schedule
        }
        if set(state["arms"]) != expected:
            raise RuntimeError("official sweep schedule changed after sample run began")
        return state
    state = initial_state(target, block_seconds)
    save_state(state)
    return state


def run_prefix(arm: dict) -> str:
    return f"sample100-{arm['stage']}-{arm['label']}-r"


def added_rows(arm: dict, rows: list[dict]) -> list[dict]:
    prefix = run_prefix(arm)
    return [row for row in rows if str(row.get("label", "")).startswith(prefix)]


def arm_progress(arm: dict, rows: list[dict]) -> dict:
    additions = added_rows(arm, rows)
    added = sum(int(row.get("n") or 0) for row in additions)
    indices = []
    prefix = re.escape(run_prefix(arm))
    for row in additions:
        match = re.fullmatch(prefix + r"(\d+)", str(row.get("label", "")))
        if match:
            indices.append(int(match.group(1)))
    return {
        "seed": int(arm["seed_trades"]),
        "added": added,
        "total": int(arm["seed_trades"]) + added,
        "reruns": len(additions),
        "next_index": max(indices, default=0) + 1,
    }


def snapshot(state: dict, rows: list[dict]) -> dict:
    arms = []
    for key, arm in state["arms"].items():
        progress = arm_progress(arm, rows)
        arms.append(
            {
                "key": key,
                "stage": arm["stage"],
                "label": arm["label"],
                "seed_trades": progress["seed"],
                "added_trades": progress["added"],
                "final_trades": progress["total"],
                "reruns": progress["reruns"],
                "target_met": progress["total"] >= int(state["target_trades"]),
            }
        )
    return {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "target_trades": int(state["target_trades"]),
        "block_seconds": int(state["block_seconds"]),
        "complete": all(arm["target_met"] for arm in arms),
        "arms": arms,
    }


def print_status(report: dict) -> None:
    print(
        f"target={report['target_trades']} block={report['block_seconds']}s "
        f"complete={report['complete']}"
    )
    for arm in report["arms"]:
        print(
            f"{arm['stage']:<8} {arm['label']:<24} "
            f"{arm['final_trades']:>4}/{report['target_trades']} trades  "
            f"reruns={arm['reruns']}"
        )


def main() -> None:
    status_only = len(sys.argv) > 1 and sys.argv[1] == "--status"
    target = 100 if status_only or len(sys.argv) < 2 else int(sys.argv[1])
    block_seconds = 1800 if status_only or len(sys.argv) < 3 else int(sys.argv[2])
    state = load_or_create_state(target, block_seconds)

    if status_only:
        print_status(snapshot(state, read_results()))
        return

    log(f"sample runner start: target={target}, block={block_seconds}s")
    while True:
        rows = read_results()
        report = snapshot(state, rows)
        SUMMARY.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if report["complete"]:
            log("all official quality, entry, and pressure arms reached target")
            print_status(report)
            return

        # One block per deficient arm per cycle keeps market-time exposure interleaved.
        for key, arm in state["arms"].items():
            rows = read_results()
            progress = arm_progress(arm, rows)
            if progress["total"] >= target:
                continue

            run_label = f"{run_prefix(arm)}{progress['next_index']:03d}"
            overrides = {"MAX_TRADES": MAX_TRADES, **arm["overrides"]}
            cmd = [
                str(PYTHON),
                "tools/run_experiment.py",
                run_label,
                str(block_seconds),
                *(f"{name}={value}" for name, value in overrides.items()),
            ]
            log(
                f"start {key} {run_label}: "
                f"{progress['total']}/{target} trades"
            )
            result = subprocess.run(cmd, cwd=str(REPO))
            if result.returncode != 0:
                log(f"ERROR {run_label}: exit {result.returncode}; retry in 60s")
                time.sleep(60)
                continue

            rows = read_results()
            exact = [row for row in rows if row.get("label") == run_label]
            if not exact:
                log(f"ERROR {run_label}: no result row; retry in 60s")
                time.sleep(60)
                continue
            count = int(exact[-1].get("n") or 0)
            total = arm_progress(arm, rows)["total"]
            log(f"finish {run_label}: +{count} trades, total={total}/{target}")


if __name__ == "__main__":
    main()
