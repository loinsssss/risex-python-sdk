# Developer guide

Use this guide to run the library or integrate it into another Python application.
For exact arguments, see the [public API reference](api-reference.md).
To change the SDK itself, see [Contributing](../CONTRIBUTING.md).

## 1. Install and make your first call

You need Python 3.11 or newer. From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -c "import risex; print(risex.__version__)"
python examples/public_rest.py --testnet
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell.
The public example needs internet access, but no account, key or funds. It prints
the market name/reference price and the best bids and asks.

For an application in another repository, use its virtual environment:

```bash
python -m pip install -e /absolute/path/to/risex-python-sdk
```

For an artifact install, run `python -m build` in this repository and install the
specific wheel from `dist/` in the consumer environment. The distribution name
is `risex-python-sdk`; imports use `risex`. No registry publication is assumed.

Save this as `quickstart.py` and run `python quickstart.py`:

```python
import asyncio

from risex import RiseXClient


async def main():
    async with RiseXClient(mainnet=False) as client:
        response = await client.get_markets(market_ids=[1])
        market = response.markets[0]
        print("Market:", market.market_id, market.config.name)
        print("Mark price:", market.mark_price)
        print("Quantity step:", market.config.step_size)
        print("Price tick:", market.config.step_price)
        book = await client.get_orderbook(market.market_id, limit=5)
        print("Best bid:", book.bids[0] if book.bids else None)
        print("Best ask:", book.asks[0] if book.asks else None)


if __name__ == "__main__":
    asyncio.run(main())
```

REST methods are asynchronous: use `await` inside an async function.
In an existing application event loop, await your function directly;
use `asyncio.run()` only at a script's entry point.

## 2. Select an environment and manage lifecycle

The library defaults to **mainnet**. Introductory examples explicitly choose
testnet with `mainnet=False`.

| Construction | Environment |
| --- | --- |
| `RiseXClient()` | Mainnet |
| `RiseXClient(mainnet=True)` | Mainnet |
| `RiseXClient(mainnet=False)` | Testnet |
| `RiseXClient(RiseXConfig(mainnet=False, request_timeout=15))` | Testnet with custom settings |

The boolean selects both REST and WebSocket endpoints. There is no automatic
fallback to another environment. `RiseXConfig` also controls retry/pacing limits,
stream buffers and explicit custom URLs. Construct it for the intended
environment; conflicting config and client booleans raise `ValueError`.

Reuse an `async with RiseXClient(...) as client` for application operations.
The context closes HTTP and all owned streams. With manual lifecycle management,
call `await client.aclose()` during shutdown. A closed client cannot be reopened.
Imports do not connect, read credentials or load configuration.

Use one signing client per account. Its nonce coordination covers concurrent
calls on that client. Other clients, processes or tools sharing the account
need coordination in the application.

## 3. Discover markets and use exact values

`get_markets()` returns native market metadata and prices. Choose market IDs from
that response; ID 1 is used in examples. Symbol mapping belongs to your application;
do not assume a specific display-name format.

Use `Decimal("0.001")`, with a string argument, for quantities and prices.
`Decimal(0.001)` already contains binary floating-point approximation.
`OrderRequest` requires `Decimal` inputs; the SDK never rounds order values.

Metadata supplies `step_size`, `step_price` and `min_order_size`.
Use `market.config.quantity_to_steps(quantity)` and
`market.config.price_to_ticks(price)` to check representability.
Choose any rounding policy in the application, then pass the exact result.
`PrecisionError` means the value does not satisfy the market's constraints.

Provider models are immutable and preserve additive fields in `model_extra`.
Use `model_dump(mode="json")` for JSON-compatible serialization; decimal values
are retained as strings.

## 4. Read an account and traverse pages

Balance/position/order reads require an address, but no private key. Set `RISEX_ACCOUNT` to your
account's 0x-prefixed Ethereum address, then run:

```bash
python examples/account_reads.py --testnet
```

This complete application script reads balances, a direct position, all indexed
positions and open orders:

