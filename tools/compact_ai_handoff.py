"""Create one compact, idempotent AI-analysis request after a sweep completes.

The handoff reads only reports/experiments.jsonl. It never reads trades.csv or raw
logs, so an AI consumer can analyze the sweep without receiving high-volume context.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "reports" / "experiments.jsonl"
OUT_DIR = REPO / "reports" / "ai"
REQUEST_DIR = OUT_DIR / "requests"


def _base_label(label: str) -> str:
    head, sep, tail = label.rpartition("-r")
    return head if sep and tail.isdigit() else label


def _load_latest(labels: list[str], reps: int) -> list[dict]:
    if not RESULTS.exists():
        return []
    expected = {f"{label}-r{rep}" for label in labels for rep in range(1, reps + 1)}
    latest: dict[str, dict] = {}
    for line in RESULTS.read_text(encoding="utf-8-sig").splitlines():
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        label = str(row.get("label", ""))
        if label in expected and (
            label not in latest
            or str(row.get("started", "")) > str(latest[label].get("started", ""))
        ):
            latest[label] = row
    return [latest[label] for label in sorted(latest)]


def _weighted(rows: list[dict], key: str) -> float:
    total = sum(int(row.get("n") or 0) for row in rows)
    if not total:
        return 0.0
    return sum(float(row.get(key) or 0) * int(row.get("n") or 0) for row in rows) / total


def compact_summary(stage: str, labels: list[str], reps: int) -> dict:
    rows = _load_latest(labels, reps)
    arms = []
    for label in labels:
        blocks = [row for row in rows if _base_label(str(row.get("label", ""))) == label]
        if not blocks:
            continue
        arms.append(
            {
                "label": label,
                "blocks": len(blocks),
                "trades": sum(int(row.get("n") or 0) for row in blocks),
                "mean_return_pct": round(_weighted(blocks, "expectancy_pct"), 3),
                "median_return_pct": round(_weighted(blocks, "median_pct"), 3),
                "win_rate_pct": round(_weighted(blocks, "win_rate_pct"), 1),
                "avg_win_pct": round(_weighted(blocks, "avg_win_pct"), 2),
                "avg_loss_pct": round(_weighted(blocks, "avg_loss_pct"), 2),
                "net_sol": round(sum(float(row.get("net_sol") or 0) for row in blocks), 4),
                "overrides": blocks[-1].get("overrides", {}),
            }
        )
    return {
        "stage": stage,
        "status": "complete",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "reports/experiments.jsonl only",
        "arms": arms,
    }


def write_handoff(stage: str, labels: list[str], reps: int) -> tuple[Path, Path, bool]:
    summary = compact_summary(stage, labels, reps)
    if len(summary["arms"]) != len(labels):
        missing = sorted(set(labels) - {arm["label"] for arm in summary["arms"]})
        raise RuntimeError(f"cannot request analysis; missing completed results: {missing}")

    fingerprint_data = json.dumps(summary["arms"], sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(fingerprint_data.encode()).hexdigest()[:16]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REQUEST_DIR.mkdir(parents=True, exist_ok=True)

    summary_path = OUT_DIR / f"{stage}_summary.json"
    request_path = REQUEST_DIR / f"{stage}_{fingerprint}.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    created = not request_path.exists()
    if created:
        request = {
            "request": "analyze_completed_sweep",
            "stage": stage,
            "fingerprint": fingerprint,
            "summary_path": str(summary_path.relative_to(REPO)).replace("\\", "/"),
            "context_policy": "Use only the compact summary; do not attach logs or trades.csv.",
        }
        request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
    return summary_path, request_path, created


def main() -> None:
    if len(sys.argv) < 4:
        sys.exit("usage: compact_ai_handoff.py <stage> <reps> <arm-label> [...]")
    stage = sys.argv[1]
    reps = int(sys.argv[2])
    summary, request, created = write_handoff(stage, sys.argv[3:], reps)
    state = "created" if created else "already exists"
    print(f"compact AI summary: {summary}")
    print(f"single completion request ({state}): {request}")


if __name__ == "__main__":
    main()
