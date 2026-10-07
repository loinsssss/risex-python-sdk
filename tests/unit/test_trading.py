import asyncio
import base64
import time
from decimal import Decimal

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils.crypto import keccak
from pydantic import ValidationError

from risex import (
    APIError,
    AuthenticationError,
    OrderRequest,
    OrderSide,
    OrderType,
    PrecisionError,
    ProtocolError,
    RiseXClient,
    TimeInForce,
    UnknownOutcomeError,
)
from risex.signing import typed_data


def client_for(config, backend, signer):
    return RiseXClient(
        config, account=backend.account, signer=signer, transport=httpx.MockTransport(backend)
    )


def standard_signature(encoded):
    compact = base64.b64decode(encoded)
    s = int.from_bytes(compact[32:], "big")
    return compact[:32] + (s & ((1 << 255) - 1)).to_bytes(32, "big") + bytes([27 + (s >> 255)])


async def test_full_rest_lifecycle_uses_distinct_nonces(config, backend, signer, order_request):
    async with client_for(config, backend, signer) as client:
        submission = await client.place_order(order_request)
        assert submission.filled_quantity is None
        order = await client.get_order(submission.order_id)
        assert not order.terminal and order.filled_size == 0
        assert len((await client.get_open_orders()).orders) == 1
        cancellation = await client.cancel_order(submission.order_id)
        assert cancellation.success
        terminal = await client.wait_for_order(submission.order_id)
        assert terminal.status == "ORDER_STATUS_CANCELLED"
    permits = [body["permit"] for _, body in backend.posts]
    assert [(permit["nonce_anchor"], permit["nonce_bitmap_index"]) for permit in permits] == [
        ("1", 0),
        ("1", 1),
    ]
    assert backend.posts[0][1]["size_steps"] == 1000
    assert backend.posts[0][1]["price_ticks"] == 600000
    assert backend.posts[0][1]["no_retry"] is True


async def test_permit_hash_matches_the_actual_submitted_fields(config, backend, signer):
    request = OrderRequest(
        market_id=1,
        side=OrderSide.SELL,
        quantity=Decimal("0.001"),
        price=Decimal("60000"),
        post_only=True,
        reduce_only=True,
        builder_id=7,
        builder_fee_bps=10,
        client_order_id=99,
    )
    async with client_for(config, backend, signer) as client:
        await client.place_order(request)
    body = backend.posts[0][1]
    flags = 1 | (1 << 1) | (1 << 2) | (1 << 5)
    packed = (1 << 70) | (1000 << 38) | (600000 << 14) | (flags << 6) | 2

    def word(value):
        return int(value).to_bytes(32, "big")

    action_hash = keccak(
        keccak(text="RISE_PERPS_PLACE_ORDER_V1")
        + word(7)
        + word(packed)
        + word(7)
        + word(10)
        + word(99)
        + word(0)
    )
    domain = {
        "name": "RISEx",
        "version": "1",
        "chainId": 4153,
        "verifyingContract": backend.domain["verifying_contract"],
    }
    signed = typed_data(
        domain,
        "VerifyWitness",
        {
            "account": backend.account,
            "target": backend.system["addresses"]["router"],
            "hash": action_hash,
            "nonceAnchor": int(body["permit"]["nonce_anchor"]),
            "nonceBitmap": body["permit"]["nonce_bitmap_index"],
            "deadline": body["permit"]["deadline"],
        },
    )
    recovered = Account.recover_message(
        encode_typed_data(full_message=signed),
        signature=standard_signature(body["permit"]["signature"]),
    )
    assert recovered == signer.address


async def test_registration_signs_two_exact_structs(config, backend, signer, account_signer):
    async with client_for(config, backend, signer) as client:
        result = await client.register_signer(account_signer, expiration=int(time.time()) + 3600)
        assert result.success
        assert (await client.get_signer_status()).active
    _, body = backend.posts[0]
    domain = {
        "name": "RISEx",
        "version": "1",
        "chainId": 4153,
        "verifyingContract": backend.domain["verifying_contract"],
    }
    common = {"account": backend.account, "nonceAnchor": 1, "nonceBitmap": 0}
    reg = typed_data(
        domain,
        "RegisterSigner",
        {
            **common,
            "signer": signer.address,
            "message": body["message"],
            "expiration": int(body["expiration"]),
        },
    )
    verify = typed_data(domain, "VerifySigner", common)
    assert (
        Account.recover_message(
            encode_typed_data(full_message=reg), signature=body["account_signature"]
        )
        == backend.account
    )
    assert (
        Account.recover_message(
            encode_typed_data(full_message=verify), signature=body["signer_signature"]
        )
        == signer.address
    )


