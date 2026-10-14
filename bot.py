"""
Monitor Pump.fun program logs (new mints and bonding-curve trades).

This process is read-only: it does not send buy/sell transactions.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import struct
from typing import Any

from solana.rpc.async_api import AsyncClient
from solana.rpc.websocket_api import connect
from solders.pubkey import Pubkey
from solders.rpc.config import RpcTransactionLogsFilterMentions

from config import COMMITMENT, PUMP_FUN_PROGRAM_ID, RPC_URL, WSS_URL
from wallet import load_keypair, print_wallet_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("pump-monitor")

CREATE_MARKERS = (
    "Instruction: Create",
    "Instruction: create",
    "CreateEvent",
)
TRADE_MARKERS = {
    "buy": ("Instruction: Buy", "Instruction: buy"),
    "sell": ("Instruction: Sell", "Instruction: sell"),
}


def _read_borsh_string(data: bytes, offset: int) -> tuple[str, int]:
    if offset + 4 > len(data):
        raise ValueError("truncated string length")
    (length,) = struct.unpack_from("<I", data, offset)
    offset += 4
    end = offset + length
    if end > len(data):
        raise ValueError("truncated string bytes")
    return data[offset:end].decode("utf-8", errors="replace"), end


def decode_create_program_data(raw: str) -> dict[str, str] | None:
    """Best-effort decode of Pump.fun Create `Program data:` (Anchor event)."""
    try:
        payload = base64.b64decode(raw)
        # Skip 8-byte Anchor event discriminator.
        offset = 8
        name, offset = _read_borsh_string(payload, offset)
        symbol, offset = _read_borsh_string(payload, offset)
        uri, offset = _read_borsh_string(payload, offset)
        if offset + 96 > len(payload):
            return {"name": name, "symbol": symbol, "uri": uri}

        mint = str(Pubkey.from_bytes(payload[offset : offset + 32]))
        bonding_curve = str(Pubkey.from_bytes(payload[offset + 32 : offset + 64]))
        user = str(Pubkey.from_bytes(payload[offset + 64 : offset + 96]))
        return {
            "name": name,
            "symbol": symbol,
            "uri": uri,
            "mint": mint,
            "bonding_curve": bonding_curve,
            "creator": user,
        }
    except Exception:
        return None


def classify_logs(logs: list[str]) -> str:
    joined = "\n".join(logs)
    for marker in CREATE_MARKERS:
        if marker in joined:
            return "create"
    for kind, markers in TRADE_MARKERS.items():
        if any(marker in joined for marker in markers):
            return kind
    return "other"


def extract_program_data_lines(logs: list[str]) -> list[str]:
    blobs: list[str] = []
    prefix = "Program data: "
    for line in logs:
        if prefix in line:
            blobs.append(line.split(prefix, 1)[1].strip())
    return blobs


def handle_log_notification(message: Any) -> None:
    result = getattr(message, "result", None)
    if result is None:
        return

    value = getattr(result, "value", None)
    if value is None:
        return

    logs = list(getattr(value, "logs", []) or [])
    signature = str(getattr(value, "signature", ""))
    err = getattr(value, "err", None)
    if err is not None:
        return

    kind = classify_logs(logs)
    if kind == "create":
        decoded = None
        for blob in extract_program_data_lines(logs):
            decoded = decode_create_program_data(blob)
            if decoded:
                break
        if decoded:
            logger.info(
                "NEW TOKEN  sig=%s  %s (%s)  mint=%s  curve=%s  creator=%s",
                signature,
                decoded.get("name"),
                decoded.get("symbol"),
                decoded.get("mint"),
                decoded.get("bonding_curve"),
                decoded.get("creator"),
            )
        else:
            logger.info("NEW TOKEN  sig=%s  (could not decode metadata)", signature)
        return

    if kind in {"buy", "sell"}:
        logger.info("%s  sig=%s", kind.upper(), signature)
        return

    logger.debug("other program log sig=%s", signature)


async def monitor_pump_fun() -> None:
    filter_ = RpcTransactionLogsFilterMentions(PUMP_FUN_PROGRAM_ID)
    logger.info("Connecting WebSocket %s", WSS_URL)
    logger.info("Watching Pump.fun program %s", PUMP_FUN_PROGRAM_ID)

    async with connect(WSS_URL) as websocket:
        await websocket.logs_subscribe(filter_, commitment=COMMITMENT)
        first = await websocket.recv()
        subscription_id = first[0].result if first else None
        logger.info("Subscribed (id=%s). Ctrl+C to stop.", subscription_id)

        async for messages in websocket:
            batch = messages if isinstance(messages, list) else [messages]
            for message in batch:
                handle_log_notification(message)


async def main() -> None:
    keypair = load_keypair()
    logger.info("RPC HTTP %s", RPC_URL)

    async with AsyncClient(RPC_URL) as client:
        if not await client.is_connected():
            raise RuntimeError(f"Cannot reach RPC: {RPC_URL}")
        await print_wallet_summary(client, keypair)

    while True:
        try:
            await monitor_pump_fun()
        except asyncio.CancelledError:
            raise
        except KeyboardInterrupt:
            raise
        except Exception:
            logger.exception("WebSocket dropped; reconnecting in 3s")
            await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped.")
