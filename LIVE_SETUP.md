# Initial live deployment

Status: **locked / no live transactions**

Configured plan:

- Starting wallet value: approximately $105.43
- Trade size: 0.09635 SOL (approximately $10 at setup time)
- Maximum open positions: 10
- Session limit: 100 opened trades
- Combined account-risk percentage: 100%
- Operational SOL reserve: 0.02 SOL
- Entry filters: 15 buyers, 2 SOL gross buys, $5,000 market cap,
  1 SOL net buys, and maximum 0.5 sell/buy ratio
- Position stop-loss: 6%

## Connect the wallet

Create this ignored local file:

`D:\AI\intial live\secrets\live-pilot.key`

Its contents must be either the wallet's base58 private key or a Solana CLI JSON
keypair array. Never commit, upload, or paste that secret into chat.

The bot derives the public wallet address from this key. Before live mode is
enabled, verify the derived public address and on-chain SOL balance.

Run the read-only verification:

```powershell
cd "D:\AI\intial live"
.\.venv\Scripts\python.exe tools\live_preflight.py
```

## Live lock

`.env` intentionally remains:

```text
DRY_RUN=true
ENABLE_LIVE_TRADING=false
```

Both values must be changed deliberately before transactions can be sent.
