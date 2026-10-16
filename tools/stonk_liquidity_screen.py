"""Find Solana tokens with liquidity comparable to STONK. Data only, no advice."""
from __future__ import annotations

import json
import statistics as st
import urllib.request
from datetime import datetime, timezone

STONK_MINT = "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx"
URL = "https://lite-api.jup.ag/tokens/v2/tag?query=verified"

# Assets that are not "coins" in the memecoin/token sense.
EXCLUDE_SYMBOLS = {
    "SOL", "WSOL", "USDC", "USDT", "USDS", "PYUSD", "FDUSD", "USDG", "JupUSD",
    "JitoSOL", "mSOL", "bSOL", "INF", "JitoSOL", "stSOL", "hSOL", "dSOL",
    "EURC", "USD1", "USDE", "sUSDE", "syrupUSDC", "CASH", "USDY", "USDO",
    "WBTC", "WETH", "cbBTC", "JLP", "Jito", "SOLVBTC", "zBTC", "LBTC",
}


def fetch(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "liquidity-screen/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    toks = fetch(URL)
    print(f"fetched verified tokens: {len(toks)}")

    stonk = next((t for t in toks if t["id"] == STONK_MINT), None)
    if not stonk:
        print("STONK not in verified list; using known figure")
        stonk_liq = 3_660_785.0
    else:
        stonk_liq = float(stonk["liquidity"])
        print(f"STONK from list: liq=${stonk_liq:,.0f} mcap=${stonk['mcap']:,.0f}")

    liqs = [float(t["liquidity"]) for t in toks if t.get("liquidity")]
    print(f"\n--- liquidity distribution across {len(liqs)} tokens ---")
    liqs.sort()
    for p in (50, 75, 90, 95, 99):
        print(f"  p{p}: ${liqs[int(len(liqs)*p/100)]:,.0f}")
    print(f"  max: ${liqs[-1]:,.0f}")

    # Comparables: within a window around STONK's liquidity.
    lo, hi = stonk_liq * 0.5, stonk_liq * 2.0
    print(f"\n--- window 0.5x-2.0x of STONK = ${lo:,.0f} - ${hi:,.0f} ---")

    cands = []
    for t in toks:
        sym = (t.get("symbol") or "").strip()
        liq = float(t.get("liquidity") or 0)
        if not liq or sym.upper() in EXCLUDE_SYMBOLS:
            continue
        if not (lo <= liq <= hi):
            continue
        aud = t.get("audit") or {}
        cands.append({
            "symbol": sym,
            "name": (t.get("name") or "")[:34],
            "mint": t["id"],
            "liq": liq,
            "mcap": float(t.get("mcap") or 0),
            "holders": t.get("holderCount") or 0,
            "org": float(t.get("organicScore") or 0),
            "mint_off": bool(aud.get("mintAuthorityDisabled")),
            "frz_off": bool(aud.get("freezeAuthorityDisabled")),
            "top10": aud.get("topHoldersPercentage"),
            "age_days": None,
        })
        fp = t.get("firstPool") or {}
        if fp.get("createdAt"):
            try:
                d = datetime.fromisoformat(fp["createdAt"].replace("Z", "+00:00"))
                cands[-1]["age_days"] = (datetime.now(timezone.utc) - d).days
            except Exception:
                pass

    cands.sort(key=lambda c: abs(c["liq"] - stonk_liq))
    print(f"comparables found: {len(cands)}\n")

    hdr = f"{'sym':<11}{'name':<35}{'liquidity':>14}{'mcap':>14}{'hold':>9}{'org':>5}{'ageD':>6}{'top10':>7}  mintOff frzOff"
    print(hdr)
    print("-" * len(hdr))
    for c in cands[:60]:
        top = f"{c['top10']:.1f}" if isinstance(c["top10"], (int, float)) else "-"
        print(f"{c['symbol']:<11}{c['name']:<35}${c['liq']:>13,.0f}${c['mcap']:>13,.0f}{c['holders']:>9,}{c['org']:>5.0f}{str(c['age_days']):>6}{top:>7}  {c['mint_off']!s:<7} {c['frz_off']!s:<6}")

    with open("reports/stonk_liquidity_comps.json", "w", encoding="utf-8") as f:
        json.dump({
            "generated": datetime.now(timezone.utc).isoformat(),
            "stonk": {"mint": STONK_MINT, "jupiter_liquidity": stonk_liq},
            "window": [lo, hi],
            "comparables": cands,
        }, f, indent=2)
    print(f"\nwrote reports/stonk_liquidity_comps.json ({len(cands)} rows)")


if __name__ == "__main__":
    main()
