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
| `decode_transaction(tx_hash)` | `DecodedTransaction`: success flag and optional decoded contract error |
| `initialize(force_refresh=False)` | Validated, cached `ProtocolMetadata` |
| `get_nonce_state(account=None)` | `NonceState` with anchor, index and bitmap |
| `get_signer_status(account=None, signer=None)` | `SessionKeyStatus` with `.active` |

`market_ids` is a sequence of unique positive integers. Omitted/empty IDs
request all markets. `initialize()` performs reads only; signing code invokes
it when necessary. `force_refresh=True` refreshes its metadata cache.

`decode_transaction()` uses the public [transaction decoder](https://developer.rise.trade/reference/decodetx)
(`GET /v1/tx/{tx_hash}`); it requires no account or signer and never submits a
transaction. Hashes must contain `0x` followed by exactly 64 hexadecimal digits.
If the provider includes a response hash, it must match the requested hash.
`DecodedTransaction.success` is a required boolean. Its optional `.error` is a
`DecodedError` retaining `.selector`, `.signature`, `.name`, `.parameters` (a tuple
of strings) and `.message`. An unsuccessful transaction without decoded details
remains `success=False, error=None`; the SDK does not invent a cause or replay it.
Save the original transaction hash when handling an uncertain submission so it
can be investigated later. Decoding does not replace fresh order/position reads.

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
| `get_account_snapshot(account=None, max_pages=1000, include_conditional_orders=False)` | `AccountSnapshot`: balances, positions, open_orders, optional active conditional_orders |
| `login()` | Token-free `LoginSession` metadata; uses configured owner/registered signer |
| `refresh_session()` | Updated `LoginSession`; refreshes once and rotates in-memory tokens |
| `logout()` | `True` after confirmed revocation, `False` if no local session existed |
| `session` (property) | `LoginSession` or `None`; no raw tokens |
| `get_user_fees()` | `UserFees`: actual account tier, taker/maker bps, volume, next-tier progress and schedule |
| `get_tpsl_orders(...)` | `TpslOrdersResponse`: orders, total, page, limit, has_next_page |
| `iter_tpsl_orders(...)` | Async iterator over `TpslOrder` |

JWT login is explicit. `get_user_fees()` refreshes before expiry, but does not sign a
new login automatically. Failed/uncertain refresh and HTTP 401 require explicit login.
Tokens are private and in memory only. Token rotation is serialized; auth POSTs are
not retried. The fee endpoint has no account override: it always uses the logged-in
account. Only fee requests carry the bearer token; public reads and permit operations
do not inherit it. Fee JSON numbers are parsed directly into Decimal, without a float
round trip. Negative maker rebates are preserved.

```text
get_tpsl_orders(
    *, account=None, market_id=None, page=1, limit=100, statuses=(),
    stop_type="STOP_TYPE_NONE", start_time=None, end_time=None
)
iter_tpsl_orders(
    *, account=None, market_id=None, page_size=100, max_pages=1000, statuses=(),
    stop_type="STOP_TYPE_NONE", start_time=None, end_time=None
)
```

The SDK explicitly sends `STOP_TYPE_NONE` to avoid the API's TAKE_PROFIT default.
Other stop filters are `TAKE_PROFIT`/`STOP_LOSS`; statuses are the native
`TPSL_ORDER_STATUS_ACCEPTED`, `TRIGGERED`, `SUCCESS`, `CANCELLED` values (each with
the `TPSL_ORDER_STATUS_` prefix). `TpslOrder.active` includes accepted and triggered.
Time filters are nanoseconds. Account/market/status/stop scopes, page sizes and totals
are checked. Duplicate IDs, changing totals, incomplete pages and page-bound exhaustion
raise `ProtocolError`; discard partial iterator results and fetch a new snapshot.
REST pagination cannot promise an atomic view during concurrent order changes.

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
| `price` | Required finite `Decimal`: positive for LIMIT, exactly zero for MARKET |
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

Compatibility correction: native MARKET orders use `price=Decimal("0")`
(`price_ticks=0`). Previous SDK versions incorrectly required a positive market
price. Use a positive-price LIMIT order with IOC/FOK when an execution price
bound is needed. Neither form bypasses the exchange's own matching price bands.

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

## Portfolio risk and account controls (local 0.1.0a3.dev0)

- `get_portfolio_details(account=None)` returns exact USD summary values and
  signed decimal coin quantities, validates account scope and unique market IDs.
  Cross margin/maintenance totals exclude isolated scopes. Blank settlement
  fields in portfolio rows remain `None`; they are not silently converted to zero.
- `update_leverage(market_id, leverage)` validates a positive uint8 value and the
  current market maximum, then submits one VerifyWitness permit.
- `update_margin_mode(market_id, isolated=bool)` submits one VerifyWitness permit.
  A redundant setting can revert `MarginModeUnchanged`; read the current mode first.
- `cancel_all_tpsl_orders(market_id)` signs `CancelAllTpslOrders` directly and returns
  a confirmed success/count. It cancels accepted TP/SLs; triggered orders still
  need reconciliation. This signature does not consume a bitmap nonce, so its
  `MutationContext` nonce fields are `None`.

Account setting replies require successful chain receipt status and consistent
block numbers. Timeout, malformed reply or ambiguous server failure is not success
and is never automatically replayed. The caller must independently reconcile.

Live testnet unit finding (2026-10-07): indexed `Position.size` returned WAD integers
(e.g. `290000000000000` for 0.00029 BTC), while `AccountPosition.size` and
`PortfolioPosition.size` returned decimal coin quantities. Wire models retain
provider values. Do not interchange the routes or infer units from magnitude.
The portfolio side flag was observed as zero even for a negative short size;
use signed size and independently compare the direct position read. Consumers
must verify units/scope before using indexed quantities for orders or displays.