```python
import asyncio
import os

from risex import RiseXClient


async def main():
    async with RiseXClient(mainnet=False, account=os.environ["RISEX_ACCOUNT"]) as client:
        balances = await client.get_balances()
        print("Collateral:", balances.collateral.balance)
        print("Cross margin:", balances.cross_margin.balance)
        position = (await client.get_position(1)).position
        print("Signed position size:", position.size)
        async for row in client.iter_positions():
            print(row.market_id, row.size)
        async for order in client.iter_open_orders():
            print(order.order_id, order.size_steps, order.price_ticks)


if __name__ == "__main__":
    asyncio.run(main())
```

Direct flat positions can have market ID 0 and optional prices set to `None`.
Determine exposure from `size`, not a retained side field.
Open orders expose protocol step/tick counts; order history exposes human-unit
decimals. These are different models.

Collection `get_*` methods return one page. Use their `iter_*` counterparts with
`async for` to traverse pages, and `max_pages` to bound work.
Exceeding that bound raises `ProtocolError` instead of reporting the collection
as complete. Exchange state can change while pages are read.
`get_account_snapshot()` gathers separate balance/position/order reads and is
not an atomic exchange snapshot.

## 5. Configure a delegated signer

| Process variable used by examples | Purpose |
| --- | --- |
| `RISEX_ACCOUNT` | Account identity for reads, permits and private streams |
| `RISEX_SIGNER_PRIVATE_KEY` | Registered delegated key for trading and private streams |
| `RISEX_ACCOUNT_PRIVATE_KEY` | Account key for the explicit registration example |

The SDK accepts credentials through arguments. Examples read these process
environment variables only when executed. `.env.example` lists their names;
neither the library nor the examples load `.env` automatically.
Supply values through your shell, IDE environment or application's secret setup.
Keep keys local and out of committed files and logs.

After arranging testnet funding, register a separate delegated signer:

```bash
python examples/register_signer.py --testnet --execute
```

This sends a real registration request. It derives the account address from the
account key; ensure that address matches `RISEX_ACCOUNT`.
Follow [testnet integration](testnet-integration.md) for prerequisites.

Construct a trading client with
`RiseXClient(mainnet=False, account=account, signer=LocalSigner(key))`.
`await client.get_signer_status()` returns a model with `.active`.
Ordinary signed operations use the delegated signer; registration/revocation
require the account signer. You can inject a custom `Signer` for another key
store; its interface is documented in the reference.

Chain IDs, domains and contracts are discovered when needed.
Use `await client.initialize()` to explicitly validate metadata before other work.

## 6. Submit, observe and cancel orders

Start with the bounded testnet example after registration and funding:

```bash
python examples/order_lifecycle.py --testnet --execute --maximum-notional 20
```

It reads metadata, chooses the minimum representable quantity and a post-only
bid below the mark, submits, cancels once and queries terminal state.
The notional bound is an example limit; a resting order may still fill.

This helper submits one limit order at a quantity/price selected by its caller:

```python
from decimal import Decimal

from risex import OrderRequest, OrderSide, OrderSubmission, RiseXClient


async def submit_limit(
    client: RiseXClient,
    market_id: int,
    quantity: Decimal,
    price: Decimal,
) -> OrderSubmission:
    return await client.place_order(
        OrderRequest(
            market_id=market_id,
            side=OrderSide.BUY,
            quantity=quantity,
            price=price,
            post_only=True,
        )
    )
```

The `OrderSubmission` is an acknowledgement. Query
`await client.get_order(receipt.order_id)` or consume private order/fill events
for execution state. An OPEN order may be partially filled: inspect `filled_size`.
FILLED/CANCELLED orders are terminal.

Cancel with `await client.cancel_order(receipt.order_id, market_id=market_id)`,
then `await client.wait_for_order(receipt.order_id)` to observe terminal state.
`cancel_all_orders(market_id)` cancels orders in that market.
Polling timeout does not itself send a cancellation.

For market orders, set `order_type=OrderType.MARKET` and `price=Decimal("0")`.
For a maximum acceptable buy price or minimum acceptable sell price, use
`OrderType.LIMIT` with `TimeInForce.IOC` or `TimeInForce.FOK`.
Default TIF is IOC for market orders, GTC for limits. `reduce_only=True` constrains
execution to a reduction. See the reference for other execution flags and bounds.

### Handle uncertainty

Catch `UnknownOutcomeError` around mutations, including cancellation and signer
registration. Persist its `context` and optional `request_id` in your application.
Context contains operation/account, nonce, market and available order/client IDs;
it contains no key or signature.

