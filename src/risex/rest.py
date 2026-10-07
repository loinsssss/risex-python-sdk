"""Paced REST reads with bounded retries, and single-attempt signed mutations."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from pydantic import SecretStr

from .config import RiseXConfig
from .exceptions import (
    APIError,
    ClientClosedError,
    MutationContext,
    ProtocolError,
    RateLimitError,
    TransportError,
    UnknownOutcomeError,
)
from .rate_limit import RateLimiter


@dataclass(frozen=True, slots=True)
class MutationResponse:
    data: dict[str, Any]
    request_id: str | None


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
        if seconds >= 0 and seconds != float("inf"):
            return seconds
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _api_error(response: httpx.Response) -> APIError:
    request_id = response.headers.get("x-request-id")
    code: int | str | None = None
    message = f"RISEx returned HTTP {response.status_code}"
    try:
        body = response.json()
        if isinstance(body, dict):
            request_id = body.get("request_id", request_id)
            error = body.get("error", body)
            if isinstance(error, dict):
                message = str(error.get("message", message))
                code = error.get("code")
            elif isinstance(error, str):
                message = error
    except ValueError:
        pass
    kwargs = {"status_code": response.status_code, "code": code, "request_id": request_id}
    if response.status_code == 429:
        return RateLimitError(message, retry_after=_retry_after(response), **kwargs)
    return APIError(message, **kwargs)


class RestClient:
    def __init__(
        self, config: RiseXConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.config = config
        self._limiter = RateLimiter(config.rest_requests_per_second)
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/") + "/",
            timeout=config.request_timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "risex-python-sdk/0.1.0a3.dev0"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
        bearer_token: SecretStr | None = None,
        exact_json_numbers: bool = False,
        error_is_data: bool = False,
    ) -> dict[str, Any]:
        if self._client.is_closed:
            raise ClientClosedError("RISEx client is closed")
        for attempt in range(self.config.max_read_retries + 1):
            delay = min(
                self.config.retry_delay * 2**attempt,
                self.config.max_retry_delay,
            )
            try:
                await self._limiter.acquire()
                response = await self._client.get(
                    path.lstrip("/"),
                    params=httpx.QueryParams(tuple(params or ())),
                    headers=None
                    if bearer_token is None
                    else {"Authorization": f"Bearer {bearer_token.get_secret_value()}"},
                )
            except httpx.TransportError as exc:
                if attempt == self.config.max_read_retries:
                    if bearer_token is not None:
                        raise TransportError("RISEx authenticated GET failed") from None
                    raise TransportError("RISEx GET failed after bounded retries") from exc
                await asyncio.sleep(delay)
                continue
            if response.status_code in {429, 500, 502, 503, 504}:
                retry_after = _retry_after(response)
                if attempt < self.config.max_read_retries and (
                    retry_after is None or retry_after <= self.config.max_retry_delay
                ):
                    await asyncio.sleep(max(delay, retry_after or 0))
                    continue
            if not response.is_success:
                if bearer_token is not None:
                    raise APIError(
                        f"RISEx authenticated GET returned HTTP {response.status_code}",
                        status_code=response.status_code,
                    )
                raise _api_error(response)
            try:
                body = json.loads(
                    response.content, parse_float=Decimal if exact_json_numbers else float
                )
            except (ValueError, UnicodeDecodeError) as exc:
                if bearer_token is not None:
                    raise ProtocolError("RISEx authenticated GET returned invalid JSON") from None
                raise ProtocolError("RISEx GET returned invalid JSON") from exc
            if not isinstance(body, dict):
                raise ProtocolError("RISEx GET response must be an object")
            # Transaction decoding uses `error` as successful-response data, not
            # an API failure envelope. HTTP failure handling above stays unchanged.
            if body.get("error") is not None and not error_is_data:
                if bearer_token is not None:
                    raise APIError(
                        "RISEx authenticated GET failed", status_code=response.status_code
                    )
                raise _api_error(response)
            # The deployed API wraps schemas in data; allow the schema's bare shape too.
            data = body.get("data", body)
            if not isinstance(data, dict):
                raise ProtocolError("RISEx GET data must be an object")
            return data
        raise AssertionError("unreachable")

    async def auth_post(
        self, path: str, *, payload: dict[str, Any], bearer_token: SecretStr | None = None
    ) -> dict[str, Any]:
        """Single attempt for one-use login/refresh tokens. Never echo response bodies."""
        if path not in {"/v1/auth/login", "/v1/auth/refresh", "/v1/auth/logout"}:
            raise ValueError("Not a session authentication endpoint")
        if self._client.is_closed:
            raise ClientClosedError("RISEx client is closed")
        await self._limiter.acquire()
        try:
            response = await self._client.post(
                path.lstrip("/"),
                json=payload,
                headers=None
                if bearer_token is None
                else {"Authorization": f"Bearer {bearer_token.get_secret_value()}"},
            )
        except httpx.TransportError:
            raise TransportError("RISEx authentication outcome is unknown; log in again") from None
        if not response.is_success:
            raise APIError(
                f"RISEx authentication returned HTTP {response.status_code}",
                status_code=response.status_code,
            )
        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError):
            raise ProtocolError("RISEx authentication returned invalid JSON") from None
        if not isinstance(body, dict) or body.get("error") is not None:
            raise ProtocolError("RISEx authentication response is invalid")
        data = body.get("data", body)
        if not isinstance(data, dict):
            raise ProtocolError("RISEx authentication data must be an object")
        return data

    async def post(
        self,
        path: str,
        *,
        payload: dict[str, Any],
        context: MutationContext,
    ) -> MutationResponse:
        """Submit once. Timeouts, cancellation and server failures can be ambiguous."""
        if self._client.is_closed:
            raise ClientClosedError("RISEx client is closed")
        # Cancellation while waiting for a send slot cannot have submitted the mutation.
        await self._limiter.acquire()
        try:
            response = await self._client.post(path.lstrip("/"), json=payload)
        except (httpx.TransportError, asyncio.CancelledError) as exc:
            raise UnknownOutcomeError(context) from exc
        if response.status_code == 408 or response.status_code >= 500:
            error = _api_error(response)
            raise UnknownOutcomeError(context, request_id=error.request_id) from error
        if not response.is_success:
            raise _api_error(response)
        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise UnknownOutcomeError(
                context, request_id=response.headers.get("x-request-id")
            ) from exc
        if not isinstance(body, dict):
            raise UnknownOutcomeError(context)
        if body.get("error") is not None:
            error = _api_error(response)
            if str(error.code) in ("Internal", "Unknown", "DeadlineExceeded", "13", "2", "4"):
                raise UnknownOutcomeError(context, request_id=error.request_id) from error
            raise error
        data = body.get("data", body)
        if not isinstance(data, dict):
            raise UnknownOutcomeError(context, request_id=body.get("request_id"))
        request_id = body.get("request_id", response.headers.get("x-request-id"))
        return MutationResponse(
            data=data, request_id=request_id if isinstance(request_id, str) else None
        )
