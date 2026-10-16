@echo off
REM Self-healing connectivity watch, run every 15 minutes by Task Scheduler.
REM
REM Probes the HTTP RPC and the WebSocket trade stream. If either is down it repairs
REM the connection by itself (DNS cache flush, then escalating retries) and, if a sweep
REM arm has wedged against the dead socket, restarts the sweep so the run continues.
REM No popups, no alerts, no user involvement. Results append to
REM reports\logs\connectivity.log.
cd /d D:\AI\solana-trading-bot
.venv\Scripts\python.exe tools\check_connectivity.py --watch >> reports\logs\conn_watch_stdout.log 2>&1