async def test_revocation_signature_uses_account_key(config, backend, signer, account_signer):
    async with client_for(config, backend, signer) as client:
        assert (await client.revoke_signer(account_signer)).success
    path, body = backend.posts[0]
    assert path == "/v1/auth/revoke-signer"
    data = typed_data(
        {
            "name": "RISEx",
            "version": "1",
            "chainId": 4153,
            "verifyingContract": backend.domain["verifying_contract"],
        },
        "RevokeSigner",
        {
            "account": backend.account,
            "signer": signer.address,
            "nonceAnchor": int(body["nonce_anchor"]),
            "nonceBitmap": body["nonce_bitmap_index"],
        },
    )
    assert (
        Account.recover_message(
            encode_typed_data(full_message=data), signature=body["account_signature"]
        )
        == account_signer.address
    )


@pytest.mark.parametrize("operation", ["register", "revoke"])
async def test_delegated_key_cannot_authorize_its_own_registration_or_revocation(
    config, backend, signer, operation
):
    async with client_for(config, backend, signer) as client:
        with pytest.raises(AuthenticationError, match="account_signer"):
            if operation == "register":
                await client.register_signer(signer, expiration=int(time.time()) + 3600)
            else:
                await client.revoke_signer(signer)
    assert not backend.requests


async def test_signed_writes_are_serialized_and_use_unique_nonces(
    config, backend, signer, order_request
):
    async with client_for(config, backend, signer) as client:
        await asyncio.gather(*(client.place_order(order_request) for _ in range(8)))
    assert [body["permit"]["nonce_bitmap_index"] for _, body in backend.posts] == list(range(8))
    assert len({body["client_order_id"] for _, body in backend.posts}) == 8


@pytest.mark.parametrize("behavior", ["timeout", "server_error", "malformed"])
async def test_ambiguous_submission_is_never_retried_and_can_be_reconciled(
    config, backend, signer, order_request, behavior
):
    backend.post_behavior = behavior
    async with client_for(config, backend, signer) as client:
        with pytest.raises(UnknownOutcomeError) as error:
            await client.place_order(order_request)
        assert len(backend.posts) == 1
        assert error.value.context.client_order_id
        resolution = await client.reconcile_submission(error.value)
        assert resolution.resolved and resolution.nonce_consumed
        assert len(resolution.orders) == 1
        backend.post_behavior = "success"
        await client.place_order(order_request)
    assert backend.posts[1][1]["permit"]["nonce_bitmap_index"] == 1


async def test_confirmed_rejection_does_not_recycle_the_nonce(
    config, backend, signer, order_request
):
    backend.post_behavior = "reject"
    async with client_for(config, backend, signer) as client:
        with pytest.raises(APIError):
            await client.place_order(order_request)
        assert len(backend.posts) == 1
        backend.post_behavior = "success"
        await client.place_order(order_request)
    assert backend.posts[1][1]["permit"]["nonce_bitmap_index"] == 1


