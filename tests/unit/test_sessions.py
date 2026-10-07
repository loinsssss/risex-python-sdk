import asyncio
import json
import time
import traceback
from decimal import Decimal

import httpx
import pytest
from eth_abi import encode
from eth_account import Account
from eth_account.messages import SignableMessage
from eth_utils import keccak

from risex import AuthenticationError, ProtocolError, RiseXClient, TransportError


class SessionBackend:
    """Only deterministic fixture keys/tokens; no network or real credentials."""

    def __init__(self, backend):
        self.backend = backend
        self.posts = []
        self.refresh_count = 0
        self.login_count = 0
        self.access = "fixture-access-0"
        self.refresh = "fixture-refresh-0"
        self.refresh_error = None
        self.login_error = None
        self.ttl = 900
        self.fee_status = 200
        self.signer = None

    async def __call__(self, request):
        path = request.url.path
        if path == "/v1/auth/login":
            self.login_count += 1
            data = json.loads(request.content)
            self.posts.append((path, data))
            assert "authorization" not in request.headers
            assert data["account"] == self.backend.account
            # Independent ABI/EIP-712 encoding, not the SDK's typed_data helper.
            domain = self.backend.domain
            domain_hash = keccak(
                encode(
                    ["bytes32", "bytes32", "bytes32", "uint256", "address"],
                    [
                        keccak(
                            text="EIP712Domain(string name,string version,"
                            "uint256 chainId,address verifyingContract)"
                        ),
                        keccak(text=domain["name"]),
                        keccak(text=domain["version"]),
                        int(domain["chain_id"]),
                        domain["verifying_contract"],
                    ],
                )
            )
            message_hash = keccak(
                encode(
                    ["bytes32", "address", "uint256", "uint32"],
                    [
                        keccak(text="Login(address account,uint256 nonce,uint32 deadline)"),
                        data["account"],
                        int(data["nonce"], 16),
                        data["deadline"],
                    ],
                )
            )
            recovered = Account.recover_message(
                SignableMessage(b"\x01", domain_hash, message_hash), signature=data["signature"]
            )
            assert recovered == (data.get("signer") or data["account"])
            assert 0 < data["deadline"] - time.time() <= 60
            self.signer = data.get("signer")
            if self.login_error:
                return self.login_error
            return self.tokens()
        if path == "/v1/auth/refresh":
            self.refresh_count += 1
            data = json.loads(request.content)
            self.posts.append((path, data))
            assert data["refresh_token"] == self.refresh
            assert "authorization" not in request.headers
            self.refresh = f"fixture-refresh-{self.refresh_count}"
            self.access = f"fixture-access-{self.refresh_count}"
            if self.refresh_error:
                if isinstance(self.refresh_error, BaseException):
                    raise self.refresh_error
                return self.refresh_error
            return self.tokens()
        if path == "/v1/auth/logout":
            self.posts.append((path, json.loads(request.content)))
            assert request.headers["authorization"] == f"Bearer {self.access}"
            return httpx.Response(200, json={"data": {"success": True}})
        if path == "/v1/user/fees":
            assert request.method == "GET"
            assert not request.url.params
            assert request.headers["authorization"] == f"Bearer {self.access}"
            if self.fee_status != 200:
                return httpx.Response(self.fee_status, json={"message": self.access})
            # JSON number tokens must retain their decimal value without float rounding.
            return httpx.Response(
                200,
                content=b'{"data":{"tier":0,"taker_bps":3.123456789123456789,"maker_bps":-0.125,"weighted_14d_volume_usd":"0","applied_at":"","schedule":[]}}',
            )
        assert "authorization" not in request.headers
        return await self.backend(request)

    def tokens(self):
        return httpx.Response(
            200,
            json={
                "data": {
                    "access_token": self.access,
                    "refresh_token": self.refresh,
                    "expires_in": self.ttl,
                    "token_type": "Bearer",
                }
            },
        )


def client_for(config, backend, signer):
    return RiseXClient(
        config,
        account=backend.backend.account,
        signer=signer,
        transport=httpx.MockTransport(backend),
    )


@pytest.mark.parametrize("delegated", [False, True])
async def test_login_signature_owner_or_session_key_and_secret_safe_metadata(
    config, backend, account_signer, signer, delegated
):
    server = SessionBackend(backend)
    async with client_for(config, server, signer if delegated else account_signer) as client:
        info = await client.login()
        assert info.account == backend.account and info.expires_in == 900
        assert server.signer == (signer.address if delegated else None)
        assert "fixture-access" not in repr(info)
        assert "fixture-refresh" not in info.model_dump_json()
        fees = await client.get_user_fees()
        assert fees.taker_bps == Decimal("3.123456789123456789")
        assert fees.maker_bps == Decimal("-0.125")
        await client.get_system_config()  # bearer token must not become a global header
        assert await client.logout() is True
        assert client.session is None
        with pytest.raises(AuthenticationError):
            await client.get_user_fees()
    assert {p for p, _ in server.posts} == {"/v1/auth/login", "/v1/auth/logout"}


