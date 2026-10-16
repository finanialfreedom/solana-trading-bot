"""Is Norton actually blocking us? Test both connections the bot needs.

HTTP RPC carries the buys and sells; the WebSocket carries the live trade stream the
confirm window and the trailing stop depend on. If either is blocked the bot is blind.
Prints no secrets.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets
from solana.rpc.async_api import AsyncClient

from config import RPC_URL, RPC_URL_LOG, WSS_URL, WSS_URL_LOG


async def http_test() -> bool:
    print(f"HTTP RPC  {RPC_URL_LOG}")
    for attempt in range(1, 4):
        t0 = time.perf_counter()
        try:
            async with AsyncClient(RPC_URL) as client:
                resp = await asyncio.wait_for(client.get_latest_blockhash(), timeout=15)
            ms = (time.perf_counter() - t0) * 1000
            print(f"  OK   blockhash received in {ms:.0f} ms")
            return True
        except Exception as exc:  # noqa: BLE001 - report whatever the network says
            ms = (time.perf_counter() - t0) * 1000
            print(f"  FAIL attempt {attempt} after {ms:.0f} ms: {type(exc).__name__}: {exc}")
            await asyncio.sleep(2)
    print("  -> HTTP RPC is NOT reachable")
    return False


async def ws_test() -> bool:
    print(f"WebSocket {WSS_URL_LOG}")
    for attempt in range(1, 4):
        t0 = time.perf_counter()
        try:
            async with websockets.connect(WSS_URL, open_timeout=15, close_timeout=5) as ws:
                await ws.send(
                    '{"jsonrpc":"2.0","id":1,"method":"blockSubscribe",'
                    '"params":["all"]}'
                )
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
            ms = (time.perf_counter() - t0) * 1000
            print(f"  OK   connected and got a frame in {ms:.0f} ms ({len(raw)} bytes)")
            return True
        except Exception as exc:  # noqa: BLE001
            ms = (time.perf_counter() - t0) * 1000
            print(f"  FAIL attempt {attempt} after {ms:.0f} ms: {type(exc).__name__}: {exc}")
            await asyncio.sleep(2)
    print("  -> WebSocket is NOT reachable (the live trade stream would be dark)")
    return False


async def probe() -> bool:
    """Silent version of both tests. Returns True only if both work."""
    import contextlib
    import io

    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        http_ok = await http_test()
        ws_ok = await ws_test()
    return http_ok and ws_ok


async def log_mode() -> int:
    """One silent probe, appended to reports/logs/connectivity.log.

    Run every 15 minutes by Task Scheduler while an unattended test run is going, so
    that in the morning there is a timestamped record of whether the network was up the
    whole night. If Norton blocks the connection mid-run this is how we find out.
    """
    log_path = Path(__file__).resolve().parent.parent / "reports" / "logs" / "connectivity.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ok = await probe()
    line = "{0}  {1}".format(
        time.strftime("%Y-%m-%d %H:%M:%S"),
        "OK   RPC + WebSocket reachable" if ok else "FAIL RPC or WebSocket blocked",
    )
    print(line)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return 0 if ok else 1


async def main() -> None:
    if "--log" in sys.argv:
        raise SystemExit(await log_mode())
    http_ok = await http_test()
    print()
    ws_ok = await ws_test()
    print()
    if http_ok and ws_ok:
        print("Both connections are up. The bot can trade for real.")
    else:
        print("If either says FAIL, Norton is still blocking and the bot cannot trade for real.")


if __name__ == "__main__":
    asyncio.run(main())
