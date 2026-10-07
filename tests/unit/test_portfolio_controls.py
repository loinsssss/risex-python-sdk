import base64
import json
from decimal import Decimal

import httpx
import pytest
from eth_keys import keys
from eth_utils import keccak
from test_signing import golden_witness, word

from risex import ProtocolError, RiseXClient, UnknownOutcomeError
from risex.signing import account_setting_action_hash


def portfolio(account):
    return {
        "account": account,
        "positions": [],
        "summary": {
            "collateral_margin_balance": "1000",
            "cross_margin_balance": "-0.000000000000000001",
            "free_collateral": "-2",
            "total_account_value": "999",
            "total_notional": "0",
            "total_initial_margin": "0",
            "total_maintenance_margin": "0",
            "in_liquidation": False,
            "risk_level": "NORMAL",
        },
    }


def receipt(status=1):
    return {
        "transaction_hash": "0x" + "ab" * 32,
        "block_number": "10",
        "receipt": {"status": status, "block_number": "10", "gas_used": "100"},
    }


async def test_portfolio_exact_scope_and_required_maintenance(config, backend):
    payload = portfolio(backend.account)

    async def transport(request):
        return httpx.Response(200, json={"data": payload})

    async with RiseXClient(
        config, account=backend.account, transport=httpx.MockTransport(transport)
    ) as client:
        result = await client.get_portfolio_details()
        assert result.summary.cross_margin_balance == Decimal("-0.000000000000000001")
        payload["account"] = "0x" + "33" * 20
        with pytest.raises(ProtocolError):
            await client.get_portfolio_details()
        payload["account"] = backend.account
        del payload["summary"]["total_maintenance_margin"]
        with pytest.raises(ProtocolError):
            await client.get_portfolio_details()


@pytest.mark.parametrize(
    ("setting", "value", "selector"),
    [
        ("leverage", 10, "RISE_PERPS_UPDATE_LEVERAGE_V1"),
        ("margin_mode", 0, "RISE_PERPS_UPDATE_MARGIN_MODE_V1"),
    ],
)
async def test_setting_wire_and_independent_signature(
    config, backend, signer, setting, value, selector
):
    posts = []

    async def transport(request):
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"data": receipt()})
        return await backend(request)

    async with RiseXClient(
        config, account=backend.account, signer=signer, transport=httpx.MockTransport(transport)
    ) as client:
        if setting == "leverage":
            await client.update_leverage(1, value)
        else:
            await client.update_margin_mode(1, isolated=False)
    assert len(posts) == 1
    body = posts[0]
    assert body["market_id"] == "1"
    assert body[setting] == (str(value) if setting == "leverage" else value)
    permit = body["permit_params"]
    expected_hash = keccak(keccak(text=selector) + word(1) + word(value))
    assert expected_hash == account_setting_action_hash(1, value, setting=setting)
    domain = {
        "name": backend.domain["name"],
        "version": backend.domain["version"],
        "chainId": int(backend.domain["chain_id"]),
        "verifyingContract": backend.domain["verifying_contract"],
    }
    digest = golden_witness(
        domain,
        {
            "account": backend.account,
            "target": backend.system["addresses"]["router"],
            "hash": expected_hash,
            "nonceAnchor": int(permit["nonce_anchor"]),
            "nonceBitmap": permit["nonce_bitmap_index"],
            "deadline": permit["deadline"],
        },
    )
    compact = base64.b64decode(permit["signature"])
    s = int.from_bytes(compact[32:], "big")
    signature = compact[:32] + word(s & ((1 << 255) - 1)) + bytes([s >> 255])
    assert (
        keys.Signature(signature).recover_public_key_from_msg_hash(digest).to_checksum_address()
        == signer.address
    )


@pytest.mark.parametrize("failure", ["timeout", "revert", "503"])
async def test_setting_unknown_is_not_replayed(config, backend, signer, failure):
    posts = []

    async def transport(request):
        if request.method == "POST":
            posts.append(request)
            if failure == "timeout":
                raise httpx.ReadTimeout("test")
            if failure == "503":
                return httpx.Response(503)
            return httpx.Response(200, json={"data": receipt(0)})
        return await backend(request)

    async with RiseXClient(
        config, account=backend.account, signer=signer, transport=httpx.MockTransport(transport)
    ) as client:
        with pytest.raises(UnknownOutcomeError):
            await client.update_margin_mode(1, isolated=False)
    assert len(posts) == 1


async def test_tpsl_cancel_signature_and_no_fake_nonce(config, backend, signer):
    posts = []

    async def transport(request):
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"success": True, "cancelled_count": "0"}})
        return await backend(request)

    async with RiseXClient(
        config, account=backend.account, signer=signer, transport=httpx.MockTransport(transport)
    ) as client:
        result = await client.cancel_all_tpsl_orders(1)
    assert result.cancelled_count == 0 and len(posts) == 1
    body = posts[0]
    sig = base64.b64decode(body["signature"])
    assert len(sig) == 65 and "nonce_anchor" not in body
    domain = backend.domain
    domain_hash = keccak(
        keccak(
            text=(
                "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
            )
        )
        + keccak(text=domain["name"])
        + keccak(text=domain["version"])
        + word(domain["chain_id"])
        + word(int(domain["verifying_contract"], 16))
    )
    struct_hash = keccak(
        keccak(text="CancelAllTpslOrders(address account,uint64 marketId,uint32 deadline)")
        + word(int(backend.account, 16))
        + word(1)
        + word(body["deadline"])
    )
    signature = keys.Signature(sig[:64] + bytes([sig[64] - 27]))
    assert (
        signature.recover_public_key_from_msg_hash(
            keccak(b"\x19\x01" + domain_hash + struct_hash)
        ).to_checksum_address()
        == signer.address
    )
