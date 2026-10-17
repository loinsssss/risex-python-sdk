# Public API reference

Import supported objects from `risex`. This reference describes the current
implementation; [the developer guide](developer-guide.md) provides runnable
workflows and [protocol contracts](api-contracts.md) describe wire encodings.

This is the SDK's own API reference. Its API is an in-process Python interface,
not an HTTP service, so there are no SDK-owned Swagger/OpenAPI routes.
The [upstream testnet Swagger UI](https://api.testnet.rise.trade/swagger/)
describes RISEx's HTTP endpoints, not these Python methods.

## Client and credentials

```text
RiseXClient(
    config: RiseXConfig | None = None,
    *,
    mainnet: bool | None = None,
    account: str | None = None,
    signer: Signer | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
)
```

Omitted environment arguments select mainnet. `mainnet=False` selects testnet.
The `None` sentinel preserves a supplied config's environment; conflicting
explicit values are rejected. `account` is a 20-byte 0x-prefixed address.
`transport` supports HTTP transport injection, including offline tests.

`client.config`, `client.account` and `client.signer` expose selected settings
and identities. Use the async context manager or `await client.aclose()`.
There is no synchronous REST client.

| Operation | Credentials needed |
| --- | --- |
| Public reads, metadata discovery, order lookup by ID, public streams | None |
| Account reads and nonce state | Account address |
| Signer status | Account and signer address, or configured Signer |
| Orders, cancellations, private streams | Account and registered delegated Signer |
| Signer registration/revocation | Configured account/delegated Signer plus account Signer argument |

An `account=` keyword on reads overrides the client's default account for that
call. Signed operations and private streams use the configured account.

## REST methods

All methods in the following tables require `await`. Arguments shown with `=`
are keyword-only; `...` denotes a required keyword argument.
Page numbers start at 1; open-order offsets start at 0.

### Public reads and metadata

| Method / arguments | Returns |
| --- | --- |
| `get_markets(market_ids=(), force_refresh=False)` | `MarketsResponse` with `.markets` |
| `get_orderbook(market_id, limit=50)` | `OrderbookSnapshot` with `.bids` and `.asks`; limit 1–250 |
| `get_signing_domain()` | `SigningDomain` |
| `get_system_config()` | `SystemConfig` |
| `initialize(force_refresh=False)` | Validated, cached `ProtocolMetadata` |
| `get_nonce_state(account=None)` | `NonceState` with anchor, index and bitmap |
| `get_signer_status(account=None, signer=None)` | `SessionKeyStatus` with `.active` |

`market_ids` is a sequence of unique positive integers. Omitted/empty IDs
request all markets. `initialize()` performs reads only; signing code invokes
it when necessary. `force_refresh=True` refreshes its metadata cache.

### Market history

| Method / arguments | Returns |
| --- | --- |
| `get_funding_rate_history(market_id, page=1, limit=100, start_time=None, end_time=None)` | `FundingRateHistory`: market_id, records (`FundingRateRecord`), page, has_next_page |
| `get_candles(market_id, interval=..., start_time=..., end_time=...)` | Tuple of `Candle`, oldest first |

```text
iter_funding_rate_history(
    market_id, *, page_size=1000, max_pages=1000, start_time=None, end_time=None
)
    -> AsyncIterator[FundingRateRecord]
```

Times are integer nanoseconds. Funding settlements come newest first and the
range filters on each settlement's `end_time`; with no range RISEx returns the
last 7 days. `has_next_page` only reports a full page, so a result that is an
exact multiple of the page size ends with one empty page. `funding_rate` is the
rate for one funding period (the market's `funding_interval`, hourly today).

`interval` must be one of `1m`, `5m`, `15m`, `1h`, `4h`, `1d`, `1w`. RISEx
answers any other duration with 1m candles, so the SDK rejects it, and raises
`ProtocolError` if a response carries a different interval. Candles are
returned as reported: `open` is the previous candle's `close`, so it can lie
outside `[low, high]`, and buckets without trades are omitted.

### Accounts and pagination

| Method / arguments | Returns |
| --- | --- |
| `get_balance(account=None, token=None)` | `Balance`; omitted token uses discovered USDC address |
| `get_cross_margin_balance(account=None)` | `Balance` |
| `get_balances(account=None)` | `Balances`: account, token, collateral, cross_margin |
| `get_position(market_id, account=None)` | `PositionResponse` containing direct `.position` |
| `get_positions(account=None, market_id=None, page=1, page_size=100)` | `PositionsResponse`: positions, page, total_count, has_next_page |
| `get_open_orders(account=None, market_id=None, start_index=0, limit=100, order_ids=())` | `OpenOrdersResponse`: orders, account, market_id, total_orders |
| `get_order(order_id, market_id=None)` | One `Order` |
| `get_order_history(...)` | `OrderHistory`: orders, page, has_next_page |
| `get_trade_history(...)` | `TradeHistory`: trades (`Fill` models), page, has_next_page |
| `get_account_snapshot(account=None, max_pages=1000)` | `AccountSnapshot`: balances, positions, open_orders |

History arguments:

```text
get_order_history(
    *, account=None, market_id=None, page=1, limit=100,
    statuses=(), order_ids=(), start_time=None, end_time=None, descending=True
)
get_trade_history(
    *, account=None, market_id=None, page=1, limit=100,
    start_time=None, end_time=None, descending=True
)
```

History time bounds are nonnegative integer nanoseconds. `end_time` must not
precede `start_time`. Status filters use the provider strings below.
Up to 100 order IDs may be passed to order-history/open-order filters.
Page sizes/limits for account collections must be integers from 1 to 1000.

Use these async generators with `async for`, without first awaiting them:

```text
iter_positions(*, account=None, market_id=None, page_size=100, max_pages=1000)
    -> AsyncIterator[Position]
iter_open_orders(*, account=None, market_id=None, page_size=100, max_pages=1000)
    -> AsyncIterator[OpenOrder]
iter_order_history(*, account=None, market_id=None, page_size=100, max_pages=1000)
    -> AsyncIterator[Order]
iter_trade_history(
    *, account=None, market_id=None, page_size=100, max_pages=1000,
    start_time=None, end_time=None
)
    -> AsyncIterator[Fill]
```

Iterators start from the first page/offset and bound traversal with `max_pages`.
Order-history iteration does not expose the single-page method's time/status/ID
filters. Use explicit `get_order_history()` pages when those filters are needed.
Snapshots and paginated reads are not atomic exchange views.

### Signer administration and orders

| Method / arguments | Returns |
| --- | --- |
| `register_signer(account_signer, expiration=..., message="RISEx session key", label="")` | `SignerRegistration` |
| `revoke_signer(account_signer)` | `SignerRegistration` |
| `place_order(request)` | `OrderSubmission` |
| `cancel_order(order_id, market_id=None, resting_order_id=None)` | `Cancellation` |
| `cancel_all_orders(market_id)` | `Cancellation` |
| `wait_for_order(order_id, timeout=30.0, poll_interval=0.5)` | Terminal `Order` |
| `reconcile_submission(error, max_pages=100)` | `SubmissionResolution` |

`expiration` is required and is a future Unix-second uint32 timestamp.
`account_signer.address` must identify the configured account.
`SignerRegistration.success` and `Cancellation.success` must be inspected;
a returned model is not itself an assertion of success.

Composite order IDs are 0x-prefixed 24-byte hex strings. Cancellation infers the
market by lookup when omitted. It decodes the resting ID from the composite ID;
an explicitly supplied resting ID must match. A globally indexed `get_order()`
does not require a configured account.

`wait_for_order()` reads until FILLED/CANCELLED. Timeout raises
`OrderWaitTimeoutError` and does not cancel. `reconcile_submission()` accepts
an `UnknownOutcomeError`; `.orders` contains located orders,
`.nonce_consumed` is bool or `None` when the old bitmap is unavailable, and
`.resolved` reports matching placement or observed cancellation state.
This helper does not prove an absent submission failed or resolve every type
of administrative mutation.

## OrderRequest and results

| Field | Default / constraint |
| --- | --- |
| `market_id` | Required positive uint16 integer |
| `side` | Required `OrderSide.BUY` (0) or `SELL` (1) |
| `quantity` | Required positive finite `Decimal`, in human units |
| `price` | Required positive finite `Decimal`; limit price or market execution bound |
| `order_type` | `OrderType.LIMIT` (1); `MARKET` is 0 |
| `time_in_force` | `None` chooses GTC for limits, IOC for market orders |
| `post_only` | `False`; supported for resting limit GTC/GTT orders |
| `reduce_only` | `False` |
| `stp_mode` | `STPMode.EXPIRE_MAKER` (0); EXPIRE_TAKER 1, EXPIRE_BOTH 2 |
| `client_order_id` | `None` generates a nonzero uint64 recovery ID; supplied values are uint64 |
| `builder_id` | 0; uint16 |
| `builder_fee_bps` | 0; uint16, nonzero requires nonzero builder_id |
| `ttl_units` | 0; uint16 protocol units, nonzero exactly for GTT |

`TimeInForce`: GTC 0, GTT 1, FOK 2, IOC 3. Market orders require FOK/IOC and cannot
be post-only. GTT uses native protocol TTL units; do not pass a Unix timestamp
or assume the field is seconds. Order precision and size/price encoding widths
are checked against current metadata before a nonce is reserved.

| Model | Fields commonly used by consumers |
| --- | --- |
| `OrderSubmission` | order_id, tx_hash, block_number, sc_order_id, filled_quantity_wei, filled_quantity, filled_percent |
| `Order` | id/order_id, market_id, sender, status, size, filled_size, price, avg_price, terminal |
| `OpenOrder` | order_id, market_id, account, size_steps, price_ticks, execution flags |
| `Fill` | id, order_id, market_id, side, price, size, fee, time, liquidity_indicator |
| `Cancellation` | success, tx_hash, block_number |
| `SignerRegistration` | success, transaction_hash, optional status/block_number |

An acknowledgement is not order status. Optional receipt fill quantities remain
`None` when unavailable; `.filled_quantity` converts the raw WAD value to Decimal.
Order statuses are `ORDER_STATUS_NONE`, `ORDER_STATUS_OPEN`,
`ORDER_STATUS_FILLED`, `ORDER_STATUS_CANCELLED`. Partial fills have a nonzero
`filled_size` and retain their provider status.

## Streams and local orderbook

These factories are synchronous and return `RiseXStream`:

| Factory (`market_ids=()` on each) | Data event | Credentials |
| --- | --- | --- |
| `stream_orderbook()` | `OrderbookEvent` | None |
| `stream_trades()` | `TradeEvent` | None |
| `stream_oracle()` | `OracleEvent` | None |
| `stream_orders()` | `OrdersEvent` | Account + signer |
| `stream_positions()` | `PositionsEvent` | Account + signer |
| `stream_fills()` | `FillsEvent` | Account + signer |

The general factory is `stream(channel, *, market_ids=())`.
All streams yield `ConnectionEvent` as well as data. Its fields are
`state` (connected/disconnected/reconnecting), `stale` and `attempt`.
Use `async with stream`, `async for event in stream`, or `await stream.aclose()`.
There is one reader/socket per stream, fresh auth on private reconnect and no
guaranteed replay. `PublicStream` is a compatibility alias of `RiseXStream`.

`LocalOrderbook(market_id)` exposes `.valid`, `.snapshot`, `.apply(event)` and
`.reset()`. Apply one market's `OrderbookEvent` and every `ConnectionEvent`.
Reading an invalid snapshot raises `StaleOrderbookError`. Deltas replace/delete
levels; checksum failure invalidates state and raises `ChecksumMismatchError`.
`compute_checksum(bids, asks)` accepts tuples of `PriceLevel` and returns CRC32.

## Units and signer injection

| Function | Result |
| --- | --- |
| `to_steps(value: Decimal, step: Decimal)` | Exact nonnegative integer count; no rounding |
| `from_steps(count: int, step: Decimal)` | Exact Decimal amount |
| `to_wei(value: Decimal)` | Exact nonnegative integer at scale 10^18 |
| `from_wei(value: str \| int)` | Exact signed Decimal at scale 10^18 |

Markets/books/trades and account amounts use human-unit Decimal values.
Oracle prices and raw acknowledgement fill amounts use integer 10^18 scaling.
History/fill/worker times use nanoseconds; oracle payload timestamps, permit
deadlines and signer expiration use seconds.

`LocalSigner(private_key: str | bytes)` implements this injectable interface:

```python
from collections.abc import Mapping
from typing import Any, Protocol


class ExternalSigner(Protocol):
    @property
    def address(self) -> str: ...

    async def sign_typed_data(self, data: Mapping[str, Any]) -> bytes: ...
```

Return canonical 65-byte `r + s + v` with low-s and v=27/28.
The SDK verifies recovered identity and performs wire-specific serialization.
`NonceManager`/`Nonce` are exported for allocation utilities; normal client users
do not allocate a second nonce manager alongside their client.

## Configuration

Construct `RiseXConfig` with explicit keywords. It is an immutable dataclass.

| Field | Default |
| --- | --- |
| mainnet | True |
| base_url / websocket_url | Selected from mainnet/testnet unless explicitly overridden |
| request_timeout | 10 seconds |
| max_read_retries | 2 additional attempts |
| retry_delay / max_retry_delay | 0.25 / 5 seconds |
| subscription_timeout / authentication_timeout | 10 / 20 seconds |
| max_reconnect_attempts | 3 consecutive reconnects |
| reconnect_delay / max_reconnect_delay | 0.5 / 5 seconds |
| websocket_max_queue | 32 incoming frames |
| websocket_max_size | 8 MiB per message |
| max_pending_messages | 128 data messages before subscription acknowledgement |
| permit_ttl_seconds | 120 seconds |
| rest_requests_per_second | 40; None disables local pacing |
| websocket_requests_per_second | 8 JSON requests; None disables local pacing |

Retry/reconnect delays are bounded exponential delays. A valid data event resets
the consecutive reconnect count. Buffers apply backpressure; data is not silently
discarded. Pacing is per client, not distributed coordination across an IP.

## Errors

| Error | Application handling |
| --- | --- |
| `RiseXError` | Base class for SDK errors |
| `ClientClosedError` | Construct a new client after shutdown |
| `PrecisionError` | Correct exact units/market constraints |
| `APIError` | Inspect status_code, code and request_id |
| `RateLimitError` | APIError with retry_after |
| `TransportError` | Network/upgrade failure; reads may have exhausted their retry budget |
| `UnknownOutcomeError` | Persist context/request_id, query state before another mutation |
| `OrderWaitTimeoutError` | Inspect order_id and current state; no cancellation was submitted |
| `AuthenticationError` | Correct credentials/registration/expiry |
| `SubscriptionError` | Resolve acknowledgement/filter/rejection failure |
| `ProtocolError` | Investigate an invalid or inconsistent provider response |
| `NonceExhaustedError` | Await authoritative anchor advancement; review account coordination |
| `ReconnectExhaustedError` | Restart after connectivity recovery and refresh state |
| `StaleOrderbookError` | Wait for a fresh full snapshot |
| `ChecksumMismatchError` | StaleOrderbookError with expected/actual CRC32; restart subscription |

Invalid caller input can also raise `ValueError`, `TypeError` or Pydantic
`ValidationError`. Reads have bounded retry rules; writes do not retry.
Cancellation during transmission can raise `UnknownOutcomeError` because the
mutation may have executed. See [recovery guidance](developer-guide.md#handle-uncertainty).
