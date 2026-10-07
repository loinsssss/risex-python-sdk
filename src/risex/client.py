"""Small public API independent of any trading strategy or consumer repository."""

from __future__ import annotations

import asyncio
import base64
import math
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Sequence
from typing import Any, Self, TypeVar, get_args

import httpx
from pydantic import BaseModel, ValidationError

from . import auth
from .config import RiseXConfig, validate_market_id, validate_market_ids
from .exceptions import (
    APIError,
    AuthenticationError,
    ClientClosedError,
    MutationContext,
    OrderWaitTimeoutError,
    ProtocolError,
    UnknownOutcomeError,
)
from .models import (
    AccountSnapshot,
    AccountUpdate,
    Balance,
    Balances,
    Cancellation,
    Channel,
    DecodedTransaction,
    Fill,
    LoginSession,
    Market,
    MarketsResponse,
    NonceState,
    OpenOrder,
    OpenOrdersResponse,
    Order,
    OrderbookSnapshot,
    OrderHistory,
    OrderRequest,
    OrderResponse,
    OrderStatus,
    OrderSubmission,
    OrderType,
    PortfolioDetails,
    Position,
    PositionResponse,
    PositionsResponse,
    ProtocolMetadata,
    SessionKeyStatus,
    SignerRegistration,
    SigningDomain,
    StopType,
    SubmissionResolution,
    SystemConfig,
    TimeInForce,
    TpslCancellation,
    TpslOrder,
    TpslOrdersResponse,
    TpslStatus,
    TradeHistory,
    UserFees,
)
from .nonce import Nonce, NonceManager
from .rate_limit import RateLimiter
from .rest import RestClient
from .session import JwtSession
from .signing import (
    Signer,
    account_setting_action_hash,
    address,
    cancel_action_hash,
    cancel_all_action_hash,
    pack_order,
    place_action_hash,
    signature_hex,
    typed_data,
    uint,
)
from .signing import (
    order_id as validate_order_id,
)
from .signing import (
    resting_order_id as decode_resting_id,
)
from .websocket import RiseXStream

ModelT = TypeVar("ModelT", bound=BaseModel)
FirstT = TypeVar("FirstT")
SecondT = TypeVar("SecondT")


async def _pair(first: Awaitable[FirstT], second: Awaitable[SecondT]) -> tuple[FirstT, SecondT]:
    """Join independent operations and clean up both when either fails or is cancelled."""
    first_task = asyncio.ensure_future(first)
    second_task = asyncio.ensure_future(second)
    try:
        first_result, second_result = await asyncio.gather(first_task, second_task)
        return first_result, second_result
    except BaseException:
        first_task.cancel()
        second_task.cancel()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
        raise


def _parse(model: type[ModelT], payload: dict[str, Any]) -> ModelT:
    try:
        return model.model_validate(payload)
    except (ValidationError, ValueError) as exc:
        raise ProtocolError(f"Invalid RISEx {model.__name__} response") from exc


def _page(page: int, limit: int) -> None:
    if type(page) is not int or page < 1:
        raise ValueError("page must be a positive integer")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("page size/limit must be an integer between 1 and 1000")