async def test_malformed_mutation_model_preserves_request_id(
    config, backend, signer, order_request
):
    async def handler(request):
        response = await backend(request)
        if request.method == "POST":
            return httpx.Response(
                200, json={"data": {"incomplete": True}, "request_id": "recover-me"}
            )
        return response

    async with RiseXClient(
        config, account=backend.account, signer=signer, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(UnknownOutcomeError) as error:
            await client.place_order(order_request)
        assert error.value.request_id == "recover-me"
        assert len(backend.posts) == 1


async def test_cancellation_during_submission_preserves_uncertainty(
    config, backend, signer, order_request
):
    submitted = asyncio.Event()

    async def handler(request):
        if request.method != "POST":
            return await backend(request)
        await backend(request)
        submitted.set()
        await asyncio.Event().wait()

    async with RiseXClient(
        config, account=backend.account, signer=signer, transport=httpx.MockTransport(handler)
    ) as client:
        task = asyncio.create_task(client.place_order(order_request))
        await asyncio.wait_for(submitted.wait(), timeout=1)
        task.cancel()
        with pytest.raises(UnknownOutcomeError) as error:
            await task
        assert (await client.reconcile_submission(error.value)).resolved
    assert len(backend.posts) == 1


async def test_invalid_precision_is_rejected_before_signing(config, backend, signer, order_request):
    request = order_request.model_copy(update={"price": Decimal("60000.01")})
    async with client_for(config, backend, signer) as client:
        with pytest.raises(PrecisionError):
            await client.place_order(request)
    assert not backend.posts
    assert not any("/nonce-state/" in request.url.path for request in backend.requests)


async def test_overflow_is_rejected_before_reserving_a_nonce(
    config, backend, signer, order_request
):
    request = order_request.model_copy(update={"price": Decimal("1677721.6")})
    async with client_for(config, backend, signer) as client:
        with pytest.raises(ValueError, match="uint24"):
            await client.place_order(request)
    assert not backend.posts


@pytest.mark.parametrize("time_in_force", [None, TimeInForce.FOK, TimeInForce.IOC])
async def test_market_order_zero_price_signed_payload(config, backend, signer, time_in_force):
    request = OrderRequest(
        market_id=1,
        side=OrderSide.SELL,
        quantity=Decimal("0.001"),
        price=Decimal("0"),
        order_type=OrderType.MARKET,
        time_in_force=time_in_force,
        reduce_only=True,
        client_order_id=99,
    )
    async with client_for(config, backend, signer) as client:
        await client.place_order(request)
    body = backend.posts[0][1]
    tif = int(time_in_force if time_in_force is not None else TimeInForce.IOC)
    assert body["order_type"] == 0 and body["time_in_force"] == tif
    assert body["price_ticks"] == 0
    assert body["size_steps"] == 1000
    assert body["reduce_only"] is True
    # Independently reconstruct the native zero-price order and recover its signer.
    flags = 1 | (1 << 2) | (tif << 6)
    packed = (1 << 70) | (1000 << 38) | (flags << 6) | 2

    def word(value):
        return int(value).to_bytes(32, "big")

    action_hash = keccak(
        keccak(text="RISE_PERPS_PLACE_ORDER_V1")
        + word(5)
        + word(packed)
        + word(0)
        + word(99)
        + word(0)
    )
    signed = typed_data(
        {
            "name": "RISEx",
            "version": "1",
            "chainId": 4153,
            "verifyingContract": backend.domain["verifying_contract"],
        },
        "VerifyWitness",
        {
            "account": backend.account,
            "target": backend.system["addresses"]["router"],
            "hash": action_hash,
            "nonceAnchor": int(body["permit"]["nonce_anchor"]),
            "nonceBitmap": body["permit"]["nonce_bitmap_index"],
            "deadline": body["permit"]["deadline"],
        },
    )
    assert (
        Account.recover_message(
            encode_typed_data(full_message=signed),
            signature=standard_signature(body["permit"]["signature"]),
        )
        == signer.address
    )


@pytest.mark.parametrize(
    ("order_type", "price", "message"),
    [
        (OrderType.LIMIT, Decimal("0"), "limit orders require a positive price"),
        (OrderType.MARKET, Decimal("60000"), "market orders require price=0"),
    ],
)
def test_order_type_price_semantics_rejected(order_type, price, message):
    with pytest.raises(ValidationError, match=message):
        OrderRequest(
            market_id=1,
            side=OrderSide.SELL,
            quantity=Decimal("0.001"),
            price=price,
            order_type=order_type,
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"quantity": 0.1},
        {"price": 60000.0},
        {"side": True},
        {"order_type": OrderType.MARKET, "price": Decimal("0"), "time_in_force": TimeInForce.GTC},
        {"order_type": OrderType.MARKET, "price": Decimal("0"), "post_only": True},
        {"time_in_force": TimeInForce.GTT},
        {"builder_fee_bps": 10},
    ],
)
def test_unsafe_order_shapes_are_rejected(updates):
    values = dict(
        market_id=1, side=OrderSide.BUY, quantity=Decimal("0.001"), price=Decimal("60000")
    )
    values.update(updates)
    with pytest.raises(ValidationError):
        OrderRequest(**values)


async def test_wrong_domain_configuration_blocks_signing(config, backend, signer):
    backend.system["addresses"]["auth"] = backend.system["addresses"]["router"]
    async with client_for(config, backend, signer) as client:
        with pytest.raises(ProtocolError):
            await client.initialize()
    assert not backend.posts


async def test_cancel_all_receipt_and_resting_id_guard(config, backend, signer, order_request):
    async with client_for(config, backend, signer) as client:
        submission = await client.place_order(order_request)
        with pytest.raises(ValueError, match="resting_order_id"):
            await client.cancel_order(submission.order_id, market_id=1, resting_order_id=999)
        assert (await client.cancel_all_orders(1)).success
    assert len(backend.posts) == 2


async def test_unknown_cancellation_queries_current_state(config, backend, signer, order_request):
    async with client_for(config, backend, signer) as client:
        submission = await client.place_order(order_request)
        backend.post_behavior = "timeout"
        with pytest.raises(UnknownOutcomeError) as error:
            await client.cancel_order(submission.order_id, market_id=1)
        assert (await client.reconcile_submission(error.value)).resolved
    assert len(backend.posts) == 2


async def test_reconciliation_rejects_another_accounts_order(
    config, backend, signer, order_request
):
    async with client_for(config, backend, signer) as client:
        submission = await client.place_order(order_request)
        backend.post_behavior = "timeout"
        with pytest.raises(UnknownOutcomeError) as error:
            await client.cancel_order(submission.order_id, market_id=1)
        backend.orders[submission.order_id]["sender"] = "0x" + "33" * 20
        with pytest.raises(ProtocolError, match="another account"):
            await client.reconcile_submission(error.value)
