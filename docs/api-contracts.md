# RISEx contracts implemented

Contracts were checked against the official reference, its embedded OpenAPI
schema and integration guide. Public deployments were read without credentials.
Signing and funded lifecycle behavior are verified offline. Live testnet account
reads, active signer status and authenticated order/position snapshots also pass;
funded mutations and private reconnect/expiry behavior remain unverified live.

## Environments and discovery

| Environment | REST | WebSocket |
| --- | --- | --- |
| Mainnet (default) | `https://api.rise.trade` | `wss://ws.rise.trade/ws` |
| Testnet (`mainnet=False`) | `https://api.testnet.rise.trade` | `wss://api.testnet.rise.trade/ws/` |

The older documented `wss://ws.testnet.rise.trade/ws` returned HTTP 525.
The authentication reference supplies the working testnet API-host WebSocket
URL; public streaming checks pass at that URL, including its trailing slash.
No automatic environment fallback exists.

`GET /v1/auth/eip712-domain` supplies name/version/chain ID/verifying contract.
`GET /v1/system/config` supplies chain and router/auth/USDC addresses. Initialization
requires matching chain IDs and authorization addresses; permit targets use
the router, not the domain's authorization contract. Values are not hardcoded.

Deployed responses commonly wrap generated schemas as
`{"data": {...}, "request_id": "..."}`. Both bare and wrapped shapes are supported.

## REST surface

| Capability | Endpoint | Important fields |
| --- | --- | --- |
| Markets | `GET /v1/markets` | Repeated numeric `market_ids`; `force_refresh` |
| Depth-limited book | `GET /v1/orderbook` | `market_id`, `limit` 1–250 |
| Bitmap nonce | `GET /v1/nonce-state/{account}` | `nonce_anchor`, `current_bitmap_index`, hexadecimal `bitmap` |
| Signer status | `GET /v1/auth/session-key-status` | Account/signer; only status 1 is active |
| Registration/revocation | `POST /v1/auth/register-signer`, `/v1/auth/revoke-signer` | Explicit account signature; registration additionally verifies delegated signer |
| Collateral balance | `GET /v1/account/balance` | `account`, `token` |
| Cross-margin balance | `GET /v1/account/cross-margin-balance` | `account` |
| Direct position | `GET /v1/account/position` | `account`, `market_id` |
| Indexed positions | `GET /v1/positions` | `account`, `page`, `page_size`, optional market |
| Open orders | `GET /v1/orders/open` | `account`, `start_index`, `limit`, optional market/repeated order IDs |
| Single order | `GET /v1/orders/by-id/{id}` | Composite ID; optional market |
| Order history | `GET /v1/orders` | `account`, page/limit, time/status/order filters; sort by created time |
| Account fills | `GET /v1/trade-history` | `account`, page/limit, time/market filters; sort by time |
| Placement | `POST /v1/orders/place` | Protocol units, execution flags, client ID, permit, `no_retry: true` |
| Cancellation | `POST /v1/orders/cancel` | Composite ID, market, permit, `no_retry: true` |
| Cancel all in market | `POST /v1/orders/cancel-all` | Market, permit |

The balance/position/order read endpoints do not require a signature. Returned account,
market, order and page scopes are checked where present.

### JWT sessions and fee tiers (development branch)

