import httpx
import pytest

from risex import ProtocolError, RiseXClient


def order(account, number, stop_type="TAKE_PROFIT", status="TPSL_ORDER_STATUS_ACCEPTED"):
    return {
        "order_id": f"conditional-{number}",
        "account": account,
        "market_id": "1",
        "side": "SELL",
        "size": "0.001",
        "stop_type": stop_type,
        "order_type": "MARKET",
        "stop_price": "70000",
        "limit_price": "0",
        "stop_price_option": "MARK_PRICE",
        "status": status,
        "tif": "GTC",
        "created_at": "1800000000000000000",
        "expires_at": "0",
        "triggered_at": "0",
        "triggered_price": "",
        "size_percent_bps": 10000,
        "filled_size": "0",
    }


class TpslBackend:
    def __init__(self, backend):
        self.backend = backend
        self.rows = [
            order(backend.account, 1),
            order(backend.account, 2, "STOP_LOSS", "TPSL_ORDER_STATUS_TRIGGERED"),
        ]
        self.queries = []
        self.transform = lambda data: data

    async def __call__(self, request):
        if request.url.path != "/v1/orders/tpsl":
            return await self.backend(request)
        assert request.method == "GET"
        query = request.url.params
        self.queries.append(query)
        assert query["account"] == self.backend.account
        assert query["stop_type"] == "STOP_TYPE_NONE"
        page, limit = int(query["page"]), int(query["limit"])
        data = {
            "orders": self.rows[(page - 1) * limit : page * limit],
            "total": str(len(self.rows)),
            "page": page,
            "limit": limit,
        }
        return httpx.Response(200, json={"data": self.transform(data)})


async def test_both_stop_types_and_triggered_orders_in_snapshot(config, backend):
    server = TpslBackend(backend)
    async with RiseXClient(
        config, account=backend.account, transport=httpx.MockTransport(server)
    ) as c:
        rows = [row async for row in c.iter_tpsl_orders(page_size=1)]
        assert len(rows) == 2 and all(row.active for row in rows)
        assert rows[1].stop_type == "STOP_LOSS"
        assert rows[0].triggered_price is None
        assert (await c.get_account_snapshot()).conditional_orders is None
        snapshot = await c.get_account_snapshot(include_conditional_orders=True)
        assert len(snapshot.conditional_orders) == 2
        assert server.queries[-1].get_list("statuses") == [
            "TPSL_ORDER_STATUS_ACCEPTED",
            "TPSL_ORDER_STATUS_TRIGGERED",
        ]
        assert not backend.posts


async def test_empty_conditional_snapshot_is_distinct_from_not_queried(config, backend):
    server = TpslBackend(backend)
    server.rows = []
    async with RiseXClient(
        config, account=backend.account, transport=httpx.MockTransport(server)
    ) as c:
        assert (
            await c.get_account_snapshot(include_conditional_orders=True)
        ).conditional_orders == ()


@pytest.mark.parametrize(
    "field,value",
    [
        ("account", "0x" + "33" * 20),
        ("market_id", "2"),
        ("status", "TPSL_ORDER_STATUS_CANCELLED"),
        ("status", "UNKNOWN_FUTURE_STATUS"),
    ],
)
async def test_scope_and_filters_fail_closed(config, backend, field, value):
    server = TpslBackend(backend)
    server.rows[0][field] = value
    async with RiseXClient(
        config, account=backend.account, transport=httpx.MockTransport(server)
    ) as c:
        with pytest.raises(ProtocolError):
            await c.get_tpsl_orders(
                market_id=1, statuses=("TPSL_ORDER_STATUS_ACCEPTED", "TPSL_ORDER_STATUS_TRIGGERED")
            )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: {**d, "page": 2},
        lambda d: {**d, "limit": 1},
        lambda d: {**d, "orders": d["orders"][:1]},
        lambda d: {**d, "orders": [d["orders"][0]] * 2},
    ],
)
async def test_incomplete_or_inconsistent_pagination_is_not_success(config, backend, mutation):
    server = TpslBackend(backend)
    server.transform = mutation
    async with RiseXClient(
        config, account=backend.account, transport=httpx.MockTransport(server)
    ) as c:
        with pytest.raises(ProtocolError):
            await c.get_tpsl_orders()


async def test_nonadvancing_and_bounded_pages(config, backend):
    server = TpslBackend(backend)
    async with RiseXClient(
        config, account=backend.account, transport=httpx.MockTransport(server)
    ) as c:
        with pytest.raises(ProtocolError, match="max_pages"):
            _ = [row async for row in c.iter_tpsl_orders(page_size=1, max_pages=1)]
        server.rows[1]["order_id"] = server.rows[0]["order_id"]
        with pytest.raises(ProtocolError, match="repeated"):
            _ = [row async for row in c.iter_tpsl_orders(page_size=1)]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"statuses": "TPSL_ORDER_STATUS_ACCEPTED"},
        {"stop_type": "bad"},
        {"limit": True},
        {"market_id": 0},
        {"start_time": 100, "end_time": 99},
    ],
)
async def test_invalid_filters_do_not_connect(config, backend, kwargs):
    async with RiseXClient(config, account=backend.account) as c:
        with pytest.raises((TypeError, ValueError)):
            await c.get_tpsl_orders(**kwargs)
