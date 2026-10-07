"""Explicit opt-in login/refresh/read/logout checks; no orders or on-chain changes."""

import os

import pytest

from risex import LocalSigner, RiseXClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RISEX_RUN_SESSION_INTEGRATION") != "1",
        reason="Set RISEX_RUN_SESSION_INTEGRATION=1 with a registered testnet session key",
    ),
]


async def test_login_refresh_fees_and_conditional_snapshot():
    account = os.getenv("RISEX_TEST_ACCOUNT")
    key = os.getenv("RISEX_TEST_SIGNER_PRIVATE_KEY")
    if not account or not key:
        pytest.fail(
            "Session integration requires explicit testnet account and signer configuration"
        )
    async with RiseXClient(mainnet=False, account=account, signer=LocalSigner(key)) as client:
        session = await client.login()
        try:
            assert session.account == client.account
            first = await client.get_user_fees()
            assert first.taker_bps.is_finite()
            refreshed = await client.refresh_session()
            assert refreshed.account == session.account
            second = await client.get_user_fees()
            assert second.taker_bps.is_finite()
            snapshot = await client.get_account_snapshot(include_conditional_orders=True)
            assert snapshot.conditional_orders is not None
            assert all(row.account == client.account for row in snapshot.conditional_orders)
        finally:
            await client.logout()
        assert client.session is None
