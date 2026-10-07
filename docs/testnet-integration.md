# Funded testnet handoff

Development-branch validation on 2026-10-07 (Python 3.12): 167 offline tests,
strict mypy, Ruff, source/wheel builds and the isolated wheel consumer passed.
The separate session integration gate passed on testnet: delegated-key login,
fee reads, one refresh, active TP/SL enumeration and logout. The observed account
had no active conditional orders; nonempty TP/SL parsing/pagination and failure
cases are covered offline, not claimed as live funded-order validation.

The implementation is ready for wallet-based validation. Public testnet REST
and streams already pass. Offline servers verify signing and lifecycle contracts;
live active signer status, account reads and authenticated order/position
snapshots also pass. Funded executions, fills and private reconnect/expiry
scenarios remain to run.

## Inputs to prepare

Use a dedicated testnet account wallet and a separate delegated signer wallet.
Arrange testnet faucet/deposit collateral through RISEx's supported setup.
Ensure the account has enough available collateral for the chosen market's
minimum order and any registration requirements. The SDK does not acquire funds.

Keep private keys local. Supply process environment variables; the SDK never
loads `.env.example` or a `.env` automatically. Do not commit keys or paste them
into chat. The account key is needed only for explicit registration/revocation;
ordinary trading/private streams use the delegated signer.

```bash
export RISEX_ACCOUNT=0xYOUR_TESTNET_ACCOUNT
export RISEX_SIGNER_PRIVATE_KEY=YOUR_DELEGATED_SIGNER_KEY
export RISEX_ACCOUNT_PRIVATE_KEY=YOUR_ACCOUNT_KEY
python examples/account_reads.py --testnet
python examples/register_signer.py --testnet --execute
```

Registration defaults to a one-day lifetime, configurable with
`--lifetime-seconds`. Check returned signer status before trading. If registration
is uncertain, inspect signer status and nonce state before another attempt.
The account-address variable should match the account key's address.

If you already authorized the API signer through the RISEx UI, skip
`register_signer.py` and do not configure `RISEX_ACCOUNT_PRIVATE_KEY`.
Only the main account address and the separate API signer key are needed for
the checks below.

## Read/private-stream gate

Integration tests always construct clients with `mainnet=False`. Explicitly
enable wallet tests and provide the account and already registered delegated key:

```bash
export RISEX_TEST_ACCOUNT="$RISEX_ACCOUNT"
export RISEX_TEST_SIGNER_PRIVATE_KEY="$RISEX_SIGNER_PRIVATE_KEY"
RISEX_RUN_ACCOUNT_INTEGRATION=1 \
python -m pytest tests/integration/test_account_testnet.py -v -k 'not lifecycle'
```

This verifies account reads and authenticated orders/positions initial snapshots.
To observe fills and exercise REST recovery after an interruption:

```bash
python examples/private_stream.py --testnet
```

No fill snapshot is promised; a live fill requires actual execution. Verify fresh
authentication after reconnect with a still-active signer, then verify expired
or revoked signers fail clearly.

## Session and fee read gate

For this branch's fee/session reads, a separate gate exercises login, a fee read,
one explicit refresh, another fee read, active conditional orders and logout:

```bash
RISEX_RUN_SESSION_INTEGRATION=1 \
python -m pytest tests/integration/test_session_testnet.py -v
```

It uses `RISEX_TEST_ACCOUNT` and `RISEX_TEST_SIGNER_PRIVATE_KEY`. It never submits
orders, registers keys or grants allowances. The bot integration independently
performs login/fee/TP-SL reads/logout, recording only financial fields and token-free
metadata. Do not load a file's funded-trading enable flags when running read tests.

## Funded placement/cancellation gate

The test selects market 1 by default, constructs the minimum representable
quantity, places a post-only bid 15% below the observed mark, queries it, submits
one cancellation and checks terminal state. Its default price-times-quantity
bound is 20 quote units. This bound is an example limit, not a margin estimate
or a guarantee that a resting order cannot fill.

```bash
RISEX_RUN_TRADE_INTEGRATION=1 \
RISEX_TEST_MARKET_ID=1 \
RISEX_TEST_MAXIMUM_NOTIONAL=20 \
python -m pytest tests/integration/test_account_testnet.py -v -k lifecycle
```

If the market minimum exceeds the bound, the test fails before placing.
It cancels only the order it just submitted; it never calls cancel-all or flattens
positions. Unknown placement/cancellation propagates without a second POST.
Use the error's `context`, order history, nonce state and open-order view to
reconcile an uncertain result. Any unexpected fill fails the test; inspect and
resolve the resulting testnet exposure explicitly.

The standalone equivalent is:

```bash
python examples/order_lifecycle.py --testnet --execute --maximum-notional 20
```

After the initial gates pass, validate a small explicitly bounded IOC market
order, resulting fill events/history and position changes, partial fills,
reduce-only behavior, signer revocation/expiry, and reconnect recovery. These
need funded scenario setup; they are not claimed as live verified by the offline
suite. Record order IDs/transaction hashes and observed states without keys or
authentication frames.

## Application handoff

Inject the installed `RiseXClient` into a consumer-owned adapter and implement
normalized models and symbol mapping there. Keep persistence, hedging,
size/price decisions and cross-exchange failure handling in the application.
Use one signing client per account and coordinate any other account users.
Pin a reviewed package version or commit once these gates pass.
