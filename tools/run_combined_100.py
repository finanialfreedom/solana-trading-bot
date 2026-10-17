"""Run the active combined strategy until 100 new trades have closed.

Progress is resumable through reports/combined100_state.json. The measured duration
is active bot runtime, not time spent stopped between restarts.
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
TRADES = REPO / "reports" / "trades.csv"
EXPERIMENTS = REPO / "reports" / "experiments.jsonl"
STATE = REPO / "reports" / "combined100_state.json"
SUMMARY = REPO / "reports" / "combined100_summary.json"
TARGET = 100
POLL_SECONDS = 5
MAX_ACTIVE_SECONDS = 48 * 3600


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    state = {
        "status": "running",
        "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "target_trades": TARGET,
        "active_seconds": 0.0,
    }
    save_state(state)
    return state


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def closed_rows(since: str) -> list[dict[str, str]]:
    if not TRADES.exists():
        return []
    with TRADES.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        row
        for row in rows
        if row.get("bought_at", "") >= since
        and row.get("result") in ("WON", "LOST")
        and (row.get("mfe_pct") or "").strip()
        and (row.get("mae_pct") or "").strip()
    ]


async def run() -> None:
    state = load_state()
    if state.get("status") == "complete":
        print(SUMMARY.read_text(encoding="utf-8"))
        return

    existing = len(closed_rows(state["started"]))
    remaining = max(0, TARGET - existing)
    # Permit a few simultaneously open positions when the 100th trade closes.
    os.environ["MAX_TRADES"] = str(max(remaining + 10, 10))

    # Import only after the session trade cap is set.
    import bot  # noqa: PLC0415
    import config  # noqa: PLC0415
    from report import REPORT  # noqa: PLC0415
    from run_experiment import score  # noqa: PLC0415

    settings = {
        "MIN_UNIQUE_BUYERS": config.MIN_UNIQUE_BUYERS,
        "MIN_CONFIRM_BUY_SOL": config.MIN_CONFIRM_BUY_SOL,
        "MIN_CONFIRM_MC_USD": config.MIN_CONFIRM_MC_USD,
        "MIN_CONFIRM_NET_SOL": config.MIN_CONFIRM_NET_SOL,
        "MAX_CONFIRM_SELL_RATIO": config.MAX_CONFIRM_SELL_RATIO,
        "SIM_SLIPPAGE_BPS": config.SIM_SLIPPAGE_BPS,
        "STOP_LOSS_PCT": config.STOP_LOSS_PCT,
        "MAX_SOL_PER_TRADE": config.MAX_SOL_PER_TRADE,
        "MAX_COMBINED_OPEN_RISK_SOL": config.MAX_COMBINED_OPEN_RISK_SOL,
        "REGIME_FILTER_ENABLED": config.REGIME_FILTER_ENABLED,
    }
    print(f"combined-100 start={state['started']} existing={existing} settings={settings}")

    task = asyncio.create_task(bot.main())
    session_started = time.monotonic()
    last_count = -1
    completed = False
    try:
        while True:
            await asyncio.sleep(POLL_SECONDS)
            count = len(closed_rows(state["started"]))
            if count != last_count:
                active = float(state.get("active_seconds", 0)) + (
                    time.monotonic() - session_started
                )
                print(
                    f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
                    f"closed={count}/{TARGET} active_hours={active / 3600:.2f}",
                    flush=True,
                )
                last_count = count
            if count >= TARGET:
                completed = True
                break
            active = float(state.get("active_seconds", 0)) + (
                time.monotonic() - session_started
            )
            if active >= MAX_ACTIVE_SECONDS:
                raise TimeoutError("combined strategy did not reach 100 trades in 48 active hours")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        state["active_seconds"] = round(
            float(state.get("active_seconds", 0)) + time.monotonic() - session_started,
            3,
        )
        save_state(state)
        REPORT.flush()
        await asyncio.sleep(1)

    if not completed:
        return

    rows = closed_rows(state["started"])
    metrics = score(rows)
    finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = {
        "label": "combined-strategy-100",
        "status": "complete",
        "started": state["started"],
        "finished": finished,
        "active_seconds": state["active_seconds"],
        "active_hours": round(state["active_seconds"] / 3600, 3),
        "target_trades": TARGET,
        "settings": settings,
        **metrics,
    }
    SUMMARY.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    with EXPERIMENTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary) + "\n")
    state.update({"status": "complete", "finished": finished})
    save_state(state)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("combined-100 stopped; progress is resumable", file=sys.stderr)
