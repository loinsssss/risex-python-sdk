"""Errors applications can handle without depending on transport internals."""

from __future__ import annotations

from dataclasses import dataclass


class RiseXError(Exception):
    """Base SDK error."""


class ClientClosedError(RiseXError):
    """An operation was attempted after the client was closed."""


class ProtocolError(RiseXError):
    """A response cannot be interpreted safely under the documented contract."""


class TransportError(RiseXError):
    """A network operation failed after its retry budget was exhausted."""


class APIError(RiseXError):
    """A REST request was rejected by RISEx."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: int | str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id


class RateLimitError(APIError):
    """RISEx rejected a request due to a rate limit."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: int | str | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, code=code, request_id=request_id)
        self.retry_after = retry_after


class PrecisionError(RiseXError, ValueError):
    """A value cannot be represented exactly in the requested protocol units."""


class SubscriptionError(RiseXError):
    """A subscription was rejected, timed out, or applied a different filter."""


class AuthenticationError(SubscriptionError):
    """Signing credentials or private-stream authentication are unavailable or rejected."""


class NonceExhaustedError(RiseXError):
    """No safe nonce can be reserved until the authoritative anchor advances."""


@dataclass(frozen=True, slots=True)
class MutationContext:
    """Non-secret identifiers for reconciling an uncertain mutation."""

    operation: str
    account: str
    # None for direct signatures (e.g. TP/SL cancellation) that do not use bitmap nonces.
    nonce_anchor: int | None
    nonce_bitmap_index: int | None
    market_id: int | None = None
    client_order_id: int | None = None
    order_id: str | None = None


class UnknownOutcomeError(RiseXError):
    """A mutation may have executed; query state instead of blindly resubmitting."""

    def __init__(self, context: MutationContext, *, request_id: str | None = None) -> None:
        super().__init__(f"{context.operation} outcome is unknown; reconcile authoritative state")
        self.context = context
        self.request_id = request_id


class OrderWaitTimeoutError(RiseXError):
    """An order did not reach a terminal state within the requested wait."""

    def __init__(self, order_id: str) -> None:
        super().__init__(f"Timed out waiting for order {order_id}; no cancellation was submitted")
        self.order_id = order_id


class ReconnectExhaustedError(TransportError):
    """A stream could not recover within its consecutive reconnect budget."""


class StaleOrderbookError(RiseXError):
    """A local book is unavailable until a fresh WebSocket snapshot is applied."""


class ChecksumMismatchError(StaleOrderbookError):
    """The local orderbook no longer matches the server's CRC32 checksum."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"Orderbook checksum mismatch: expected {expected}, computed {actual}")
        self.expected = expected
        self.actual = actual
