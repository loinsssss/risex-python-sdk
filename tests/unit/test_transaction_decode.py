import httpx
import pytest

from risex import APIError, DecodedError, DecodedTransaction, ProtocolError, RiseXClient

TX_HASH = "0x" + "ab" * 32


@pytest.mark.parametrize("wrapped", [False, True])
async def test_decode_reverted_transaction_retains_details_without_credentials(config, wrapped):
    payload = {
        "tx_hash": TX_HASH.upper().replace("0X", "0x"),
        "success": False,
        "error": {
            "selector": "3af8647b",
            "signature": "InsufficientMargin(uint256,uint256)",
            "name": "InsufficientMargin",
            "parameters": ["340282366920938463463374607431768211455", "0"],
            "message": "Insufficient margin for this transaction",
            "provider_detail": "retained",
        },
    }
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == f"/v1/tx/{TX_HASH}"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"data": payload} if wrapped else payload)

    async with RiseXClient(config, transport=httpx.MockTransport(handler)) as client:
        result = await client.decode_transaction(TX_HASH)
    assert len(requests) == 1
    assert isinstance(result, DecodedTransaction)
    assert result.success is False
    assert isinstance(result.error, DecodedError)
    assert result.error.selector == "3af8647b"
    assert result.error.signature == "InsufficientMargin(uint256,uint256)"
    assert result.error.name == "InsufficientMargin"
    assert result.error.parameters == ("340282366920938463463374607431768211455", "0")
    assert result.error.message == "Insufficient margin for this transaction"
    assert result.error.model_extra["provider_detail"] == "retained"


@pytest.mark.parametrize(
    "payload",
    [
        {"tx_hash": TX_HASH, "success": True},
        {"success": True, "error": None},
        {"tx_hash": TX_HASH, "success": False, "error": None},
    ],
)
async def test_decode_outcome_without_revert_details(config, payload):
    async with RiseXClient(
        config, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        result = await client.decode_transaction(TX_HASH)
    assert result.success is payload["success"]
    assert result.error is None


@pytest.mark.parametrize(
    "tx_hash",
    [None, True, 1, "", "ab" * 32, "0x" + "ab" * 31, "0x" + "ag" * 32, TX_HASH + "/x"],
)
async def test_decode_invalid_hash_never_sends_request(config, tx_hash):
    def handler(_):
        pytest.fail("invalid hash must fail before a network request")

    async with RiseXClient(config, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="tx_hash must"):
            await client.decode_transaction(tx_hash)


@pytest.mark.parametrize(
    "payload",
    [
        {"tx_hash": "0x" + "cd" * 32, "success": False},
        {"tx_hash": "invalid", "success": False},
        {"tx_hash": TX_HASH},
        {"tx_hash": TX_HASH, "success": "false"},
        {"tx_hash": TX_HASH, "success": 0},
        {"tx_hash": TX_HASH, "success": True, "error": {"name": "Reverted"}},
        {"tx_hash": TX_HASH, "success": False, "error": {"parameters": [123]}},
    ],
)
async def test_decode_rejects_wrong_identity_and_malformed_outcomes(config, payload):
    async with RiseXClient(
        config, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": payload}))
    ) as client:
        with pytest.raises(ProtocolError):
            await client.decode_transaction(TX_HASH)


async def test_decode_http_error_still_preserves_api_failure(config):
    response = httpx.Response(404, json={"error": {"code": 5, "message": "Transaction not found"}})
    async with RiseXClient(config, transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(APIError) as error:
            await client.decode_transaction(TX_HASH)
    assert error.value.status_code == 404
    assert error.value.code == 5
    assert str(error.value) == "Transaction not found"


async def test_decode_data_error_does_not_change_other_get_error_handling(config):
    response = httpx.Response(200, json={"error": {"code": 13, "message": "Backend error"}})
    async with RiseXClient(config, transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(APIError, match="Backend error"):
            await client.get_system_config()
