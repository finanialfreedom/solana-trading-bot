# Mandatory gates before the 24-hour dry-run

Risk policy used by every gate:

- Paper account: **100 SOL**
- Maximum combined capital at risk across all open positions: **10% / 10 SOL**
- This is an account-level cap, not a per-trade stop.
- Per-position size remains capped at 1 SOL and is reduced automatically when less
  than 1 SOL remains in the account risk budget.

The 24-hour test must not start until all of these pass:

1. Entry-quality comparison completes under the 10 SOL combined cap.
2. The selected entry filter reaches 30-45% win rate.
3. Average win is at least twice the average loss.
4. Position-size comparison (0.1, 0.25, 0.5 and 1 SOL) identifies a positive,
   stable size without exceeding 10 SOL of combined open risk.
5. Sell-pressure plus early/late ladder comparison is positive across repetitions.
6. A 3-4 hour validation at 5% simulated slippage has positive net P&L.
7. No frozen positions, stale capacity, uncaught errors, or account-risk breaches.

Failing any gate sends the bot back for adjustment; it does not proceed to 24 hours.
