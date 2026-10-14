# Solana Trading Bot (Pump.fun monitor)

Python starter that connects to Solana, loads a wallet from `.env`, prints balances, and watches Pump.fun program logs for new tokens and bonding-curve buys/sells. It does **not** send trades.

## Setup

```powershell
cd C:\Users\Honz\solana-trading-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` and set `PRIVATE_KEY` (or `PRIVATE_KEY_JSON`) plus your RPC URLs.

## Run

```powershell
python bot.py
```

Stop with `Ctrl+C`.