def _max_pages(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("max_pages must be a positive integer")


class RiseXClient:
    def __init__(
        self,
        config: RiseXConfig | None = None,
        *,
        mainnet: bool | None = None,
        account: str | None = None,
        signer: Signer | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if mainnet is not None and type(mainnet) is not bool:
            raise TypeError("mainnet must be bool")
        if config is None:
            config = RiseXConfig(mainnet=True if mainnet is None else mainnet)
        elif mainnet is not None and mainnet != config.mainnet:
            raise ValueError("mainnet argument conflicts with the supplied config")
        self.config = config
        self._account = address(account) if account is not None else None
        self._signer = signer
        if signer is not None:
            address(signer.address)
        self._rest = RestClient(config, transport=transport)
        self._jwt = JwtSession(self._rest)
        self._streams: set[RiseXStream] = set()
        self._closed = False
        self._metadata: ProtocolMetadata | None = None
        self._initialize_lock = asyncio.Lock()
        self._nonces = NonceManager(self.get_nonce_state)
        self._websocket_limiter = RateLimiter(config.websocket_requests_per_second)

    @property
    def account(self) -> str | None:
        return self._account

    @property
    def signer(self) -> Signer | None:
        return self._signer

    def _account_address(self, account: str | None = None) -> str:
        selected = account if account is not None else self._account
        if selected is None:
            raise AuthenticationError("Supply an account address for account operations")
        return address(selected)

    def _credentials(self) -> tuple[str, Signer]:
        account = self._account_address()
        if self._signer is None:
            raise AuthenticationError("Supply an injected Signer for signed operations")
        return account, self._signer

    def _ensure_open(self) -> None:
        if self._closed:
            raise ClientClosedError("RISEx client is closed")

    async def __aenter__(self) -> Self:
        self._ensure_open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._jwt.close()
        try:
            results = await asyncio.gather(
                *(stream.aclose() for stream in tuple(self._streams)), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        finally:
            await self._rest.aclose()

    async def get_markets(
        self, *, market_ids: Sequence[int] = (), force_refresh: bool = False
    ) -> MarketsResponse:
        self._ensure_open()
        ids = validate_market_ids(tuple(market_ids))
        if type(force_refresh) is not bool:
            raise TypeError("force_refresh must be bool")
        params = [("force_refresh", str(force_refresh).lower())]
        params.extend(("market_ids", str(market_id)) for market_id in ids)
        payload = await self._rest.get("/v1/markets", params=params)
        return _parse(MarketsResponse, payload)

    async def get_orderbook(self, market_id: int, *, limit: int = 50) -> OrderbookSnapshot:
        self._ensure_open()
        validate_market_id(market_id)
        if type(limit) is not int or not 1 <= limit <= 250:
            raise ValueError("limit must be an integer between 1 and 250")
        payload = await self._rest.get(
            "/v1/orderbook", params=[("market_id", str(market_id)), ("limit", str(limit))]
        )
        book = _parse(OrderbookSnapshot, payload)
        if book.market_id != market_id:
            raise ProtocolError("Orderbook response belongs to another market")
        return book

    async def get_signing_domain(self) -> SigningDomain:
        self._ensure_open()
        return _parse(SigningDomain, await self._rest.get("/v1/auth/eip712-domain"))

    async def get_system_config(self) -> SystemConfig:
        self._ensure_open()
        return _parse(SystemConfig, await self._rest.get("/v1/system/config"))

    async def decode_transaction(self, tx_hash: str) -> DecodedTransaction:
        """Read a transaction outcome and any provider-decoded contract revert details."""
        self._ensure_open()
        if not isinstance(tx_hash, str) or re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash) is None:
            raise ValueError("tx_hash must be 0x followed by 64 hexadecimal characters")
        payload = await self._rest.get(f"/v1/tx/{tx_hash}", error_is_data=True)
        result = _parse(DecodedTransaction, payload)
        if result.tx_hash is not None and result.tx_hash.lower() != tx_hash.lower():
            raise ProtocolError("Decoded transaction response belongs to another transaction")
        return result

    @property
    def session(self) -> LoginSession | None:
        """Session metadata only; closing the client discards tokens without network I/O."""
        return self._jwt.info

    async def login(self) -> LoginSession:
        """Sign Login with the configured owner or registered session key. No allowance changes."""
        self._ensure_open()
        account, signer = self._credentials()
        if address(signer.address) != account and not (await self.get_signer_status()).active:
            raise AuthenticationError("JWT login requires an active registered session key")
        return await self._jwt.login(account, signer, self.initialize)

    async def refresh_session(self) -> LoginSession:
        self._ensure_open()
        return await self._jwt.refresh()

    async def logout(self) -> bool:
        """Revoke this token family once; always discard its local tokens, including on failure."""
        self._ensure_open()
        return await self._jwt.logout()

    async def get_user_fees(self) -> UserFees:
        """Read the logged-in account's fees; automatically refresh an expiring session."""
        self._ensure_open()
        token = await self._jwt.access_token()
        try:
            payload = await self._rest.get(
                "/v1/user/fees", bearer_token=token, exact_json_numbers=True
            )
        except APIError as error:
            if error.status_code == 401:
                self._jwt.invalidate(token)
            raise
        return _parse(UserFees, payload)

    async def initialize(self, *, force_refresh: bool = False) -> ProtocolMetadata:
        """Fetch and cross-check signing domain and router; no signing or mutations."""
        self._ensure_open()
        if type(force_refresh) is not bool:
            raise TypeError("force_refresh must be bool")
        async with self._initialize_lock:
            if self._metadata is None or force_refresh:
                domain, system = await _pair(self.get_signing_domain(), self.get_system_config())
                if domain.chain_id != system.chain.chain_id:
                    raise ProtocolError("Signing domain and system config have different chain IDs")
                if domain.verifying_contract != system.addresses.auth:
                    raise ProtocolError(
                        "Signing domain does not identify the Authorization contract"
                    )
                if int(system.addresses.router, 16) == 0 or int(domain.verifying_contract, 16) == 0:
                    raise ProtocolError("Signing contracts must not be zero addresses")
                self._metadata = ProtocolMetadata(domain=domain, system=system)
            return self._metadata

    async def get_nonce_state(self, *, account: str | None = None) -> NonceState:
        self._ensure_open()
        selected = self._account_address(account)
        return _parse(NonceState, await self._rest.get(f"/v1/nonce-state/{selected}"))

    async def get_signer_status(
        self, *, account: str | None = None, signer: str | None = None
    ) -> SessionKeyStatus:
        self._ensure_open()
        selected = self._account_address(account)
        if signer is None:
            if self._signer is None:
                raise AuthenticationError("Supply a signer address or configure an injected Signer")
            signer = self._signer.address
        return _parse(
            SessionKeyStatus,
            await self._rest.get(
                "/v1/auth/session-key-status",
                params=[("account", selected), ("signer", address(signer))],
            ),
        )

    async def _submit(
        self,
        path: str,
        payload: dict[str, Any],
        context: MutationContext,
        model: type[ModelT],
    ) -> ModelT:
        result = await self._rest.post(path, payload=payload, context=context)
        try:
            return _parse(model, result.data)
        except ProtocolError as exc:
            raise UnknownOutcomeError(context, request_id=result.request_id) from exc

    async def register_signer(
        self,
        account_signer: Signer,
        *,
        expiration: int,
        message: str = "RISEx session key",
        label: str = "",
    ) -> SignerRegistration:
        self._ensure_open()
        account, signer = self._credentials()
        if address(account_signer.address) != account:
            raise AuthenticationError("account_signer must sign for the configured account")
        uint(expiration, 32, "expiration")
        if expiration <= int(time.time()):
            raise ValueError("signer expiration must be in the future")
        if not isinstance(message, str) or not isinstance(label, str):
            raise TypeError("message and label must be strings")
        metadata = await self.initialize()
        async with self._nonces.operation_lock:
            nonce = await self._nonces.reserve()
            common = {
                "account": account,
                "nonceAnchor": nonce.anchor,
                "nonceBitmap": nonce.bitmap_index,
            }
            registration = typed_data(
                metadata.domain.signing_values(),
                "RegisterSigner",
                {
                    **common,
                    "signer": address(signer.address),
                    "message": message,
                    "expiration": expiration,
                },
            )
            verification = typed_data(metadata.domain.signing_values(), "VerifySigner", common)
            account_signature, signer_signature = await _pair(
                auth.sign(account_signer, registration), auth.sign(signer, verification)
            )
            context = MutationContext("register_signer", account, nonce.anchor, nonce.bitmap_index)
            return await self._submit(
                "/v1/auth/register-signer",
                {
                    "account": account,
                    "signer": address(signer.address),
                    "message": message,
                    "expiration": str(expiration),
                    "nonce_anchor": str(nonce.anchor),
                    "nonce_bitmap_index": nonce.bitmap_index,
                    "account_signature": signature_hex(account_signature),
                    "signer_signature": signature_hex(signer_signature),
                    "label": label,
                },
                context,
                SignerRegistration,
            )

    async def revoke_signer(self, account_signer: Signer) -> SignerRegistration:
        self._ensure_open()
        account, signer = self._credentials()
        if address(account_signer.address) != account:
            raise AuthenticationError("account_signer must sign for the configured account")
        metadata = await self.initialize()
        async with self._nonces.operation_lock:
            nonce = await self._nonces.reserve()
            data = typed_data(
                metadata.domain.signing_values(),
                "RevokeSigner",
                {
                    "account": account,
                    "signer": address(signer.address),
                    "nonceAnchor": nonce.anchor,
                    "nonceBitmap": nonce.bitmap_index,
                },
            )
            signature = await auth.sign(account_signer, data)
            return await self._submit(
                "/v1/auth/revoke-signer",
                {
                    "account": account,
                    "signer": address(signer.address),
                    "nonce_anchor": str(nonce.anchor),
                    "nonce_bitmap_index": nonce.bitmap_index,
                    "account_signature": signature_hex(signature),
                },
                MutationContext("revoke_signer", account, nonce.anchor, nonce.bitmap_index),
                SignerRegistration,
            )

    async def get_balance(self, *, account: str | None = None, token: str | None = None) -> Balance:
        self._ensure_open()
        selected = self._account_address(account)
        if token is None:
            token = (await self.initialize()).system.addresses.usdc
        return _parse(
            Balance,
            await self._rest.get(
                "/v1/account/balance", params=[("account", selected), ("token", address(token))]
            ),
        )

    async def get_cross_margin_balance(self, *, account: str | None = None) -> Balance:
        self._ensure_open()
        return _parse(
            Balance,
            await self._rest.get(
                "/v1/account/cross-margin-balance",
                params=[("account", self._account_address(account))],
            ),
        )

    async def get_balances(self, *, account: str | None = None) -> Balances:
        self._ensure_open()
        selected = self._account_address(account)
        token = (await self.initialize()).system.addresses.usdc
        collateral, cross_margin = await _pair(
            self.get_balance(account=selected, token=token),
            self.get_cross_margin_balance(account=selected),
        )
        return Balances(
            account=selected, token=token, collateral=collateral, cross_margin=cross_margin
        )

    async def get_position(self, market_id: int, *, account: str | None = None) -> PositionResponse:
        self._ensure_open()
        validate_market_id(market_id)
        result = _parse(
            PositionResponse,
            await self._rest.get(
                "/v1/account/position",
                params=[("account", self._account_address(account)), ("market_id", str(market_id))],
            ),
        )
        if result.position.market_id not in (0, market_id):
            raise ProtocolError("Position response belongs to another market")
        return result

    async def get_portfolio_details(self, *, account: str | None = None) -> PortfolioDetails:
        self._ensure_open()
        selected = self._account_address(account)
        result = _parse(
            PortfolioDetails,
            await self._rest.get("/v1/portfolio/details", params=[("account", selected)]),
        )
        if result.account != selected:
            raise ProtocolError("Portfolio response belongs to another account")
        ids = [p.market_id for p in result.positions]
        if len(ids) != len(set(ids)) or any(i <= 0 for i in ids):
            raise ProtocolError("Portfolio contains duplicate or invalid market IDs")
        return result

    async def update_leverage(self, market_id: int, leverage: int) -> AccountUpdate:
        """Single signed mutation; an unknown outcome must be reconciled, not replayed."""
        self._ensure_open()
        action_hash = account_setting_action_hash(market_id, leverage, setting="leverage")
        markets = await self.get_markets(market_ids=[market_id], force_refresh=True)
        market = next((m for m in markets.markets if m.market_id == market_id), None)
        if market is None or market.config.max_leverage is None:
            raise ProtocolError("Cannot verify market leverage limit")
        if leverage > market.config.max_leverage:
            raise ValueError("Leverage exceeds the market maximum")
        return await self._update_account_setting(
            market_id,
            "/v1/account/leverage",
            "update_leverage",
            {"leverage": str(leverage)},
            action_hash,
        )

    async def update_margin_mode(self, market_id: int, *, isolated: bool) -> AccountUpdate:
        self._ensure_open()
        if type(isolated) is not bool:
            raise TypeError("isolated must be bool")
        value = int(isolated)
        return await self._update_account_setting(
            market_id,
            "/v1/account/margin-mode",
            "update_margin_mode",
            {"margin_mode": value},
            account_setting_action_hash(market_id, value, setting="margin_mode"),
        )

    async def _update_account_setting(
        self, market_id: int, path: str, operation: str, fields: dict[str, Any], action_hash: bytes
    ) -> AccountUpdate:
        account, _ = self._credentials()
        await self.initialize()
        async with self._nonces.operation_lock:
            nonce = await self._nonces.reserve()
            permit = await self._permit(action_hash, nonce)
            return await self._submit(
                path,
                {"market_id": str(market_id), **fields, "permit_params": permit},
                MutationContext(
                    operation, account, nonce.anchor, nonce.bitmap_index, market_id=market_id
                ),
                AccountUpdate,
            )

    async def cancel_all_tpsl_orders(self, market_id: int) -> TpslCancellation:
        """Cancel accepted TP/SLs only; triggered orders still require reconciliation."""
        self._ensure_open()
        validate_market_id(market_id)
        account, signer = self._credentials()
        metadata = await self.initialize()
        deadline = int(time.time()) + self.config.permit_ttl_seconds
        signature = await auth.sign(
            signer,
            typed_data(
                metadata.domain.signing_values(),
                "CancelAllTpslOrders",
                {
                    "account": account,
                    "marketId": market_id,
                    "deadline": deadline,
                },
            ),
        )
        return await self._submit(
            "/v1/orders/tpsl/cancel-all",
            {
                "account": account,
                "market_id": str(market_id),
                "signer": address(signer.address),
                "deadline": deadline,
                "signature": base64.b64encode(signature).decode("ascii"),
            },
            MutationContext("cancel_all_tpsl_orders", account, None, None, market_id=market_id),
            TpslCancellation,
        )

    async def get_positions(
        self,
        *,
        account: str | None = None,
        market_id: int | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> PositionsResponse:
        self._ensure_open()
        _page(page, page_size)
        selected = self._account_address(account)
        params = [("account", selected), ("page", str(page)), ("page_size", str(page_size))]
        if market_id is not None:
            params.append(("market_id", str(validate_market_id(market_id))))
        response = _parse(PositionsResponse, await self._rest.get("/v1/positions", params=params))
        if response.page != page:
            raise ProtocolError("Positions pagination did not advance as requested")
        if any(row.account != selected for row in response.positions):
            raise ProtocolError("Positions response belongs to another account")
        if market_id is not None and any(row.market_id != market_id for row in response.positions):
            raise ProtocolError("Positions response belongs to another market")
        return response

    async def iter_positions(
        self,
        *,
        account: str | None = None,
        market_id: int | None = None,
        page_size: int = 100,
        max_pages: int = 1000,
    ) -> AsyncIterator[Position]:
        _max_pages(max_pages)
        for page in range(1, max_pages + 1):
            response = await self.get_positions(
                account=account, market_id=market_id, page=page, page_size=page_size
            )
            for position in response.positions:
                yield position
            if not response.has_next_page:
                return
        raise ProtocolError("Positions exceeded max_pages; results may be incomplete")

    async def get_open_orders(
        self,
        *,
        account: str | None = None,
        market_id: int | None = None,
        start_index: int = 0,
        limit: int = 100,
        order_ids: Sequence[str] = (),
    ) -> OpenOrdersResponse:
        self._ensure_open()
        _page(1, limit)
        uint(start_index, 64, "start_index")
        selected = self._account_address(account)
        if len(order_ids) > 100:
            raise ValueError("At most 100 order IDs may be requested")
        params = [("account", selected), ("start_index", str(start_index)), ("limit", str(limit))]
        if market_id is not None:
            params.append(("market_id", str(validate_market_id(market_id))))
        params.extend(("order_ids", validate_order_id(value)) for value in order_ids)
        result = _parse(OpenOrdersResponse, await self._rest.get("/v1/orders/open", params=params))
        if result.account != selected or any(row.account != selected for row in result.orders):
            raise ProtocolError("Open orders response belongs to another account")
        if market_id is not None and any(row.market_id != market_id for row in result.orders):
            raise ProtocolError("Open orders response belongs to another market")
        if order_ids and any(
            row.order_id.lower() not in {value.lower() for value in order_ids}
            for row in result.orders
        ):
            raise ProtocolError("Open orders response ignored the order ID filter")
        return result

    async def iter_open_orders(
        self,
        *,
        account: str | None = None,
        market_id: int | None = None,
        page_size: int = 100,
        max_pages: int = 1000,
    ) -> AsyncIterator[OpenOrder]:
        _max_pages(max_pages)
        offset = 0
        for _ in range(max_pages):
            result = await self.get_open_orders(
                account=account, market_id=market_id, start_index=offset, limit=page_size
            )
            for order in result.orders:
                yield order
            offset += len(result.orders)
            if offset >= result.total_orders:
                return
            if not result.orders:
                raise ProtocolError("Open orders pagination returned an empty incomplete page")
        raise ProtocolError("Open orders exceeded max_pages; results may be incomplete")

    async def get_order(self, order_id: str, *, market_id: int | None = None) -> Order:
        self._ensure_open()
        validate_order_id(order_id)
        params = [] if market_id is None else [("market_id", str(validate_market_id(market_id)))]
        response = _parse(
            OrderResponse,
            await self._rest.get(f"/v1/orders/by-id/{order_id}", params=params),
        )
        if response.order.id.lower() != order_id.lower():
            raise ProtocolError("Order lookup returned a different order")
        if market_id is not None and response.order.market_id != market_id:
            raise ProtocolError("Order lookup returned a different market")
        return response.order

    async def get_tpsl_orders(
        self,
        *,
        account: str | None = None,
        market_id: int | None = None,
        page: int = 1,
        limit: int = 100,
        statuses: Sequence[TpslStatus] = (),
        stop_type: StopType = "STOP_TYPE_NONE",
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> TpslOrdersResponse:
        """Read TP/SLs; explicitly send STOP_TYPE_NONE to include both stop types."""
        self._ensure_open()
        _page(page, limit)
        selected = self._account_address(account)
        if stop_type not in get_args(StopType):
            raise ValueError("Unknown stop type")
        if isinstance(statuses, str) or any(s not in get_args(TpslStatus) for s in statuses):
            raise ValueError("Unknown TP/SL status filter")
        if len(set(statuses)) != len(statuses):
            raise ValueError("Duplicate TP/SL statuses")
        params = [
            ("account", selected),
            ("page", str(page)),
            ("limit", str(limit)),
            ("stop_type", stop_type),
        ]
        self._history_filters(params, market_id, start_time, end_time)
        params.extend(("statuses", s) for s in statuses)
        result = _parse(TpslOrdersResponse, await self._rest.get("/v1/orders/tpsl", params=params))
        expected = min(limit, max(0, result.total - (page - 1) * limit))
        if result.page != page or result.limit != limit or len(result.orders) != expected:
            raise ProtocolError("TP/SL pagination is inconsistent; results may be incomplete")
        if len({o.order_id for o in result.orders}) != len(result.orders):
            raise ProtocolError("TP/SL response contains duplicate order IDs")
        for order in result.orders:
            if order.account != selected:
                raise ProtocolError("TP/SL response belongs to another account")
            if market_id is not None and order.market_id != market_id:
                raise ProtocolError("TP/SL response belongs to another market")
            if statuses and order.status not in statuses:
                raise ProtocolError("TP/SL response ignored status filters")
            if stop_type != "STOP_TYPE_NONE" and order.stop_type != stop_type:
                raise ProtocolError("TP/SL response ignored stop type filter")
        return result

    async def iter_tpsl_orders(
        self,
        *,
        account: str | None = None,
        market_id: int | None = None,
        page_size: int = 100,
        max_pages: int = 1000,
        statuses: Sequence[TpslStatus] = (),
        stop_type: StopType = "STOP_TYPE_NONE",
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> AsyncIterator[TpslOrder]:
        _max_pages(max_pages)
        seen: set[str] = set()
        total: int | None = None
        for page in range(1, max_pages + 1):
            result = await self.get_tpsl_orders(
                account=account,
                market_id=market_id,
                page=page,
                limit=page_size,
                statuses=statuses,
                stop_type=stop_type,
                start_time=start_time,
                end_time=end_time,
            )
            if total is not None and result.total != total:
                raise ProtocolError("TP/SL total changed during pagination; refresh the snapshot")
            total = result.total
            for order in result.orders:
                if order.order_id in seen:
                    raise ProtocolError(
                        "TP/SL pagination repeated an order; results may be incomplete"
                    )
                seen.add(order.order_id)
                yield order
            if not result.has_next_page:
                return
        raise ProtocolError("TP/SL pagination exceeded max_pages; results may be incomplete")

    async def get_order_history(
        self,
        *,
        account: str | None = None,
        market_id: int | None = None,
        page: int = 1,
        limit: int = 100,
        statuses: Sequence[OrderStatus] = (),
        order_ids: Sequence[str] = (),
        start_time: int | None = None,
        end_time: int | None = None,
        descending: bool = True,
    ) -> OrderHistory:
        self._ensure_open()
        _page(page, limit)
        selected = self._account_address(account)
        params = [
            ("account", selected),
            ("page", str(page)),
            ("limit", str(limit)),
            ("sorted_by", "-created_at" if descending else "created_at"),
        ]
        self._history_filters(params, market_id, start_time, end_time)
        for status in statuses:
            if status not in (
                "ORDER_STATUS_NONE",
                "ORDER_STATUS_OPEN",
                "ORDER_STATUS_FILLED",
                "ORDER_STATUS_CANCELLED",
            ):
                raise ValueError("Unknown RISEx order status")
            params.append(("statuses", status))
        if len(order_ids) > 100:
            raise ValueError("At most 100 order IDs may be requested")
        params.extend(("order_ids", validate_order_id(value)) for value in order_ids)
        result = _parse(OrderHistory, await self._rest.get("/v1/orders", params=params))
        if result.page != page or any(row.sender != selected for row in result.orders):
            raise ProtocolError("Order history pagination/account scope is inconsistent")
        if market_id is not None and any(row.market_id != market_id for row in result.orders):
            raise ProtocolError("Order history returned another market")
        if order_ids and any(
            row.id.lower() not in {value.lower() for value in order_ids} for row in result.orders
        ):
            raise ProtocolError("Order history ignored the order ID filter")
        return result

    async def iter_order_history(
        self,
        *,
        account: str | None = None,
        market_id: int | None = None,
        page_size: int = 100,
        max_pages: int = 1000,
    ) -> AsyncIterator[Order]:
        _max_pages(max_pages)
        for page in range(1, max_pages + 1):
            result = await self.get_order_history(
                account=account, market_id=market_id, page=page, limit=page_size
            )
            for order in result.orders:
                yield order
            if not result.has_next_page:
                return
        raise ProtocolError("Order history exceeded max_pages; results may be incomplete")

    @staticmethod
    def _history_filters(
        params: list[tuple[str, str]],
        market_id: int | None,
        start_time: int | None,
        end_time: int | None,
    ) -> None:
        if market_id is not None:
            params.append(("market_id", str(validate_market_id(market_id))))
        for name, value in (("start_time", start_time), ("end_time", end_time)):
            if value is not None:
                params.append((name, str(uint(value, 64, name))))
        if start_time is not None and end_time is not None and end_time < start_time:
            raise ValueError("end_time must not precede start_time")

    async def get_trade_history(
        self,
        *,
        account: str | None = None,
        market_id: int | None = None,
        page: int = 1,
        limit: int = 100,
        start_time: int | None = None,
        end_time: int | None = None,
        descending: bool = True,
    ) -> TradeHistory:
        self._ensure_open()
        _page(page, limit)
        selected = self._account_address(account)
        params = [
            ("account", selected),
            ("page", str(page)),
            ("limit", str(limit)),
            ("sorted_by", "-time" if descending else "time"),
        ]
        self._history_filters(params, market_id, start_time, end_time)
        result = _parse(TradeHistory, await self._rest.get("/v1/trade-history", params=params))
        if result.page != page or result.wallet_address != selected:
            raise ProtocolError("Trade history pagination/account scope is inconsistent")
        if market_id is not None and any(row.market_id != market_id for row in result.trades):
            raise ProtocolError("Trade history returned another market")
        return result

    async def iter_trade_history(
        self,
        *,
        account: str | None = None,
        market_id: int | None = None,
        page_size: int = 100,
        max_pages: int = 1000,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> AsyncIterator[Fill]:
        _max_pages(max_pages)
        for page in range(1, max_pages + 1):
            result = await self.get_trade_history(
                account=account,
                market_id=market_id,
                page=page,
                limit=page_size,
                start_time=start_time,
                end_time=end_time,
            )
            for fill in result.trades:
                yield fill
            if not result.has_next_page:
                return
        raise ProtocolError("Trade history exceeded max_pages; results may be incomplete")

    async def _permit(self, action_hash: bytes, nonce: Nonce) -> dict[str, Any]:
        account, signer = self._credentials()
        return await auth.permit(
            await self.initialize(),
            account=account,
            signer=signer,
            action_hash=action_hash,
            nonce=nonce,
            deadline=int(time.time()) + self.config.permit_ttl_seconds,
        )

    async def place_order(self, request: OrderRequest) -> OrderSubmission:
        self._ensure_open()
        account, _ = self._credentials()
        if not isinstance(request, OrderRequest):
            raise TypeError("request must be an OrderRequest")
        request = OrderRequest.model_validate(request.model_dump())
        metadata = await self.initialize()
        if metadata.system.is_maintenance_mode:
            raise ProtocolError("RISEx reports active maintenance")
        response = await self.get_markets(market_ids=[request.market_id], force_refresh=True)
        market: Market | None = next(
            (value for value in response.markets if value.market_id == request.market_id), None
        )
        if market is None:
            raise ValueError("Requested market does not exist")
        if not market.config.unlocked or market.active is False:
            raise ValueError("Requested market is locked or inactive")
        if (
            market.reduce_only
            and not request.reduce_only
            and request.effective_time_in_force in (TimeInForce.GTC, TimeInForce.GTT)
        ):
            raise ValueError("Requested market only permits reduce-only resting orders")
        size_steps = market.config.quantity_to_steps(request.quantity)
        price_ticks = (
            0
            if request.order_type == OrderType.MARKET
            else market.config.price_to_ticks(request.price)
        )
        packed = pack_order(
            market_id=request.market_id,
            size_steps=size_steps,
            price_ticks=price_ticks,
            side=int(request.side),
            post_only=request.post_only,
            reduce_only=request.reduce_only,
            stp_mode=int(request.stp_mode),
            order_type=int(request.order_type),
            time_in_force=int(request.effective_time_in_force),
        )
        client_id = (
            request.client_order_id
            if request.client_order_id is not None
            else secrets.randbelow(2**64 - 1) + 1
        )
        action_hash = place_action_hash(
            packed,
            builder_id=request.builder_id,
            builder_fee_bps=request.builder_fee_bps,
            client_order_id=client_id,
            ttl_units=request.ttl_units,
        )
        async with self._nonces.operation_lock:
            nonce = await self._nonces.reserve()
            permit = await self._permit(action_hash, nonce)
            context = MutationContext(
                "place_order",
                account,
                nonce.anchor,
                nonce.bitmap_index,
                market_id=request.market_id,
                client_order_id=client_id,
            )
            return await self._submit(
                "/v1/orders/place",
                {
                    "market_id": request.market_id,
                    "size_steps": size_steps,
                    "price_ticks": price_ticks,
                    "side": int(request.side),
                    "post_only": request.post_only,
                    "reduce_only": request.reduce_only,
                    "stp_mode": int(request.stp_mode),
                    "order_type": int(request.order_type),
                    "time_in_force": int(request.effective_time_in_force),
                    "builder_id": request.builder_id,
                    "builder_fee_bps": request.builder_fee_bps,
                    "client_order_id": str(client_id),
                    "ttl_units": request.ttl_units,
                    "permit": permit,
                    "no_retry": True,
                },
                context,
                OrderSubmission,
            )

    async def cancel_order(
        self,
        order_id: str,
        *,
        market_id: int | None = None,
        resting_order_id: int | None = None,
    ) -> Cancellation:
        self._ensure_open()
        account, _ = self._credentials()
        validate_order_id(order_id)
        decoded = decode_resting_id(order_id)
        if (
            resting_order_id is not None
            and uint(resting_order_id, 64, "resting_order_id") != decoded
        ):
            raise ValueError("resting_order_id does not match the composite order ID")
        if market_id is None:
            order = await self.get_order(order_id)
            if order.sender != account:
                raise ValueError("Order belongs to a different account")
            market_id = order.market_id
        action_hash = cancel_action_hash(market_id, decoded)
        await self.initialize()
        async with self._nonces.operation_lock:
            nonce = await self._nonces.reserve()
            permit = await self._permit(action_hash, nonce)
            return await self._submit(
                "/v1/orders/cancel",
                {"market_id": market_id, "order_id": order_id, "permit": permit, "no_retry": True},
                MutationContext(
                    "cancel_order",
                    account,
                    nonce.anchor,
                    nonce.bitmap_index,
                    market_id=market_id,
                    order_id=order_id,
                ),
                Cancellation,
            )

    async def cancel_all_orders(self, market_id: int) -> Cancellation:
        self._ensure_open()
        account, _ = self._credentials()
        action_hash = cancel_all_action_hash(market_id)
        await self.initialize()
        async with self._nonces.operation_lock:
            nonce = await self._nonces.reserve()
            permit = await self._permit(action_hash, nonce)
            return await self._submit(
                "/v1/orders/cancel-all",
                {"market_id": market_id, "permit": permit},
                MutationContext(
                    "cancel_all_orders",
                    account,
                    nonce.anchor,
                    nonce.bitmap_index,
                    market_id=market_id,
                ),
                Cancellation,
            )

    async def wait_for_order(
        self,
        order_id: str,
        *,
        timeout: float = 30.0,  # noqa: ASYNC109 -- implemented with asyncio.timeout below
        poll_interval: float = 0.5,
    ) -> Order:
        validate_order_id(order_id)
        for name, value in (("timeout", timeout), ("poll_interval", poll_interval)):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        try:
            async with asyncio.timeout(timeout):
                while True:
                    try:
                        order = await self.get_order(order_id)
                        if order.terminal:
                            return order
                    except APIError as exc:
                        if exc.status_code != 404:
                            raise
                    await asyncio.sleep(poll_interval)
        except TimeoutError as exc:
            raise OrderWaitTimeoutError(order_id) from exc

    async def reconcile_submission(
        self, error: UnknownOutcomeError, *, max_pages: int = 100
    ) -> SubmissionResolution:
        """Query identifiers; an absent order or unused nonce is not proof of failure."""
        self._ensure_open()
        context = error.context
        selected = self._account_address()
        if address(context.account) != selected:
            raise ValueError("Submission belongs to a different account")
        consumed = None
        if context.nonce_anchor is not None and context.nonce_bitmap_index is not None:
            state = await self.get_nonce_state()
            consumed = (
                bool((state.bitmap >> context.nonce_bitmap_index) & 1)
                if state.nonce_anchor == context.nonce_anchor
                else False
                if state.nonce_anchor < context.nonce_anchor
                else None
            )
        found: list[Order] = []
        if context.operation == "place_order" and context.client_order_id:
            async for order in self.iter_order_history(
                market_id=context.market_id, max_pages=max_pages
            ):
                if order.client_order_id == context.client_order_id:
                    found.append(order)
        elif context.order_id is not None:
            try:
                order = await self.get_order(context.order_id, market_id=context.market_id)
                if order.sender != selected:
                    raise ProtocolError("Reconciliation returned another account's order")
                found.append(order)
            except APIError as exc:
                if exc.status_code != 404:
                    raise
        resolved = len(found) == 1 and (
            context.operation == "place_order" or found[0].status == "ORDER_STATUS_CANCELLED"
        )
        return SubmissionResolution(orders=tuple(found), nonce_consumed=consumed, resolved=resolved)

    async def get_account_snapshot(
        self,
        *,
        account: str | None = None,
        max_pages: int = 1000,
        include_conditional_orders: bool = False,
    ) -> AccountSnapshot:
        """Fresh REST views for recovery; separate calls are not an atomic exchange snapshot."""
        selected = self._account_address(account)
        if type(include_conditional_orders) is not bool:
            raise TypeError("include_conditional_orders must be bool")

        async def positions() -> tuple[Position, ...]:
            return tuple(
                [row async for row in self.iter_positions(account=selected, max_pages=max_pages)]
            )

        async def orders() -> tuple[OpenOrder, ...]:
            return tuple(
                [row async for row in self.iter_open_orders(account=selected, max_pages=max_pages)]
            )

        async def conditionals() -> tuple[TpslOrder, ...] | None:
            if not include_conditional_orders:
                return None
            return tuple(
                [
                    row
                    async for row in self.iter_tpsl_orders(
                        account=selected,
                        max_pages=max_pages,
                        statuses=("TPSL_ORDER_STATUS_ACCEPTED", "TPSL_ORDER_STATUS_TRIGGERED"),
                    )
                ]
            )

        (balances, conditional_rows), (position_rows, order_rows) = await _pair(
            _pair(self.get_balances(account=selected), conditionals()), _pair(positions(), orders())
        )
        return AccountSnapshot(
            balances=balances,
            positions=position_rows,
            open_orders=order_rows,
            conditional_orders=conditional_rows,
        )

    async def _websocket_auth(self) -> dict[str, Any]:
        account, signer = self._credentials()
        metadata = await self.initialize()
        response = await self._rest.get("/v1/auth/nonce")
        nonce = response.get("nonce")
        if not isinstance(nonce, str):
            raise AuthenticationError("Server omitted its one-time authentication nonce")
        return await auth.websocket_auth(
            metadata, account=account, signer=signer, server_nonce=nonce
        )

    def stream(self, channel: Channel, *, market_ids: Sequence[int] = ()) -> RiseXStream:
        self._ensure_open()
        private = channel in ("orders", "positions", "fills")
        if private:
            self._credentials()
        stream = RiseXStream(
            self.config,
            channel,
            market_ids=market_ids,
            on_close=self._streams.discard,
            authenticator=self._websocket_auth if private else None,
            account=self._account if private else None,
            request_limiter=self._websocket_limiter,
        )
        self._streams.add(stream)
        return stream

    def stream_orderbook(self, *, market_ids: Sequence[int] = ()) -> RiseXStream:
        return self.stream("orderbook", market_ids=market_ids)

    def stream_trades(self, *, market_ids: Sequence[int] = ()) -> RiseXStream:
        return self.stream("trades", market_ids=market_ids)

    def stream_oracle(self, *, market_ids: Sequence[int] = ()) -> RiseXStream:
        return self.stream("oracle", market_ids=market_ids)

    def stream_orders(self, *, market_ids: Sequence[int] = ()) -> RiseXStream:
        return self.stream("orders", market_ids=market_ids)

    def stream_positions(self, *, market_ids: Sequence[int] = ()) -> RiseXStream:
        return self.stream("positions", market_ids=market_ids)

    def stream_fills(self, *, market_ids: Sequence[int] = ()) -> RiseXStream:
        return self.stream("fills", market_ids=market_ids)
