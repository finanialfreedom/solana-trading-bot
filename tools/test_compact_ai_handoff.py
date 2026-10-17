"""Regression checks for the completion-only compact AI handoff."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import compact_ai_handoff as handoff


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        results = root / "experiments.jsonl"
        rows = [
            {
                "label": "A-r1", "started": "2026-01-01 00:00:00", "n": 10,
                "expectancy_pct": 1, "median_pct": -1, "win_rate_pct": 30,
                "avg_win_pct": 10, "avg_loss_pct": -4, "net_sol": 0.1,
                "overrides": {"X": "1"},
            },
            {
                "label": "A-r1", "started": "2026-01-02 00:00:00", "n": 20,
                "expectancy_pct": 2, "median_pct": -2, "win_rate_pct": 40,
                "avg_win_pct": 12, "avg_loss_pct": -5, "net_sol": 0.2,
                "overrides": {"X": "2"},
            },
            {
                "label": "B-r1", "started": "2026-01-02 00:30:00", "n": 30,
                "expectancy_pct": 3, "median_pct": -3, "win_rate_pct": 50,
                "avg_win_pct": 14, "avg_loss_pct": -6, "net_sol": 0.3,
                "overrides": {"X": "3"},
            },
        ]
        results.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

        original_repo, original_results, original_out, original_requests = (
            handoff.REPO, handoff.RESULTS, handoff.OUT_DIR, handoff.REQUEST_DIR
        )
        handoff.REPO = root
        handoff.RESULTS = results
        handoff.OUT_DIR = root / "ai"
        handoff.REQUEST_DIR = handoff.OUT_DIR / "requests"
        try:
            summary_path, request_path, first = handoff.write_handoff(
                "quality", ["A", "B"], 1
            )
            _, repeated_path, second = handoff.write_handoff("quality", ["A", "B"], 1)
        finally:
            handoff.REPO, handoff.RESULTS, handoff.OUT_DIR, handoff.REQUEST_DIR = (
                original_repo, original_results, original_out, original_requests
            )

        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        request = json.loads(request_path.read_text(encoding="utf-8"))
        assert first and not second and request_path == repeated_path
        assert summary["source"] == "reports/experiments.jsonl only"
        assert summary["arms"][0]["trades"] == 20  # latest duplicate, not both
        assert request["context_policy"].startswith("Use only the compact summary")
        assert "trades.csv" not in summary_path.read_text(encoding="utf-8")
        print("[PASS] compact handoff uses latest experiment summaries and deduplicates")


if __name__ == "__main__":
    main()
