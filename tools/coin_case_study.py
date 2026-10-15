"""Case study: reconstruct one coin's market-cap trajectory.

Answers "what would it have taken to catch this coin" for a given mint by pulling
free GeckoTerminal OHLCV for every pool the token trades in and converting price to
market cap (pump.fun supply is a fixed 1e9).

Usage:
    python tools/coin_case_study.py <mint> [--target 1000000]
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import datetime, timezone

API = "https://api.geckoterminal.com/api/v2"
SUPPLY = 1_000_000_000  # pump.fun tokens are minted with a fixed 1e9 supply


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fmt_mc(mc: float) -> str:
    if mc >= 1_000_000:
        return f"${mc / 1_000_000:.2f}M"
    if mc >= 1_000:
        return f"${mc / 1_000:.1f}k"
    return f"${mc:.0f}"


def fetch_pools(mint: str) -> list[dict]:
    data = get_json(f"{API}/networks/solana/tokens/{mint}/pools")["data"]
    return [
        {
            "pool": p["attributes"]["address"],
            "dex": p["relationships"]["dex"]["data"]["id"],
            "created": p["attributes"].get("pool_created_at", ""),
        }
        for p in data
    ]


def fetch_candles(pool: str, timeframe: str = "minute", aggregate: int = 1) -> list[list]:
    url = f"{API}/networks/solana/pools/{pool}/ohlcv/{timeframe}?aggregate={aggregate}&limit=1000"
    try:
        return get_json(url)["data"]["attributes"]["ohlcv_list"]
    except Exception:
        return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mint")
    ap.add_argument("--target", type=float, default=1_000_000)
    args = ap.parse_args()

    pools = fetch_pools(args.mint)
    if not pools:
        raise SystemExit("no pools returned for this mint")

    # merge every pool's candles so we see the bonding curve AND the post-graduation AMM
    points: dict[int, dict] = {}
    for pool in pools:
        for candle in fetch_candles(pool["pool"]):
            ts, _o, high, low, close = int(candle[0]), candle[1], candle[2], candle[3], candle[4]
            close_px = float(close)
            if close_px <= 0:
                continue
            slot = points.setdefault(ts, {"ts": ts, "close": close_px, "high": float(high),
                                          "low": float(low), "pools": []})
            if close_px > slot["close"]:
                slot["close"] = close_px
            if float(high) > slot["high"]:
                slot["high"] = float(high)
            slot["pools"].append(pool["dex"])

    series = [points[k] for k in sorted(points)]
    if not series:
        raise SystemExit("no usable candles")

    launch = series[0]
    peak = max(series, key=lambda p: p["high"])
    low = min(series, key=lambda p: p["low"])
    last = series[-1]

    def ts(p: dict) -> str:
        return datetime.fromtimestamp(p["ts"], tz=timezone.utc).strftime("%H:%M:%SZ")

    print(f"mint {args.mint}")
    print(f"pools: {', '.join(sorted({p['dex'] for p in pools}))}")
    print(f"candles: {len(series)} minutes from {ts(launch)} to {ts(last)}\n")
    print(f"  launch MC      {fmt_mc(launch['close'] * SUPPLY)}  at {ts(launch)}")
    print(f"  peak MC        {fmt_mc(peak['high'] * SUPPLY)}  at {ts(peak)}")
    print(f"  lowest MC      {fmt_mc(low['low'] * SUPPLY)}  at {ts(low)}")
    print(f"  latest MC      {fmt_mc(last['close'] * SUPPLY)}  at {ts(last)}")
    print(f"  launch->peak   {peak['high'] / launch['close']:.1f}x over "
          f"{(peak['ts'] - launch['ts']) / 60:.0f} min")
    print(f"  peak->latest   {(last['close'] / peak['high'] - 1) * 100:+.1f}% give-back\n")

    print("  minute-by-minute MC (every 5 min):")
    for p in series:
        if (p["ts"] - launch["ts"]) % 300 == 0:
            print(f"    {ts(p):>9}  {fmt_mc(p['close'] * SUPPLY):>9}  "
                  f"high {fmt_mc(p['high'] * SUPPLY)}")

    # how early would you need to be, and what does riding to a target pay?
    print(f"\n  entering at these MCs and riding to {fmt_mc(args.target)}:")
    for entry_mc in (3_000, 5_000, 10_000, 20_000, 40_000, 69_000, 100_000, 250_000):
        crosses = next((p for p in series if p["high"] * SUPPLY >= args.target), None)
        print(f"    entry {fmt_mc(entry_mc):>8} -> target {fmt_mc(args.target):>7} = "
              f"{args.target / entry_mc:>6.1f}x", end="")
        if crosses:
            minutes = (crosses["ts"] - launch["ts"]) / 60
            print(f"   (target hit at {ts(crosses)}, {minutes:.0f} min after launch)")
        else:
            print("   (never reached in this window)")


if __name__ == "__main__":
    main()
