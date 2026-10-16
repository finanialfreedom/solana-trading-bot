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


async def http_test() -> None:
    print(f"HTTP RPC  {RPC_URL_LOG}")
    for attempt in range(1, 4):
        t0 = time.perf_counter()
        try:
            async with AsyncClient(RPC_URL) as client:
                resp = await asyncio.wait_for(client.get_latest_blockhash(), timeout=15)
            ms = (time.perf_counter() - t0) * 1000
            print(f"  OK   blockhash received in {ms:.0f} ms")
            return
        except Exception as exc:  # noqa: BLE001 - report whatever the network says
            ms = (time.perf_counter() - t0) * 1000
            print(f"  FAIL attempt {attempt} after {ms:.0f} ms: {type(exc).__name__}: {exc}")
            await asyncio.sleep(2)
    print("  -> HTTP RPC is NOT reachable")


async def ws_test() -> None:
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
            return
        except Exception as exc:  # noqa: BLE001
            ms = (time.perf_counter() - t0) * 1000
            print(f"  FAIL attempt {attempt} after {ms:.0f} ms: {type(exc).__name__}: {exc}")
            await asyncio.sleep(2)
    print("  -> WebSocket is NOT reachable (the live trade stream would be dark)")


async def main() -> None:
    await http_test()
    print()
    await ws_test()
    print()
    print("If either says FAIL, Norton is still blocking and the bot cannot trade for real.")


if __name__ == "__main__":
    asyncio.run(main())
