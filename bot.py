"""
Watch Pump.fun and paper-trade new tokens with size limits and stop-loss.

Live sends stay off unless DRY_RUN=false and ENABLE_LIVE_TRADING=true.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from solana.rpc.async_api import AsyncClient
from solana.rpc.websocket_api import SolanaWsClient
from solders.rpc.config import RpcTransactionLogsFilterMentions

from config import (
    BUY_ON_NEW_TOKEN,
    COMMITMENT,
    CONFIRM_SECONDS,
    MAX_TRADES,
    PUMP_FUN_PROGRAM_ID,
    RPC_URL,
    RPC_URL_LOG,
    _redact_rpc,
    SCAN_EXISTING_COINS,
    SCAN_INTERVAL_SECONDS,
    SCAN_PAGE_SIZE,
    SCAN_REVIEW_DELAY_SECONDS,
    WSS_URL,
    WSS_URL_LOG,
)
from pumpfun import parse_create_event, parse_trade_event
from report import REPORT
from scanner import fetch_existing_coins
from trader import Trader
from wallet import load_keypair, print_wallet_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


class _RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:
            return True
        if "api-key=" in text.lower():
            record.msg = _redact_rpc(text)
            record.args = ()
        return True


_redact_filter = _RedactSecrets()
_root = logging.getLogger()
_root.addFilter(_redact_filter)
for _handler in _root.handlers:
    _handler.addFilter(_redact_filter)
for _name in ("httpx", "httpcore", "httpcore.http2", "httpcore.http11"):
    logging.getLogger(_name).setLevel(logging.WARNING)
    logging.getLogger(_name).addFilter(_redact_filter)
logger = logging.getLogger("pump-bot")

CREATE_MARKERS = (
    "Instruction: Create",
    "Instruction: create",
    "CreateEvent",
)
TRADE_MARKERS = {
    "buy": ("Instruction: Buy", "Instruction: buy"),
    "sell": ("Instruction: Sell", "Instruction: sell"),
}


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


class PumpBot:
    def __init__(self, trader: Trader) -> None:
        self.trader = trader

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._log_task_error)

    @staticmethod
    def _log_task_error(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.exception("Background task %s failed", task.get_name(), exc_info=exc)

    def handle_log_notification(self, message: Any) -> None:
        result = getattr(message, "result", None)
        if result is None:
            return
        value = getattr(result, "value", None)
        if value is None:
            return
        logs = list(getattr(value, "logs", []) or [])
        signature = str(getattr(value, "signature", ""))
        if getattr(value, "err", None) is not None:
            return

        kind = classify_logs(logs)
        blobs = extract_program_data_lines(logs)

        if kind == "create":
            decoded = None
            for blob in blobs:
                try:
                    decoded = parse_create_event(base64.b64decode(blob))
                except Exception:
                    decoded = None
                if decoded:
                    break
            if decoded:
                logger.info(
                    "NEW TOKEN  %s (%s)  mint=%s  curve=%s  sig=%s",
                    decoded.name,
                    decoded.symbol,
                    decoded.mint,
                    decoded.bonding_curve,
                    signature,
                )
                if BUY_ON_NEW_TOKEN:
                    if CONFIRM_SECONDS > 0:
                        self.trader.confirm.watch(decoded)
                    else:
                        self._spawn(
                            self.trader.on_new_token(decoded, signature),
                            name=f"buy-{decoded.mint}",
                        )
            else:
                logger.info("NEW TOKEN  sig=%s  (could not decode metadata)", signature)
            return

        if kind in {"buy", "sell"}:
            for blob in blobs:
                try:
                    trade = parse_trade_event(base64.b64decode(blob))
                except Exception:
                    trade = None
                if trade:
                    self.trader.confirm.observe_trade(trade)
                    if self.trader.book.get(trade.mint):
                        self.trader.trailing.observe_trade(trade)
                    break
            logger.debug("%s  sig=%s", kind.upper(), signature)

    async def monitor(self) -> None:
        filter_ = RpcTransactionLogsFilterMentions(PUMP_FUN_PROGRAM_ID)
        logger.info("Connecting WebSocket %s", WSS_URL_LOG)
        logger.info("Watching Pump.fun program %s", PUMP_FUN_PROGRAM_ID)

        async with SolanaWsClient(WSS_URL) as websocket:
            subscription = await websocket.logs_subscribe(
                filter_=filter_,
                commitment=COMMITMENT,
            )
            logger.info("Subscribed (id=%s). Ctrl+C to stop.", subscription.subscription_id)
            async for message in websocket:
                self.handle_log_notification(message)


async def _scan_existing(trader: Trader, stop: asyncio.Event) -> None:
    seen: set[str] = set()
    offset = 0
    logger.info("Scanning existing Pump.fun coins (not just new creates)")
    while not stop.is_set():
        if REPORT.opened_count() >= MAX_TRADES:
            logger.info("Existing-coin scan idle: %s-trade cap reached", MAX_TRADES)
            try:
                await asyncio.wait_for(stop.wait(), timeout=SCAN_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                continue
            continue
        try:
            coins, raw = await fetch_existing_coins(offset=offset, limit=SCAN_PAGE_SIZE)
        except Exception as exc:
            logger.warning("Existing-coin fetch failed: %s", exc)
            coins, raw = [], 0
        if raw <= 0:
            offset = 0
            try:
                await asyncio.wait_for(stop.wait(), timeout=SCAN_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                continue
            continue
        for info in coins:
            if stop.is_set() or REPORT.opened_count() >= MAX_TRADES:
                break
            key = str(info.mint)
            if key in seen:
                continue
            seen.add(key)
            logger.info(
                "EXISTING TOKEN  %s (%s)  mint=%s  curve=%s",
                info.name,
                info.symbol,
                info.mint,
                info.bonding_curve,
            )
            try:
                await trader.on_existing_token(info)
            except Exception:
                logger.exception("Existing-coin buy failed for %s", info.symbol)
            try:
                await asyncio.wait_for(stop.wait(), timeout=SCAN_REVIEW_DELAY_SECONDS)
            except asyncio.TimeoutError:
                pass
        offset += SCAN_PAGE_SIZE
        if offset > 400:
            offset = 0
            try:
                await asyncio.wait_for(stop.wait(), timeout=SCAN_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                continue


async def main() -> None:
    keypair = load_keypair()
    logger.info("RPC HTTP %s", RPC_URL_LOG)

    async with AsyncClient(RPC_URL) as client:
        if not await client.is_connected():
            raise RuntimeError(f"Cannot reach RPC: {RPC_URL}")
        await print_wallet_summary(client, keypair)

        trader = Trader(client, keypair)
        await trader.initialize()
        bot = PumpBot(trader)
        stop = asyncio.Event()
        tasks = [
            asyncio.create_task(trader.trailing.run(stop), name="trailing-tp-sl"),
            asyncio.create_task(trader.confirm.run(stop), name="confirm-window"),
        ]
        if SCAN_EXISTING_COINS:
            tasks.append(asyncio.create_task(_scan_existing(trader, stop), name="existing-scan"))

        try:
            if BUY_ON_NEW_TOKEN:
                while True:
                    try:
                        await bot.monitor()
                    except asyncio.CancelledError:
                        raise
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        logger.exception("WebSocket dropped; reconnecting in 3s")
                        await asyncio.sleep(3)
            else:
                logger.info("New-token sniper is off; trading existing Pump.fun coins only.")
                await stop.wait()
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped.")
