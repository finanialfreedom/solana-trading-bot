# Solana Trading Bot (Pump.fun)

Watches Pump.fun logs, paper-trades new tokens in **dry-run** by default, and exits on stop-loss (optional take-profit). No live transaction is sent unless you turn dry-run off **and** set `ENABLE_LIVE_TRADING=true`.

## Setup

```powershell
cd D:\AI\solana-trading-bot
D:\AI\Python\python.exe -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`: set `PRIVATE_KEY` (or `PRIVATE_KEY_JSON`). For faster RPC, set `HELIUS_API_KEY` from [dashboard.helius.dev](https://dashboard.helius.dev) (free plan is fine to test). Leave `RPC_URL` / `WSS_URL` empty and the bot will use Helius automatically.

## Run

```powershell
.\.venv\Scripts\Activate.ps1
python bot.py
```

Stop with `Ctrl+C`.

## Trading knobs

| Variable | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `true` | Simulate orders only |
| `ENABLE_LIVE_TRADING` | `false` | Required, with `DRY_RUN=false`, to send real txs |
| `BUY_ON_NEW_TOKEN` | `true` | Buy when a new Pump.fun token is created |
| `SCAN_EXISTING_COINS` | `false` | Also scan coins already trading on Pump.fun |
| `BUY_SOL_AMOUNT` | `1` | Fallback size if confirm window is off |
| `BUY_PROBE_SOL` | `0.2` | First buy, placed after `CONFIRM_SECONDS` |
| `BUY_ADD_SOL` | `0.8` | Second buy once the add window has passed and MC is high enough |
| `MAX_SOL_PER_TRADE` | `1` | Hard cap per coin (probe + add) |
| `MAX_TRADES` | `100` | Session stops opening after this many buys |
| `MAX_OPEN_POSITIONS` | `4` | Max concurrent positions |
| `CONFIRM_SECONDS` | `5` | Wait this long after launch, then buy the probe |
| `ADD_AFTER_SECONDS` | `10` | Earliest time for the add-on buy |
| `MIN_ADD_MC_USD` | `5000` | Add the extra size only if market cap has reached this |
| `DEAD_COIN_SECONDS` | `30` | Sell a probe that never reaches +10% |
| `STOP_LOSS_PCT` | `3` | Sell if price is down this percent before the breakeven stop is armed |
| `BREAKEVEN_ARM_PCT` | `3.5` | Once price is up this percent, move the stop to entry |
| `TAKE_PROFIT_TRIGGER_PCT` | `30` | Arm trailing take-profit once PnL reaches this percent |
| `TRAILING_STOP_PCT` | `15` | After arming, sell if price drops this percent from the peak |
| `TRAILING_LOCK_BREAKEVEN` | `true` | After the +30% arm, also sell if price falls back to entry |
| `TRAILING_TICK_SECONDS` | `0.5` | Background watchdog interval for peak tracking and SL/TP |
| `TRAILING_ARMED_TICK_SECONDS` | `0.15` | Faster poll while any position is in trailing mode |

The live Excel report is `reports/pumpfun_trades.xlsx` (Trades + Blocked + Summary). Each trade row is WON or LOST with the percentage. Security rejects are listed on the **Blocked** sheet and in `reports/blocked.csv`.

## Security filters

These run even in dry-run. A coin is skipped if any check fails:

| Filter | Default | What it catches |
|---|---|---|
| `MIN_LP_LOCKED_PCT` | `0` | LP lock floor (0 = off) |
| `MIN_LIQUIDITY_USD` | `0` | Total pool liquidity floor (0 = off) |
| `MAX_CREATOR_HOLD_PCT` | `10` | Skip if creator holds more than 10% |
| `MIN_HOLDERS` | `0` | Holder-count floor (0 = off) |
| `BUNDLE_WALLET_COUNT` | `5` | Skip sniper/bundle clusters |
| `MAX_INSIDER_TOKENS` | `10000000` | Insider/whale bag over 10M tokens |
| `MAX_HOLDER_PCT` | `30` | One wallet (not the LP) holds too much supply |
| `REJECT_FREEZE_AUTHORITY` | `true` | Team can freeze your tokens |
| `REJECT_EXTERNAL_MINT_AUTH` | `true` | Team can mint infinite supply |
| `MAX_TAX_BPS` | `100` | Buy/sell tax above 1% |
| `REJECT_MUTABLE_TAX` | `true` | Buy/sell tax is open (team can raise it later) |
| `REJECT_UNVERIFIED` | `false` | Skip unverified metadata (off) |
| `REJECT_PERMANENT_DELEGATE` | `true` | Team can steal from any wallet |
| `REJECT_TRANSFER_HOOK` | `true` | Custom hook can block sells (honeypot) |
| `REJECT_NON_TRANSFERABLE` | `true` | Tokens that cannot be sold |
| `REJECT_PAUSABLE` | `true` | Trading can be paused |

Brand-new Pump.fun coins often have **under $10k real liquidity**, so this filter will skip most launches until the curve fills. That is intentional.

Use a **small test wallet**. Live Pump.fun sniping is high risk.