async def test_concurrent_expired_reads_rotate_once(config, backend, signer, monkeypatch):
    server = SessionBackend(backend)
    async with client_for(config, server, signer) as client:
        await client.login()
        now = time.monotonic()
        monkeypatch.setattr("risex.session.time.monotonic", lambda: now + 1000)
        fees = await asyncio.gather(*(client.get_user_fees() for _ in range(5)))
        assert len(fees) == 5 and server.refresh_count == 1
        await client.refresh_session()
        assert server.refresh_count == 2
        assert server.posts[-1][1]["refresh_token"] == "fixture-refresh-1"


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("fixture-refresh-0"),
        httpx.Response(503, json={"message": "fixture-refresh-0"}),
        httpx.Response(200, json={"data": {"access_token": "fixture-access-1"}}),
    ],
)
async def test_unknown_refresh_is_never_replayed_or_logged(config, backend, signer, error):
    server = SessionBackend(backend)
    server.refresh_error = error
    async with client_for(config, server, signer) as client:
        await client.login()
        with pytest.raises(Exception) as caught:
            await client.refresh_session()
        assert client.session is None
        trace = "".join(traceback.format_exception(caught.value))
        assert "fixture-refresh-0" not in trace and "fixture-access-1" not in trace
        with pytest.raises(AuthenticationError):
            await client.refresh_session()
        assert server.refresh_count == 1


async def test_cancelled_refresh_discards_consumed_token(config, backend, signer):
    server = SessionBackend(backend)
    entered = asyncio.Event()

    async def handler(request):
        if request.url.path == "/v1/auth/refresh":
            entered.set()
            await asyncio.Event().wait()
        return await server(request)

    async with RiseXClient(
        config, account=backend.account, signer=signer, transport=httpx.MockTransport(handler)
    ) as client:
        await client.login()
        task = asyncio.create_task(client.refresh_session())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.session is None
        with pytest.raises(AuthenticationError):
            await client.get_user_fees()


@pytest.mark.parametrize(
    "token_data",
    [
        {
            "access_token": "SECRET",
            "refresh_token": "SECRET",
            "expires_in": 0,
            "token_type": "Bearer",
        },
        {"access_token": "SECRET", "refresh_token": "", "expires_in": 900, "token_type": "Bearer"},
        {
            "access_token": "SECRET",
            "refresh_token": "SECRET",
            "expires_in": True,
            "token_type": "Bearer",
        },
    ],
)
async def test_malformed_login_response_never_leaks_tokens(config, backend, signer, token_data):
    server = SessionBackend(backend)
    server.login_error = httpx.Response(200, json={"data": token_data})
    async with client_for(config, server, signer) as client:
        with pytest.raises(ProtocolError) as caught:
            await client.login()
        assert "SECRET" not in "".join(traceback.format_exception(caught.value))
        assert client.session is None


async def test_fee_401_discards_session_without_implicit_relogin(config, backend, signer):
    server = SessionBackend(backend)
    server.fee_status = 401
    async with client_for(config, server, signer) as client:
        await client.login()
        with pytest.raises(Exception) as caught:
            await client.get_user_fees()
        assert "fixture-access" not in str(caught.value)
        assert client.session is None
        assert server.login_count == 1 and server.refresh_count == 0


async def test_inactive_signer_and_missing_login_fail_without_auth_post(config, backend, signer):
    server = SessionBackend(backend)
    backend.transform = lambda path, data: (
        {"status": 0, "status_description": "Inactive"}
        if path == "/v1/auth/session-key-status"
        else data
    )
    async with client_for(config, server, signer) as client:
        with pytest.raises(AuthenticationError):
            await client.get_user_fees()
        with pytest.raises(AuthenticationError):
            await client.login()
    assert not server.posts


async def test_login_transport_failure_single_attempt(config, backend, signer):
    server = SessionBackend(backend)
    calls = 0

    async def handler(request):
        nonlocal calls
        if request.url.path == "/v1/auth/login":
            calls += 1
            raise httpx.ReadTimeout("sensitive_signature")
        return await server(request)

    async with RiseXClient(
        config, account=backend.account, signer=signer, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(TransportError) as caught:
            await client.login()
        assert "sensitive_signature" not in "".join(traceback.format_exception(caught.value))
        assert client.session is None
    assert calls == 1
