"""In-memory JWT sessions; rotation is serialized and never replayed after uncertainty."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from . import auth
from .exceptions import AuthenticationError, ClientClosedError, ProtocolError
from .models import LoginSession, ProtocolMetadata
from .rest import RestClient
from .signing import Signer


class _Tokens(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    access_token: SecretStr
    refresh_token: SecretStr
    expires_in: Annotated[int, Field(gt=0, strict=True)]
    token_type: Literal["Bearer"]

    @field_validator("access_token", "refresh_token")
    @classmethod
    def nonempty_token(cls, value: SecretStr) -> SecretStr:
        text = value.get_secret_value()
        if not text or any(c.isspace() for c in text):
            raise ValueError("Invalid authentication token")
        return value


class JwtSession:
    def __init__(self, rest: RestClient) -> None:
        self._rest = rest
        self._lock = asyncio.Lock()
        self._tokens: _Tokens | None = None
        self._info: LoginSession | None = None
        self._refresh_at = 0.0
        self._closed = False

    @property
    def info(self) -> LoginSession | None:
        return self._info

    def close(self) -> None:
        self._closed = True
        self.clear()

    def clear(self) -> None:
        self._tokens = None
        self._info = None
        self._refresh_at = 0.0

    def invalidate(self, token: SecretStr) -> None:
        # A late 401 from an older request must not discard a newer rotated session.
        if self._tokens is not None and self._tokens.access_token == token:
            self.clear()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ClientClosedError("RISEx client is closed")

    def _store(self, data: dict[str, Any], account: str, started: float) -> LoginSession:
        self._ensure_open()
        try:
            tokens = _Tokens.model_validate(data)
        except ValidationError:
            # Pydantic validation contexts can contain raw tokens: suppress them.
            raise ProtocolError("RISEx returned invalid session tokens") from None
        self._tokens = tokens
        self._info = LoginSession(
            account=account, expires_in=tokens.expires_in, token_type=tokens.token_type
        )
        # Count from request start conservatively; refresh before expiry, with a
        # proportional margin for unusually short server TTLs.
        self._refresh_at = started + tokens.expires_in - min(30, tokens.expires_in / 10)
        return self._info

    async def login(
        self,
        account: str,
        signer: Signer,
        metadata: Callable[[], Awaitable[ProtocolMetadata]],
    ) -> LoginSession:
        async with self._lock:
            self._ensure_open()
            self.clear()
            domain = await metadata()
            nonce = (await self._rest.get("/v1/auth/nonce")).get("nonce")
            if not isinstance(nonce, str):
                raise AuthenticationError("Server omitted the login nonce")
            payload = await auth.login_payload(
                domain,
                account=account,
                signer=signer,
                nonce=nonce,
                deadline=int(time.time()) + 60,
            )
            started = time.monotonic()
            data = await self._rest.auth_post("/v1/auth/login", payload=payload)
            return self._store(data, account, started)

    async def _refresh_locked(self) -> LoginSession:
        self._ensure_open()
        if self._tokens is None or self._info is None:
            raise AuthenticationError("Call login() before using a JWT account endpoint")
        token, account = self._tokens.refresh_token, self._info.account
        # Retire BEFORE send: cancellation, timeout or invalid responses must not
        # permit replay of a refresh token that the server may have consumed.
        self.clear()
        started = time.monotonic()
        data = await self._rest.auth_post(
            "/v1/auth/refresh", payload={"refresh_token": token.get_secret_value()}
        )
        return self._store(data, account, started)

    async def refresh(self) -> LoginSession:
        async with self._lock:
            return await self._refresh_locked()

    async def access_token(self) -> SecretStr:
        async with self._lock:
            self._ensure_open()
            if self._tokens is None:
                raise AuthenticationError("Call login() before using a JWT account endpoint")
            if time.monotonic() >= self._refresh_at:
                await self._refresh_locked()
            assert self._tokens is not None
            return self._tokens.access_token

    async def logout(self) -> bool:
        async with self._lock:
            self._ensure_open()
            tokens = self._tokens
            self.clear()
            if tokens is None:
                return False
            data = await self._rest.auth_post(
                "/v1/auth/logout",
                payload={"refresh_token": tokens.refresh_token.get_secret_value()},
                bearer_token=tokens.access_token,
            )
            if data.get("success") is not True:
                raise ProtocolError("RISEx did not confirm logout")
            return True
