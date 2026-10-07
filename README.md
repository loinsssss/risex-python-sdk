# RISEx Python SDK

An independently maintained, asynchronous Python library for RISEx REST and
WebSocket APIs. Python 3.11+; distribution `risex-python-sdk`, import `risex`.
The package has no dependency on a trading application, strategies, or application
configuration.

**New to the repository?** Start with the [developer guide](docs/developer-guide.md)
for installation, your first REST call, and your first WebSocket stream.

| Documentation | Use it for |
| --- | --- |
| [Developer guide](docs/developer-guide.md) | Setup, credentials, common workflows, recovery and troubleshooting |
| [Public API reference](docs/api-reference.md) | Methods, return types, request fields, configuration and errors |
| [Contributing](CONTRIBUTING.md) | Repository layout, local checks, fixtures and package verification |
| [Testnet integration](docs/testnet-integration.md) | Wallet preparation and explicit funded test gates |
| [Protocol contracts](docs/api-contracts.md) | Wire formats, signatures and provider discrepancies |
| [Security](SECURITY.md) | Credential handling and reporting security issues |

The SDK's public API consists of Python methods, request/response models and
stream events, documented in the public API reference. It does not expose an
HTTP server, so it has no SDK-owned Swagger UI or OpenAPI routes. RISEx's
[testnet Swagger UI](https://api.testnet.rise.trade/swagger/) documents the
upstream exchange routes used by the SDK.

Implemented: public market data; account balances, positions and history;
delegated signer registration/revocation; EIP-712 order permits; limit/market
orders, cancellation and outcome reconciliation; public and private streams;
and a checksummed local orderbook. This development branch adds JWT sessions,
account fee-tier reads, and paginated TP/SL enumeration.

This checkout is `0.1.0a3.dev0`; the published alpha is `0.1.0a2`. Offline lifecycle tests and live public
checks pass, as do live testnet account reads and authenticated order/position
snapshots. Funded transactions, live fills and private reconnect/expiry scenarios
remain to be validated, with explicit test gates provided.

## Installation and environments

Install the published alpha from PyPI (it does not include this branch's new APIs):

```bash
python -m pip install risex-python-sdk==0.1.0a2
```

For local development:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

An independent consumer can install the checkout with
`pip install -e /absolute/path/to/risex-python-sdk`, or install the built wheel.
The SDK is distributed under the [MIT license](LICENSE).

The repository owner's manual **Publish to PyPI** workflow builds, validates and
publishes releases from `main` using GitHub Trusted Publishing. Before the first
release, register a pending GitHub Trusted Publisher in your PyPI account:

| Field | Value |
| --- | --- |
| PyPI project name | `risex-python-sdk` |
| GitHub owner | `loinsssss` |
| Repository | `risex-python-sdk` |
| Workflow filename | `publish.yml` |
| Environment | `pypi` |

Run the workflow on `main` with the exact package version, initially `0.1.0a2`.
Only the repository owner's account can run the publishing job. No PyPI token
needs to be stored in GitHub. For subsequent releases, update the version in
`pyproject.toml`, `risex.__version__` and the HTTP user agent together; PyPI release
versions cannot be overwritten.

```python
from risex import RiseXClient, RiseXConfig

client = RiseXClient()  # mainnet, the default
client = RiseXClient(mainnet=True)  # mainnet
client = RiseXClient(mainnet=False)  # testnet: both REST and WebSocket
client = RiseXClient(RiseXConfig(mainnet=False, request_timeout=15))
```

Each constructed client must be closed, preferably with `async with`.
`RiseXConfig.testnet()` is also supported. Custom transport URLs can be supplied
explicitly through config. Conflicting config/`mainnet` arguments are rejected.
Imports do not connect, load `.env`, read keys, or create files.

## Public REST

```python
import asyncio
from risex import RiseXClient


async def main():
    async with RiseXClient(mainnet=False) as client:
        markets = await client.get_markets(market_ids=[1])
        print(markets.markets[0].config.name, markets.markets[0].mark_price)
        book = await client.get_orderbook(1, limit=10)
        print(book.bids, book.asks)


asyncio.run(main())
```

Market IDs are numeric provider IDs. Prices and quantities use exact `Decimal`
values; market metadata supplies `step_size`, `step_price`, and `min_order_size`.
`quantity_to_steps()` and `price_to_ticks()` reject invalid precision instead of
rounding. Floats are rejected at numerical protocol boundaries.

## Accounts and signing

Balance, position and order reads require an account address, but no signer. Use
`get_balances()`, `get_position(market_id)`, `get_positions()`,
`get_open_orders()`, `get_order(order_id)`, `get_order_history()` and
`get_trade_history()`. Paginated `iter_positions()`, `iter_open_orders()`,
`iter_order_history()` and `iter_trade_history()` have explicit page bounds.
`get_account_snapshot()` collects fresh REST balances, positions and open orders;
the constituent requests are separate exchange reads.

`get_tpsl_orders()` and `iter_tpsl_orders()` query conditional orders, including both
take-profit and stop-loss by default. `get_account_snapshot(include_conditional_orders=True)`
adds all active TP/SL orders, including triggered orders that may still be executing.
`conditional_orders=None` means not queried; `()` means queried and empty.

Account fee tiers require a JWT session. With a configured owner or already-registered
session-key signer, call `await client.login()`, then `await client.get_user_fees()`.
The response supplies `tier`, `taker_bps`, `maker_bps` and the schedule/progress.
Rates are already basis points. See [the testnet example](examples/account_fees.py).

Sessions are held only in memory; public `client.session` metadata contains no tokens.
Fee reads refresh expiring sessions automatically, with a lock around token rotation.
Login/refresh/logout POSTs are attempted once. An uncertain or failed refresh discards
the local session, so callers must log in again instead of replaying a rotating token.
A fee-read HTTP 401 also invalidates the session and requires explicit login.
`await client.logout()` revokes this token family; closing the client only clears local
tokens and does not contact the server. These methods do not approve allowances,
register keys, place orders or change margin.

For signed operations, pass the account identity and its registered delegated
signer separately:

```python
import os
from risex import LocalSigner, RiseXClient

signer = LocalSigner(os.environ["RISEX_SIGNER_PRIVATE_KEY"])
client = RiseXClient(mainnet=False, account=os.environ["RISEX_ACCOUNT"], signer=signer)
```

`LocalSigner` stores a supplied key in memory. Applications can inject another
`Signer` implementing `address` and async `sign_typed_data(data) -> bytes`;
signatures must be canonical 65-byte `r + s + v`, with `v` 27/28.
Recovered signatures are checked against the injected signer's address.

`initialize()` fetches and cross-checks the signing domain, chain and contracts.
Addresses and chain IDs are discovered from the selected environment.
`register_signer(account_signer, expiration=...)` requires the account signer
and delegated signer; expiration is Unix seconds. `revoke_signer(account_signer)`
revokes the configured delegated signer. Registration is explicit, and
`get_signer_status().active` recognizes only the provider's active status.

## Orders and uncertain outcomes

```python
from decimal import Decimal
from risex import OrderRequest, OrderSide, UnknownOutcomeError

# Inside an async function, with a configured client and registered signer:
request = OrderRequest(
    market_id=1,
    side=OrderSide.BUY,
    quantity=Decimal("0.001"),
    price=Decimal("60000"),
    post_only=True,
)
try:
    receipt = await client.place_order(request)
except UnknownOutcomeError as error:
    resolution = await client.reconcile_submission(error)
    # Persist error.context; inspect resolution and account state before deciding.
    raise
else:
    current = await client.get_order(receipt.order_id)
    await client.cancel_order(receipt.order_id, market_id=1)
    terminal = await client.wait_for_order(receipt.order_id)
```

Choose actual quantities/prices using current market metadata. A market order
uses `order_type=OrderType.MARKET` with `price=Decimal("0")`, matching the native
zero-price encoding. For a maximum buy price or minimum sell price, use
`OrderType.LIMIT` with `TimeInForce.IOC` or `TimeInForce.FOK` instead.
Default time-in-force is IOC for market orders, GTC for limit orders.
GTT requires nonzero protocol `ttl_units`; reduce-only, post-only and STP flags
are validated. The SDK checks market activity, precision and protocol widths
before reserving a nonce.

A submission receipt is not a completed fill. Query order state, account fills
or private events for execution. `filled_quantity` on an IOC/FOK receipt is
converted from the provider's WAD integer; unavailable values remain `None`.
Partially filled orders retain provider status and a nonzero `filled_size`.
`wait_for_order()` times out without cancelling.

Signed POSTs are attempted once. Timeouts, cancellation during transmission,
server failures and unusable success responses raise `UnknownOutcomeError`
with non-secret recovery identifiers and a request ID when available.
`reconcile_submission()` queries order history/lookup and nonce state; absence
or an unused nonce does not prove rejection. Client order IDs are recovery
identifiers, **not idempotency keys**. Persist them in the consuming application.
Signer registration/revocation uncertainty should additionally be checked with
`get_signer_status()`; cancel-all needs a fresh open-order view.

Nonce reservations and signed mutations are coordinated within one client.
Use one signing client per account, with exclusive ownership of that account's
bitmap nonce range. Multiple clients, processes, manual trading tools or other
signers sharing the account require external coordination. Reserved nonces are
never recycled locally, including after errors.

## WebSockets

The implementation is in `src/risex/websocket.py`; subscriptions are exposed by
the public client:

| Public streams | Private streams (account + registered signer) |
| --- | --- |
| `stream_orderbook()` | `stream_orders()` |
| `stream_trades()` | `stream_positions()` |
| `stream_oracle()` | `stream_fills()` |

`stream(channel, market_ids=[1])` is the general subscription API. Omit IDs to
subscribe to all markets. Each stream owns a socket, starts when iterated,
supports one reader, and must be closed when consumption ends.

```python
from risex import ConnectionEvent, LocalOrderbook, RiseXClient

book = LocalOrderbook(1)
async with RiseXClient(mainnet=False) as client:
    async with client.stream_orderbook(market_ids=[1]) as stream:
        async for event in stream:
            book.apply(event)
            if isinstance(event, ConnectionEvent):
                print(event.state, "stale:", event.stale)
            else:
                print(book.snapshot.bids[:1], book.snapshot.asks[:1])
```

The stream checks acknowledgement status and filters, retains snapshots received
before acknowledgements, handles protocol ping/pong, and applies bounded
buffering and reconnect/resubscription. Private reconnects request a fresh
server nonce and authenticate again; authorization rejection terminates.

Consume `ConnectionEvent` alongside data events. Disconnects mark prior state
stale. Orders/positions supply snapshots; trades, oracle and fills have no replay
guarantee. After reconnect, refresh REST account views and backfill fill history
with an application checkpoint and ID deduplication.

`LocalOrderbook` replaces snapshots, applies/deletes levels, and validates
full-book CRC32. It invalidates on connection events or checksum failure.
After `ChecksumMismatchError`, close the stream and open a fresh one.
A depth-limited REST book cannot seed full-book checksum validation.

## Limits and errors

Read retries cover transient GET transport failures and HTTP
429/500/502/503/504, within configured bounds. `Retry-After` is honored or the
request fails when it exceeds the delay budget. `APIError` preserves status,
code and request ID; `RateLimitError` also exposes retry delay.

Default per-client pacing is 40 REST requests/second and 8 WebSocket JSON
requests/second, shared across its streams. Applications must coordinate
multiple clients sharing an IP. Timeouts, buffers, read retries, reconnect
budgets and pacing are configured with `RiseXConfig`.

Parsing errors raise `ProtocolError`; authentication/subscription errors raise
`AuthenticationError`/`SubscriptionError`. Stream transport exhaustion raises
`ReconnectExhaustedError`. These errors do not silently replace stale state.

## Examples and checks

```bash
python examples/public_rest.py --testnet
python examples/public_stream.py --testnet
python examples/account_reads.py --testnet
python examples/private_stream.py --testnet
# Mutating examples require explicit execution:
python examples/register_signer.py --testnet --execute
python examples/order_lifecycle.py --testnet --execute

python -m pytest
ruff check .
ruff format --check .
mypy
python -m build
RISEX_RUN_INTEGRATION=1 python -m pytest tests/integration/test_public_testnet.py -v
```

Examples read explicitly provided process environment variables; `.env.example`
lists the names. Public examples require no credentials.
See [testnet handoff](docs/testnet-integration.md) for wallet setup and funded
gates and [API contracts](docs/api-contracts.md) for encoding details.

The offline suite covers signing, exact units, pagination, order lifecycle,
uncertain outcomes and local stream recovery. Live public data, testnet account
reads and authenticated order/position snapshots have also passed. Live order
execution, fills, signer administration and private reconnect/expiry scenarios
remain unverified. Local checks used Python 3.12; CI is configured for 3.11–3.13.

Keep symbol mapping, normalized models, hedging, execution limits and persistence
in the consuming application.
