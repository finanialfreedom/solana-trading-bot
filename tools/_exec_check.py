"""Two questions about execution realism in dry-run:

1. Does the recorded entry price match what we would actually pay?
   The code sets entry_price = curve.price (the price BEFORE our buy) but sizes the
   position with tokens_out, which already includes our own price impact. If so, every
   position opens showing an instant profit that does not exist.

2. How much does our own 1 SOL trade move the price? That is unavoidable slippage and
   it differs between entry and exit.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import LAMPORTS_PER_SOL
from pumpfun import BondingCurve, sol_out_for_tokens_in, tokens_out_for_sol_in

FEE_BPS = 100  # pump.fun's 1%

# pump.fun's standard opening reserves.
curve = BondingCurve(
    virtual_token_reserves=1_073_000_000_000_000,
    virtual_quote_reserves=30 * LAMPORTS_PER_SOL,
    real_token_reserves=793_100_000_000_000,
    real_quote_reserves=0,
    token_total_supply=1_000_000_000_000_000,
    complete=False,
    creator=None,
)

for spend_sol in (0.2, 1.0, 2.0, 5.0):
    spend = int(spend_sol * LAMPORTS_PER_SOL)
    tokens = tokens_out_for_sol_in(curve, spend, FEE_BPS)
    price_before = curve.price
    avg_price = spend / tokens if tokens else 0.0
    # What the bot records as entry, and what it marks right after the fill.
    mark_after = (curve.virtual_quote_reserves + spend) / (
        curve.virtual_token_reserves - tokens
    )
    instant_pnl = (mark_after - price_before) / price_before * 100.0
    pay_over = (avg_price - price_before) / price_before * 100.0
    print(f"buy {spend_sol:>4.1f} SOL")
    print(f"   price before         {price_before:.6e}")
    print(f"   avg price actually paid {avg_price:.6e}  (+{pay_over:.2f}% over the pre-trade price)")
    print(f"   bot records entry    {price_before:.6e}")
    print(f"   bot marks right after{mark_after:.6e}  -> instant pnl {instant_pnl:+.2f}%")
    print()

# Exit side: selling the whole 1 SOL position back into the curve.
spend = 1 * LAMPORTS_PER_SOL
tokens = tokens_out_for_sol_in(curve, spend, FEE_BPS)
post = BondingCurve(
    virtual_token_reserves=curve.virtual_token_reserves - tokens,
    virtual_quote_reserves=curve.virtual_quote_reserves + spend,
    real_token_reserves=curve.real_token_reserves,
    real_quote_reserves=0,
    token_total_supply=curve.token_total_supply,
    complete=False,
    creator=None,
)
back = sol_out_for_tokens_in(post, tokens, FEE_BPS)
print("round trip with no price movement at all (buy then immediately sell):")
print(f"   spent      {spend / LAMPORTS_PER_SOL:.4f} SOL")
print(f"   got back   {back / LAMPORTS_PER_SOL:.4f} SOL")
print(f"   cost       {-100 * (spend - back) / spend:.2f}%  <- pure fees + our own impact size")
