"""Regression checks for Token-2022 routing and confirmed live transactions."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.system_program import TransferParams, transfer  # noqa: E402
from spl.token.instructions import get_associated_token_address  # noqa: E402

from pumpfun import (  # noqa: E402
    BUYBACK_FEE_RECIPIENT,
    associated_bonding_curve,
    bonding_curve_v2_pda,
    build_buy_exact_sol_in,
    build_create_ata,
    build_sell,
)
from trader import SendResult, TOKEN_2022, Trader  # noqa: E402


def check(label: str, condition: bool) -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        raise AssertionError(label)


class FakeClient:
    def __init__(self, error: object | None = None) -> None:
        self.error = error
        self.signature = Signature.default()
        self.balances = [1_000_000_000, 900_000_000]

    async def get_balance(self, *_args, **_kwargs):
        return SimpleNamespace(value=self.balances.pop(0))

    async def get_latest_blockhash(self):
        return SimpleNamespace(
            value=SimpleNamespace(
                blockhash=Hash.default(),
                last_valid_block_height=123,
            )
        )

    async def send_transaction(self, _tx, opts=None):
        check("preflight is enabled", opts.skip_preflight is False)
        return SimpleNamespace(value=self.signature)

    async def confirm_transaction(self, *_args, **_kwargs):
        return SimpleNamespace(value=[SimpleNamespace(err=self.error)])


async def check_confirmation() -> None:
    trader = Trader.__new__(Trader)
    trader.client = FakeClient()
    trader.wallet = Keypair()
    trader._tx_lock = asyncio.Lock()
    instruction = transfer(
        TransferParams(
            from_pubkey=trader.wallet.pubkey(),
            to_pubkey=Keypair().pubkey(),
            lamports=1,
        )
    )
    result = await trader._send([instruction])
    check("confirmed send returns a result", isinstance(result, SendResult))
    check(
        "wallet delta is measured from chain balances",
        result is not None and result.wallet_delta_lamports == -100_000_000,
    )

    trader.client = FakeClient(error="simulated failure")
    failed = await trader._send([instruction])
    check("failed confirmation is never recorded as success", failed is None)


def check_token_2022_routing() -> None:
    mint = Keypair().pubkey()
    curve = Keypair().pubkey()
    owner = Keypair().pubkey()
    fee_recipient = Keypair().pubkey()
    creator = Keypair().pubkey()
    curve_ata = associated_bonding_curve(mint, curve, TOKEN_2022)
    user_ata = get_associated_token_address(owner, mint, TOKEN_2022)
    check(
        "bonding-curve ATA uses Token-2022",
        curve_ata == get_associated_token_address(curve, mint, TOKEN_2022),
    )

    create_ata = build_create_ata(owner, owner, mint, TOKEN_2022)
    check(
        "ATA creation includes Token-2022",
        TOKEN_2022 in {meta.pubkey for meta in create_ata.accounts},
    )
    buy = build_buy_exact_sol_in(
        mint=mint,
        bonding_curve=curve,
        associated_curve=curve_ata,
        associated_user=user_ata,
        user=owner,
        fee_recipient=fee_recipient,
        creator=creator,
        spendable_sol=100,
        min_tokens_out=1,
        token_program=TOKEN_2022,
    )
    check("buy routes through Token-2022", buy.accounts[8].pubkey == TOKEN_2022)
    check("buy has upgraded 18-account layout", len(buy.accounts) == 18)
    check(
        "buy appends bonding-curve-v2 then buyback recipient",
        buy.accounts[-2].pubkey == bonding_curve_v2_pda(mint)
        and buy.accounts[-1].pubkey == BUYBACK_FEE_RECIPIENT
        and buy.accounts[-1].is_writable,
    )
    sell = build_sell(
        mint=mint,
        bonding_curve=curve,
        associated_curve=curve_ata,
        associated_user=user_ata,
        user=owner,
        fee_recipient=fee_recipient,
        creator=creator,
        token_amount=1,
        min_sol_output=1,
        token_program=TOKEN_2022,
    )
    check("sell routes through Token-2022", sell.accounts[9].pubkey == TOKEN_2022)
    check("non-cashback sell has upgraded 16-account layout", len(sell.accounts) == 16)


def main() -> None:
    check_token_2022_routing()
    asyncio.run(check_confirmation())
    print("All live transaction safety checks passed.")


if __name__ == "__main__":
    main()
