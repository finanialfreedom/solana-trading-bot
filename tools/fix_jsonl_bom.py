"""Strip a UTF-8 BOM from reports/experiments.jsonl and verify every line parses.

The BOM breaks json.loads on line 1, which would silently drop the first result
from every downstream report. Run this if the file ever gets rewritten by a tool
that emits a BOM (PowerShell's `Set-Content -Encoding utf8` does).
"""
from __future__ import annotations

import json
import pathlib

P = pathlib.Path(__file__).resolve().parent.parent / "reports" / "experiments.jsonl"


def main() -> int:
    raw = P.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        print("BOM removed")

    lines = [ln for ln in raw.decode("utf-8").splitlines() if ln.strip()]

    ok = 0
    for ln in lines:
        json.loads(ln)  # raises if any line is still unparseable
        ok += 1

    # Write back with explicit utf-8 and no BOM.
    P.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))

    after = P.read_bytes()
    rows = [json.loads(ln) for ln in after.decode("utf-8").splitlines() if ln.strip()]
    diag = sum(1 for r in rows if r.get("label") == "ZZ-diag")

    print(f"first bytes   : {after[:3]!r}  (b'{{\"l' == clean)")
    print(f"lines parsed  : {ok}")
    print(f"rows total    : {len(rows)}")
    print(f"ZZ-diag rows  : {diag}  (must be 0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
