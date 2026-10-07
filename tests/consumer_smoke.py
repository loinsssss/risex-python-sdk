"""Run with -I after copying this file and fixtures/ outside the checkout."""

import asyncio
import importlib.util
import json
import socket
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
original_connect = socket.socket.connect


def local_connect(self, target):
    if target[0] not in ("127.0.0.1", "::1"):
        raise AssertionError("Consumer smoke test must not access the external network")
    return original_connect(self, target)


socket.socket.connect = local_connect

import httpx  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402

import risex  # noqa: E402
from risex import (  # noqa: E402
    ConnectionEvent,
    LocalOrderbook,
    LocalSigner,
    OrderRequest,
    OrdersEvent,
    OrderSide,
    OrderType,
    RiseXClient,
    RiseXConfig,
    UnknownOutcomeError,
)


def module_from_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def main():
    assert "site-packages" in risex.__file__
    assert RiseXConfig().mainnet is True
    assert RiseXConfig(mainnet=False).mainnet is False
    for name in risex.__all__:
        assert getattr(risex, name) is not None
    backend_module = module_from_file("fixture_backend", ROOT / "fixtures/backend.py")
    market = json.loads((ROOT / "fixtures/markets.json").read_text())["data"]["markets"][0]
    book_reply = json.loads((ROOT / "fixtures/orderbook.json").read_text())
    snapshot = json.loads((ROOT / "fixtures/ws_orderbook_snapshot.json").read_text())
    owner = LocalSigner("0x" + "11" * 32)  # Public offline test vector.
    signer = LocalSigner("0x" + "22" * 32)
    backend = backend_module.Backend(owner.address, signer.address, market)

    async def http_handler(request):
        if request.url.path in {"/v1/auth/login", "/v1/auth/refresh"}:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "access_token": "offline-access",
                        "refresh_token": "offline-refresh",
                        "expires_in": 900,
                        "token_type": "Bearer",
                    }
                },
            )
        if request.url.path == "/v1/auth/logout":
            return httpx.Response(200, json={"data": {"success": True}})
        if request.url.path == "/v1/user/fees":
            assert request.headers["authorization"] == "Bearer offline-access"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "tier": 1,
                        "taker_bps": 2,
                        "maker_bps": 0.5,
                        "weighted_14d_volume_usd": "0",
                        "applied_at": "",
                        "schedule": [],
                    }
                },
            )
        if request.url.path == "/v1/orders/tpsl":
            assert request.url.params["stop_type"] == "STOP_TYPE_NONE"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "orders": [],
                        "total": "0",
                        "page": 1,
                        "limit": 100,
                    }
                },
            )
        if request.url.path == "/v1/orderbook":
            return httpx.Response(200, json=book_reply)
        return await backend(request)

    async def ws_handler(connection):
        frame = json.loads(await connection.recv())
        if frame["method"] == "auth_v2":
            assert frame["params"]["account"] == owner.address
            assert frame["params"]["signer"] == signer.address
            await connection.send(
                json.dumps(
                    {
                        "method": "auth_v2",
                        "status": "success",
                        "data": {"account": owner.address, "signer": signer.address},
                    }
                )
            )
            frame = json.loads(await connection.recv())
        channel = frame["params"]["channel"]
        data = (
            snapshot
            if channel == "orderbook"
            else {
                "channel": "orders",
                "type": "snapshot",
                "worker_timestamp": "1",
                "data": list(backend.orders.values()),
            }
        )
        await connection.send(json.dumps(data))
        await connection.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "status": "success",
                    "channel": channel,
                    "data": {"market_ids": [1]},
                }
            )
        )
        await connection.wait_closed()

    async with serve(ws_handler, "127.0.0.1", 0) as server:
        config = RiseXConfig(
            mainnet=False,
            websocket_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
            rest_requests_per_second=None,
            websocket_requests_per_second=None,
        )
        async with RiseXClient(
            config,
            account=owner.address,
            signer=signer,
            transport=httpx.MockTransport(http_handler),
        ) as client:
            await client.initialize()
            await client.get_markets()
            await client.get_orderbook(1)
            await client.get_signer_status()
            await client.login()
            assert (await client.get_user_fees()).taker_bps == Decimal(2)
            await client.refresh_session()
            assert await client.logout()
            assert (
                await client.get_account_snapshot(include_conditional_orders=True)
            ).conditional_orders == ()
            assert (await client.register_signer(owner, expiration=int(time.time()) + 3600)).success
            assert (await client.get_position(1)).position.size == 0
            receipt = await client.place_order(
                OrderRequest(
                    market_id=1,
                    side=OrderSide.BUY,
                    quantity=Decimal("0.001"),
                    price=Decimal("60000"),
                    order_type=OrderType.MARKET,
                )
            )
            assert (await client.get_order(receipt.order_id)).id == receipt.order_id
            assert (await client.cancel_order(receipt.order_id, market_id=1)).success
            assert (await client.wait_for_order(receipt.order_id)).terminal
            assert not (await client.get_account_snapshot()).open_orders
            await client.get_positions()
            await client.get_trade_history()
            await client.get_order_history()
            await client.cancel_all_orders(1)
            request = OrderRequest(
                market_id=1,
                side=OrderSide.BUY,
                quantity=Decimal("0.001"),
                price=Decimal("60000"),
            )
            backend.post_behavior = "timeout"
            try:
                await client.place_order(request)
            except UnknownOutcomeError as error:
                assert (await client.reconcile_submission(error)).resolved
            else:
                raise AssertionError("Ambiguous POST should expose recovery identifiers")
            backend.post_behavior = "success"
            async with client.stream_orderbook(market_ids=[1]) as stream:
                assert isinstance(await anext(stream), ConnectionEvent)
                book = LocalOrderbook(1)
                book.apply(await anext(stream))
                assert book.valid
            async with client.stream_orders(market_ids=[1]) as stream:
                assert isinstance(await anext(stream), ConnectionEvent)
                assert isinstance(await anext(stream), OrdersEvent)
            assert (await client.revoke_signer(owner)).success
    print(f"Installed risex {risex.__version__}: isolated consumer lifecycle and streams passed")


if __name__ == "__main__":
    asyncio.run(main())
