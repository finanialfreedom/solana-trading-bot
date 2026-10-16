@echo off
REM One silent connectivity probe, appended to reports\logs\connectivity.log.
REM Run every 15 minutes by Task Scheduler so an unattended overnight test leaves a
REM timestamped record of whether Norton ever blocked the RPC or the trade stream.
cd /d D:\AI\solana-trading-bot
.venv\Scripts\python.exe tools\check_connectivity.py --log >> reports\logs\conn_watch_stdout.log 2>&1
