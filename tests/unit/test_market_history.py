from decimal import Decimal

import httpx
import pytest

from risex import APIError, ProtocolError, RiseXClient

NS = 10**9


def funding_record(end_s, rate="-0.000008922550399859"):
    return {
        "funding_rate": rate,
        "accumulated_funding": "213.275807696824943633",
        "index_price": "81631.869562724168",
        "start_time": str((end_s - 3600) * NS),
        "end_time": str(end_s * NS),
        "block_number": "24618241",
        "block_time": str(end_s * NS),
        "tx_hash": "0x0a084c4da4f77a5be8c80f32a2477071d02713c078c07df55c3b2c588da64d3f",
    }


def funding_page(page, records, has_next, market_id="1"):
    return {
        "data": {
            "market_id": market_id,
            "records": records,
            "page": page,
            "has_next_page": has_next,
        },
        "request_id": "test",
    }


def candle(time_s, interval="1m", market_id="1"):
    return {
        "market_id": market_id,
        "interval": interval,
        "time": str(time_s * NS),
        "low": "81544.5",
        "high": "81552.8",
        "open": "81552.5",
        "close": "81552.6",
        "volume": "0.52815",
    }


async def test_funding_history_request_and_exact_values(config):
    def handler(request):
        assert request.url.path == "/v1/markets/id/1/funding-rate-history"
        assert request.url.params["page"] == "2"
        assert request.url.params["limit"] == "1000"
        assert request.url.params["start_time"] == str(1_000 * NS)
        assert request.url.params["end_time"] == str(9_000 * NS)
        return httpx.Response(200, json=funding_page(2, [funding_record(7_200)], False))

    async with RiseXClient(config, transport=httpx.MockTransport(handler)) as client:
        result = await client.get_funding_rate_history(
            1, page=2, limit=1000, start_time=1_000 * NS, end_time=9_000 * NS
        )
    record = result.records[0]
    assert record.funding_rate == Decimal("-0.000008922550399859")
    assert record.index_price == Decimal("81631.869562724168")
    assert record.end_time - record.start_time == 3_600 * NS
    assert result.has_next_page is False


async def test_funding_history_iterates_all_pages(config):
    pages = {
        "1": funding_page(1, [funding_record(10_800), funding_record(7_200)], True),
        "2": funding_page(2, [funding_record(3_600)], False),
    }
    seen = []

    def handler(request):
        seen.append(request.url.params["page"])
        return httpx.Response(200, json=pages[request.url.params["page"]])

    async with RiseXClient(config, transport=httpx.MockTransport(handler)) as client:
        records = [r async for r in client.iter_funding_rate_history(1, page_size=2)]
    assert seen == ["1", "2"]
    assert [r.end_time // NS for r in records] == [10_800, 7_200, 3_600]


async def test_funding_history_page_guard_reports_incomplete_results(config):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json=funding_page(int(request.url.params["page"]), [funding_record(3_600)], True)
        )
    )
    async with RiseXClient(config, transport=transport) as client:
        with pytest.raises(ProtocolError, match="max_pages"):
            _ = [r async for r in client.iter_funding_rate_history(1, max_pages=2)]


@pytest.mark.parametrize(
    "payload",
    [
        funding_page(1, [], False, market_id="2"),  # another market
        funding_page(3, [], False),  # page we didn't ask for
        funding_page(1, [{**funding_record(3_600), "funding_rate": 0.1}], False),  # float
    ],
)
async def test_funding_history_rejects_inconsistent_responses(config, payload):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    async with RiseXClient(config, transport=transport) as client:
        with pytest.raises(ProtocolError):
            await client.get_funding_rate_history(1)


async def test_funding_history_validates_arguments_without_connecting(config):
    def handler(_):
        raise AssertionError("must not send a request")

    async with RiseXClient(config, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await client.get_funding_rate_history(1, limit=1001)
        with pytest.raises(ValueError):
            await client.get_funding_rate_history(1, start_time=2, end_time=1)
        with pytest.raises(ValueError):
            await client.get_funding_rate_history(0)


async def test_candles_request_uses_nanosecond_interval_and_range(config):
    def handler(request):
        assert request.url.path == "/v1/markets/id/1/trading-view-data"
        assert request.url.params["interval"] == str(3_600 * NS)
        assert request.url.params["from"] == str(1_000 * NS)
        assert request.url.params["to"] == str(9_000 * NS)
        rows = [candle(3_600, "1h"), candle(7_200, "1h")]
        return httpx.Response(200, json={"data": {"data": rows}})

    async with RiseXClient(config, transport=httpx.MockTransport(handler)) as client:
        candles = await client.get_candles(
            1, interval="1h", start_time=1_000 * NS, end_time=9_000 * NS
        )
    assert [c.time // NS for c in candles] == [3_600, 7_200]
    assert candles[0].open == Decimal("81552.5")
    assert candles[0].volume == Decimal("0.52815")


async def test_empty_candle_range(config):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"data": {"data": []}}))
    async with RiseXClient(config, transport=transport) as client:
        assert await client.get_candles(1, interval="1m", start_time=0, end_time=NS) == ()


async def test_candles_reject_silent_interval_fallback(config):
    # RISEx answers unsupported durations with 1m candles instead of an error.
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"data": {"data": [candle(60, "1m")]}})
    )
    async with RiseXClient(config, transport=transport) as client:
        with pytest.raises(ProtocolError, match="interval"):
            await client.get_candles(1, interval="4h", start_time=0, end_time=86_400 * NS)


async def test_candles_validate_arguments_without_connecting(config):
    def handler(_):
        raise AssertionError("must not send a request")

    async with RiseXClient(config, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="interval"):
            await client.get_candles(1, interval="7m", start_time=0, end_time=NS)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await client.get_candles(1, interval="1m", start_time=NS, end_time=0)
        with pytest.raises(ValueError):
            await client.get_candles(1, interval="1m", start_time=-1, end_time=NS)


async def test_unknown_market_surfaces_api_error(config):
    body = {"error": {"code": "InvalidArgument", "message": "invalid market_id"}, "request_id": "x"}
    transport = httpx.MockTransport(lambda _: httpx.Response(400, json=body))
    async with RiseXClient(config, transport=transport) as client:
        with pytest.raises(APIError, match="invalid market_id"):
            await client.get_candles(999, interval="1m", start_time=0, end_time=NS)
