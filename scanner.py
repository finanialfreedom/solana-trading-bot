"""Fetch already-trading Pump.fun coins (not just brand-new creates)."""

from __future__ import annotations

import logging

import httpx
from solders.pubkey import Pubkey

from pumpfun import CreateInfo

logger = logging.getLogger("pump-scanner")

PUMP_COINS_URL = "https://frontend-api-v3.pump.fun/coins"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def _to_info(row: dict) -> CreateInfo | None:
    try:
        mint = Pubkey.from_string(str(row["mint"]))
        curve = Pubkey.from_string(str(row["bonding_curve"]))
        creator_raw = row.get("creator")
        creator = Pubkey.from_string(str(creator_raw)) if creator_raw else None
        return CreateInfo(
            name=str(row.get("name") or ""),
            symbol=str(row.get("symbol") or ""),
            uri=str(row.get("metadata_uri") or row.get("image_uri") or ""),
            mint=mint,
            bonding_curve=curve,
            creator=creator,
            virtual_token_reserves=int(row.get("virtual_token_reserves") or 0),
            virtual_quote_reserves=int(
                row.get("virtual_sol_reserves")
                or row.get("virtual_quote_reserves")
                or 0
            ),
        )
    except Exception:
        logger.debug("Skip malformed Pump.fun row", exc_info=True)
        return None


async def fetch_existing_coins(*, offset: int, limit: int) -> tuple[list[CreateInfo], int]:
    params = {
        "offset": offset,
        "limit": limit,
        "sort": "last_trade_timestamp",
        "order": "DESC",
        "includeNsfw": "false",
    }
    # Some Windows setups intercept TLS and fail certifi's CA bundle.
    async with httpx.AsyncClient(
        timeout=20.0,
        headers=HEADERS,
        verify=False,
    ) as http:
        resp = await http.get(PUMP_COINS_URL, params=params)
        resp.raise_for_status()
        payload = resp.json()
    if not isinstance(payload, list):
        return [], 0
    coins: list[CreateInfo] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        if row.get("complete") or row.get("is_banned"):
            continue
        info = _to_info(row)
        if info:
            coins.append(info)
    logger.info(
        "Existing-coin page offset=%s: %s/%s still on the bonding curve",
        offset,
        len(coins),
        len(payload),
    )
    return coins, len(payload)
