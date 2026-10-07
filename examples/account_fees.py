"""Testnet login, fee tier and active TP/SL reads using an already-registered signer."""

import asyncio
import os

from risex import LocalSigner, RiseXClient


async def main() -> None:
    async with RiseXClient(
        mainnet=False,
        account=os.environ["RISEX_TEST_ACCOUNT"],
        signer=LocalSigner(os.environ["RISEX_TEST_SIGNER_PRIVATE_KEY"]),
    ) as client:
        await client.login()
        try:
            fees = await client.get_user_fees()
            snapshot = await client.get_account_snapshot(include_conditional_orders=True)
            print("Fee tier:", fees.tier, "Taker bps:", fees.taker_bps)
            assert snapshot.conditional_orders is not None
            print("Active TP/SL orders:", len(snapshot.conditional_orders))
        finally:
            await client.logout()


if __name__ == "__main__":
    asyncio.run(main())