`await client.reconcile_submission(error)` inspects matching orders and nonce
consumption. Absence, `resolved=False` or an unused nonce does not prove failure.
Client order IDs aid recovery but do not make placement idempotent.
The SDK does not retry signed POSTs.

For uncertain registration/revocation, additionally check signer status.
For cancel-all, refresh open orders. Preserve uncertainty and query authoritative
state before deciding on another mutation; that decision belongs to the application.

## 7. Consume WebSockets and recover state

Run the finite public example without credentials:

```bash
python examples/public_stream.py --testnet
```

This standalone script consumes a full book snapshot and updates:

```python
import asyncio

from risex import ConnectionEvent, LocalOrderbook, OrderbookEvent, RiseXClient


async def main():
    book = LocalOrderbook(1)
    async with RiseXClient(mainnet=False) as client:
        async with client.stream_orderbook(market_ids=[1]) as stream:
            async with asyncio.timeout(30):
                received = 0
                async for event in stream:
                    book.apply(event)
                    if isinstance(event, ConnectionEvent):
                        print(event.state, "stale:", event.stale)
                        continue
                    if isinstance(event, OrderbookEvent):
                        print(book.snapshot.bids[:1], book.snapshot.asks[:1])
                        received += 1
                        if received == 3:
                            break


if __name__ == "__main__":
    asyncio.run(main())
```

Stream factories are synchronous: call `client.stream_orderbook()` without
`await`, then use the async context manager/iterator.
A stream starts when iterated, owns one socket and supports one concurrent reader.
Close streams after early exits; the context above does this automatically.
Closing a stream does not close the client.

Public streams cover orderbook, trades and oracle; private streams cover orders,
positions and fills, using the configured account/registered signer.
Omit `market_ids` for all markets or pass numeric IDs to filter.
Handle `ConnectionEvent` alongside typed data events.

Feed every connection event into `LocalOrderbook`: stale state is invalidated
until a fresh WebSocket snapshot arrives. A limited REST book cannot seed its
full-book CRC32 check. After `ChecksumMismatchError`, close the stream and start
a new subscription.

Private orders/positions have snapshots; fills, public trades and oracle do not
promise replay. After reconnect, refresh `get_account_snapshot()`, backfill
`iter_trade_history(start_time=checkpoint_ns)`, and deduplicate by fill ID.
Persist checkpoints/deduplication state in your application.

`python examples/private_stream.py --testnet` demonstrates fills and REST recovery.
It runs until interrupted and does not submit orders. Live fills require activity
on that account. Private reconnects authenticate with a fresh server nonce;
expired/revoked authorization terminates the stream.

## 8. Troubleshooting and application boundary

| Symptom | Next step |
| --- | --- |
| `ModuleNotFoundError: risex` | Activate the intended venv and install with its `python -m pip` |
| Missing account/key variable | Set the example's process environment; `.env` is not loaded |
| `AuthenticationError` | Check identities, selected environment, registration and expiry |
| `PrecisionError`/invalid request | Use Decimal strings, current steps/minimum size and valid flags |
| `UnknownOutcomeError` | Persist context and reconcile; do not blindly place again |
| `OrderWaitTimeoutError` | Inspect order/open orders; the waiter sent no cancellation |
| `NonceExhaustedError` | Wait for authoritative anchor advancement and check other account users |
| `RateLimitError` | Inspect `retry_after`; coordinate clients sharing an IP |
| `ProtocolError`/`SubscriptionError` | Check endpoints/filters and provider payload/ack compatibility |
| `ChecksumMismatchError`/`StaleOrderbookError` | Obtain a fresh full book snapshot |
| `ReconnectExhaustedError` | Restore connectivity, restart the stream and reconcile state |

Use `APIError.status_code`, `.code` and `.request_id` for diagnostics.
Keep keys and signed authentication frames out of logs.

Import supported objects from `risex`. Put symbol mapping, normalized models,
hedging, execution limits and persistence in your application.

Live public data, testnet account reads and authenticated order/position snapshots
are verified. Funded trading, live fills and private reconnect/expiry scenarios
remain wallet-gated. Consult the [testnet integration guide](testnet-integration.md) before
treating an offline scenario as verified exchange behavior.