Verified against the current [OpenAPI schema](https://api.testnet.rise.trade/swagger/api.swagger.json),
[login reference](https://developer.rise.trade/reference/authservice_login),
[fee reference](https://developer.rise.trade/reference/feetierservice_getuserfees) and
[TP/SL reference](https://developer.rise.trade/reference/orderservice_gettpslorders).

- `GET /v1/auth/nonce` returns a one-use 32-byte hex nonce. Login signs
  `Login(address account,uint256 nonce,uint32 deadline)` with the native EIP-712
  domain. The nonce is signed as an integer but submitted in its original hex form.
  Signature wire encoding is 65-byte hex, not the compact base64 permit format.
- `POST /v1/auth/login` includes `signer` only for a delegated session key. The SDK
  checks that delegated keys are Active, and validates its signatures by recovery.
- `POST /v1/auth/refresh` rotates the refresh token. Replaying a consumed token can
  revoke the family. The SDK retires tokens before sending and never retries auth
  POSTs, including after cancellation or an unusable response.
- `GET /v1/user/fees` requires the returned bearer token. `expires_in` governs
  refresh timing; no token lifetime is hardcoded. Bps/progress are JSON numbers in
  this schema, so this endpoint parses their lexical values into Decimal.
- `POST /v1/auth/logout` sends the access token and refresh token to revoke the family.
  The SDK clears local tokens even when revocation cannot be confirmed.
- `GET /v1/orders/tpsl` uses repeated `statuses`, page/limit and an explicit
  `stop_type=STOP_TYPE_NONE` to include both TP and SL. Active snapshots include
  ACCEPTED and TRIGGERED. Response `total` determines pagination; missing pages,
  duplicates or changed totals are errors, not empty account state.

The new methods do not implement JWT trading, allowance approval, TP/SL placement
or cancellation. Account snapshots remain non-atomic reads.

## Numbers and lifecycle

Market/book/trade prices and quantities are human-unit decimal strings.
Contrary to raw-int wording in some schemas, deployed account balances and direct
position amounts are also human-unit decimal strings. Direct flat positions may
have market ID 0 and empty optional price/PnL fields; these parse to `None`.
Size, rather than a retained side field, determines whether a position is flat.

Open-order views expose integer `size_steps`/`price_ticks` and numeric enums.
History views expose human decimals and string enums; separate models avoid
mixing these contracts. Order status is NONE/OPEN/FILLED/CANCELLED; partial fills
retain a status and report `filled_size`, without an invented status enum.

Placement acknowledgements include composite order ID, transaction hash/block,
wide ID and optional fill fields. `filled_quantity` is a WAD integer string
or empty, converted by `OrderSubmission.filled_quantity`. It is not order status.
History timestamps and worker timestamps use nanoseconds; oracle payload
timestamps, permit deadlines and signer expiration use seconds.

## Signed protocol

EIP-712 structures:

```text
RegisterSigner(address account,address signer,string message,uint32 expiration,uint48 nonceAnchor,uint8 nonceBitmap)
VerifySigner(address account,uint48 nonceAnchor,uint8 nonceBitmap)
RevokeSigner(address account,address signer,uint48 nonceAnchor,uint8 nonceBitmap)
VerifyWitness(address account,address target,bytes32 hash,uint48 nonceAnchor,uint8 nonceBitmap,uint32 deadline)
RegisterV2(address signer,string message,uint256 nonce)
```

Registration uses the account's RegisterSigner signature and the delegated
signer's VerifySigner signature over the same nonce. Revocation uses the account
signer. REST permit signatures use compact 64-byte EIP-2098 base64; registration,
revocation and WebSocket signatures use hex-encoded canonical 65-byte signatures.

The order is packed into uint88:

```text
(market_id << 70) | (size_steps << 38) | (price_ticks << 14)
| (execution_flags << 6) | (version_1 << 1)
```

The validated widths are uint16 market, uint32 size steps, uint24 price ticks.
Execution flags contain side, post-only, reduce-only, STP, order type and TIF.
The low reserved bit remains zero.

Place action hash is keccak of ABI-encoded selector hash
`RISE_PERPS_PLACE_ORDER_V1`, uint8 header flags, uint88 order, uint16 builder ID,
optional uint16 builder fee **only when nonzero**, uint64 client ID, uint16 TTL.
Header bits are permit 0x01, nonzero builder ID 0x02, nonzero client ID 0x04,
nonzero TTL 0x10. The permit signs this hash with the selected router target.

Cancel action uses selector `RISE_PERPS_CANCEL_ORDER_V1`, uint256 market and
uint256 resting order ID. Composite order IDs are 24 bytes: the first eight
encode WideOrderID; the resting ID is that value shifted right by one.
Cancel-all uses `RISE_PERPS_CANCEL_ALL_ORDERS_V1` plus uint256 market.

Nonces have an account-wide uint48 anchor and 208 usable bitmap slots (0–207).
The client initially reserves at authoritative anchor + 1, then increments
slots. It rolls only after the preceding anchor is observed on chain.
Reservations are never reused locally. Serial mutation submission coordinates
one client; independent account users require external coordination.

Client order IDs are not promised to deduplicate submissions. Transport loss,
5xx/408, cancellation during POST and malformed acknowledgements remain
explicitly uncertain. History/nonce checks may help recovery but an absent record
does not establish that an order failed.

## WebSocket contracts and recovery

Subscribe with a channel and numeric market IDs. Subscription acceptance is
determined by `status`, not `type`; rejected frames may say `subscribed`.
Echoed filters are checked, and data received before acknowledgement is retained.
Each SDK stream owns a socket, avoiding conflicting repeated-subscription rules.

Public channels: `orderbook`, `trades`, `oracle`. Private: `orders`, `positions`,
`fills`. Orders and positions carry arrays; fill updates carry one fill object.
Private rows are checked against the authenticated account when an owner field
exists. Empty snapshots and omitted envelope market IDs are supported.

Private authentication obtains `GET /v1/auth/nonce`, signs RegisterV2 with the
delegated signer and message `WebSocket Authentication`, then sends `auth_v2`
with account, signer, server nonce and signature. The acknowledgement must
confirm both identities. A fresh server nonce is obtained on every connection;
auth rejection is terminal.

The WebSocket library handles protocol ping/pong frames. Buffers and retries
are bounded; the SDK applies backpressure rather than dropping messages.
Disconnected/reconnecting events mark prior state stale. There is no established
continuous event sequence or replay guarantee. Blockchain block/log numbers
must not be interpreted as consecutive subscription sequence numbers.

Book updates replace price levels and delete zero quantities. Full-book
CRC32-IEEE sorts bids descending and asks ascending, interleaves sides, converts
price/quantity to integer wei, colon-joins and hashes. A fresh full WebSocket
snapshot is required after invalidation; a limited REST book is insufficient.

Trades, oracle and fills have no snapshot/replay guarantee. Reconcile account
views and paginated fill history from an application checkpoint after reconnect,
deduplicating by fill ID. An assembled account snapshot is not atomic.

## Limits and exclusions

The reference describes an IP REST limit of 500 requests per 10 seconds and a
WebSocket JSON-request limit of 10 per second. Defaults pace below these limits;
coordination across clients/IP users belongs to the application.

No verified ticker contract is exposed. Funding/faucets, deposits/withdrawals,
durable state, synchronous wrappers and cross-exchange execution are outside
the implemented library boundary. Live checks require explicit opt-in; completed
checks are summarized in the [README](../README.md), with remaining scenarios
described in [testnet integration](testnet-integration.md).

## Primary sources

- [Integration guide and signing layouts](https://developer.rise.trade/reference/integration)
- [Authentication and current WebSocket URL](https://developer.rise.trade/reference/authentication-3)
- [Signing domain](https://developer.rise.trade/reference/authservice_geteip712domain)
- [System config](https://developer.rise.trade/reference/apiservice_getsystemconfig)
- [Nonce state](https://developer.rise.trade/reference/authservice_getnoncestate)
- [Placement](https://developer.rise.trade/reference/orderservice_placeorder)
- [Cancellation](https://developer.rise.trade/reference/orderservice_cancelorder)
- [Account balance](https://developer.rise.trade/reference/accountservice_getbalance)
- [Position](https://developer.rise.trade/reference/accountservice_getposition)
- [Order history](https://developer.rise.trade/reference/orderservice_getorderhistory)
- [Account trade history](https://developer.rise.trade/reference/orderservice_getaccounttradehistory)
- [Connection lifecycle](https://developer.rise.trade/reference/ws-connection)
- [Messages](https://developer.rise.trade/reference/ws-messages)
- [Orderbook checksum](https://developer.rise.trade/reference/orderbook-channel)
- [Private orders](https://developer.rise.trade/reference/orders-channel)
- [Private positions](https://developer.rise.trade/reference/positions-channel)
- [Private fills](https://developer.rise.trade/reference/fills-channel)
