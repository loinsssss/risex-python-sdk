"""Provider-native wire models with exact numeric fields."""

from __future__ import annotations

from decimal import Decimal
from enum import IntEnum
from typing import Annotated, Literal, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .exceptions import PrecisionError
from .signing import address
from .units import to_steps

FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]
NonnegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
MarketID = Annotated[int, Field(gt=0, lt=2**64)]


class WireModel(BaseModel):
    # Preserve unknown provider fields so additive changes don't lose information.
    model_config = ConfigDict(extra="allow", frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def reject_wire_floats(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(value, float):
            raise ValueError("RISEx numeric fields must use exact strings or integers")
        if isinstance(value, bool) and info.field_name is not None:
            annotation = cls.model_fields[info.field_name].annotation
            if annotation is not bool and bool not in get_args(annotation):
                raise ValueError("Booleans cannot represent RISEx numeric or string fields")
        return value


class MarketConfig(WireModel):
    name: str
    quote: str
    step_size: PositiveDecimal
    step_price: PositiveDecimal
    min_order_size: NonnegativeDecimal
    unlocked: bool
    max_leverage: NonnegativeDecimal | None = None
    maintenance_margin_factor: NonnegativeDecimal | None = None
    open_interest_limit: NonnegativeDecimal | None = None

    def quantity_to_steps(self, quantity: Decimal) -> int:
        steps = to_steps(quantity, self.step_size)
        if quantity <= 0 or quantity < self.min_order_size:
            raise PrecisionError(f"quantity must be positive and at least {self.min_order_size}")
        return steps

    def price_to_ticks(self, price: Decimal) -> int:
        ticks = to_steps(price, self.step_price)
        if price <= 0:
            raise PrecisionError("price must be positive")
        return ticks


class Market(WireModel):
    market_id: MarketID
    config: MarketConfig
    base_asset_symbol: str = ""
    quote_asset_symbol: str = ""
    display_name: str = ""
    underlying: str = ""
    last_price: NonnegativeDecimal | None = None
    mark_price: NonnegativeDecimal | None = None
    index_price: NonnegativeDecimal | None = None
    high_24h: NonnegativeDecimal | None = None
    low_24h: NonnegativeDecimal | None = None
    change_24h: FiniteDecimal | None = None
    quote_volume_24h: NonnegativeDecimal | None = None
    open_interest: NonnegativeDecimal | None = None
    max_position_size: NonnegativeDecimal | None = None
    accumulated_funding: FiniteDecimal | None = None
    current_funding_rate: FiniteDecimal | None = None
    funding_rate_8h: FiniteDecimal | None = None
    funding_interval: int | None = None
    next_funding_time: int | None = None
    active: bool | None = None
    post_only: bool | None = None
    reduce_only: bool | None = None


class MarketsResponse(WireModel):
    markets: tuple[Market, ...]
    cached_at: int | None = None


class PriceLevel(WireModel):
    price: PositiveDecimal
    quantity: NonnegativeDecimal
    order_count: Annotated[int, Field(ge=0)]


class OrderbookSnapshot(WireModel):
    market_id: MarketID
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    total_bids: Annotated[int, Field(ge=0)] | None = None
    total_asks: Annotated[int, Field(ge=0)] | None = None


class ChannelEvent(WireModel):
    type: Literal["snapshot", "update"]
    worker_timestamp: Annotated[int, Field(ge=0)]
    market_id: MarketID | None = None
    block_number: Annotated[int, Field(ge=0)] | None = None
    log_index: Annotated[int, Field(ge=0)] | None = None
    tx_hash: str | None = None


class OrderbookEvent(ChannelEvent):
    channel: Literal["orderbook"]
    market_id: MarketID
    data: OrderbookSnapshot
    checksum: Annotated[int, Field(ge=0, lt=2**32)] | None = None
    level_count: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def consistent_book(self) -> OrderbookEvent:
        if self.market_id != self.data.market_id:
            raise ValueError("orderbook envelope and payload market IDs disagree")
        if self.type == "update" and self.checksum is None:
            raise ValueError("orderbook update requires a checksum")
        return self


class Trade(WireModel):
    id: str
    maker_order_id: str
    taker_order_id: str
    maker: str
    taker: str
    maker_side: Literal[0, 1]
    price: PositiveDecimal
    size: PositiveDecimal
    fee_maker: FiniteDecimal
    fee_taker: FiniteDecimal
    fee_liquidation: FiniteDecimal


class TradeEvent(ChannelEvent):
    channel: Literal["trades"]
    type: Literal["update"]
    market_id: MarketID
    data: Trade


class OraclePrice(WireModel):
    # Keep protocol wei explicit; convert through from_wei at the application boundary.
    index_price: Annotated[int, Field(ge=0)] | None = None
    mark_price: Annotated[int, Field(ge=0)] | None = None


class OracleData(WireModel):
    prices: dict[MarketID, OraclePrice]
    timestamp: Annotated[int, Field(ge=0)]


class OracleEvent(ChannelEvent):
    channel: Literal["oracle"]
    type: Literal["update"]
    market_id: None = None
    data: OracleData


class ConnectionEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    state: Literal["connected", "disconnected", "reconnecting"]
    stale: bool
    attempt: int = 0


EthereumAddress = Annotated[str, AfterValidator(address)]
UInt64 = Annotated[int, Field(ge=0, lt=2**64)]


class SigningDomain(WireModel):
    name: str
    version: str
    chain_id: Annotated[int, Field(gt=0, lt=2**256)]
    verifying_contract: EthereumAddress

    def signing_values(self) -> dict[str, str | int]:
        return {
            "name": self.name,
            "version": self.version,
            "chainId": self.chain_id,
            "verifyingContract": self.verifying_contract,
        }


class ContractAddresses(WireModel):
    router: EthereumAddress
    auth: EthereumAddress
    usdc: EthereumAddress


class ChainInfo(WireModel):
    chain_id: Annotated[int, Field(gt=0)]
    name: str = ""


class SystemConfig(WireModel):
    addresses: ContractAddresses
    chain: ChainInfo
    is_maintenance_mode: bool = False


class ProtocolMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)
    domain: SigningDomain
    system: SystemConfig


class NonceState(WireModel):
    nonce_anchor: Annotated[int, Field(ge=0, lt=2**48)]
    current_bitmap_index: Annotated[int, Field(ge=0, le=208)]
    bitmap: Annotated[int, Field(ge=0, lt=2**256)]

    @field_validator("bitmap", mode="before")
    @classmethod
    def parse_bitmap(cls, value: object) -> object:
        if isinstance(value, str):
            return int(value, 16 if value.startswith("0x") else 10)
        return value


class SessionKeyStatus(WireModel):
    status: Annotated[int, Field(ge=0)]
    status_description: str = ""

    @property
    def active(self) -> bool:
        return self.status == 1


class SignerRegistration(WireModel):
    success: bool
    transaction_hash: str = ""
    status: int | None = None
    block_number: int | None = None


class Balance(WireModel):
    # The deployed API formats on-chain balances as human-unit decimal strings.
    balance: FiniteDecimal


class Balances(BaseModel):
    model_config = ConfigDict(frozen=True)
    account: EthereumAddress
    token: EthereumAddress
    collateral: Balance
    cross_margin: Balance


class AccountPosition(WireModel):
    size: FiniteDecimal
    quote_amount: FiniteDecimal
    last_funding_payment: FiniteDecimal
    margin_mode: Literal[0, 1]
    side: Literal[0, 1]
    isolated_usdc_balance: NonnegativeDecimal
    # The direct chain read reports 0 for an absent/flat position.
    market_id: Annotated[int, Field(ge=0)]
    avg_entry_price: NonnegativeDecimal | None = None
    mark_price: NonnegativeDecimal | None = None
    index_price: NonnegativeDecimal | None = None
    leverage: NonnegativeDecimal | None = None
    unrealized_pnl: FiniteDecimal | None = None
    liquidation_price: NonnegativeDecimal | None = None
    margin_balance: FiniteDecimal | None = None
    initial_margin_requirement: NonnegativeDecimal | None = None
    maintenance_margin_requirement: NonnegativeDecimal | None = None
    quote_balance: FiniteDecimal | None = None
    free_isolated_usdc_balance: FiniteDecimal | None = None
    adl_price: NonnegativeDecimal | None = None

    @field_validator(
        "avg_entry_price",
        "mark_price",
        "index_price",
        "leverage",
        "unrealized_pnl",
        "liquidation_price",
        "margin_balance",
        "initial_margin_requirement",
        "maintenance_margin_requirement",
        "quote_balance",
        "free_isolated_usdc_balance",
        "adl_price",
        mode="before",
    )
    @classmethod
    def empty_number(cls, value: object) -> object:
        return None if value == "" else value


class PositionResponse(WireModel):
    position: AccountPosition


class Position(WireModel):
    account: EthereumAddress
    market_id: MarketID
    size: FiniteDecimal
    quote_amount: FiniteDecimal
    side: Literal["BUY", "SELL"]
    margin_mode: Literal[0, 1]
    isolated_usdc_balance: NonnegativeDecimal
    last_funding_payment: FiniteDecimal
    unsettled_funding: FiniteDecimal | None = None
    leverage: NonnegativeDecimal
    avg_entry_price: NonnegativeDecimal
    block_number: int | None = None
    log_index: int | None = None
    worker_timestamp: int | None = None


class PositionsResponse(WireModel):
    positions: tuple[Position, ...]
    total_count: Annotated[int, Field(ge=0)]
    page: Annotated[int, Field(ge=1)]
    page_size: Annotated[int, Field(ge=1)]
    has_next_page: bool


class OrderSide(IntEnum):
    BUY = 0
    SELL = 1


class OrderType(IntEnum):
    MARKET = 0
    LIMIT = 1


class TimeInForce(IntEnum):
    GTC = 0
    GTT = 1
    FOK = 2
    IOC = 3


class STPMode(IntEnum):
    EXPIRE_MAKER = 0
    EXPIRE_TAKER = 1
    EXPIRE_BOTH = 2


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    market_id: Annotated[int, Field(gt=0, lt=2**16, strict=True)]
    side: OrderSide
    quantity: PositiveDecimal
    # For market orders this is the worst acceptable execution price.
    price: PositiveDecimal
    order_type: OrderType = OrderType.LIMIT
    time_in_force: TimeInForce | None = None
    post_only: Annotated[bool, Field(strict=True)] = False
    reduce_only: Annotated[bool, Field(strict=True)] = False
    stp_mode: STPMode = STPMode.EXPIRE_MAKER
    client_order_id: Annotated[int, Field(ge=0, lt=2**64, strict=True)] | None = None
    builder_id: Annotated[int, Field(ge=0, lt=2**16, strict=True)] = 0
    builder_fee_bps: Annotated[int, Field(ge=0, lt=2**16, strict=True)] = 0
    ttl_units: Annotated[int, Field(ge=0, lt=2**16, strict=True)] = 0

    @field_validator("quantity", "price", mode="before")
    @classmethod
    def require_decimal(cls, value: object) -> object:
        if not isinstance(value, Decimal):
            raise ValueError("quantity and price must be Decimal")
        return value

    @field_validator("side", "order_type", "time_in_force", "stp_mode", mode="before")
    @classmethod
    def reject_bool_enum(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("enum fields must not be bool")
        return value

    @property
    def effective_time_in_force(self) -> TimeInForce:
        if self.time_in_force is not None:
            return self.time_in_force
        return TimeInForce.IOC if self.order_type == OrderType.MARKET else TimeInForce.GTC

    @model_validator(mode="after")
    def validate_execution(self) -> OrderRequest:
        tif = self.effective_time_in_force
        if self.order_type == OrderType.MARKET:
            if tif not in (TimeInForce.FOK, TimeInForce.IOC) or self.post_only:
                raise ValueError("market orders require FOK/IOC and cannot be post_only")
        elif self.post_only and tif in (TimeInForce.FOK, TimeInForce.IOC):
            raise ValueError("post_only requires a resting GTC/GTT order")
        if (tif == TimeInForce.GTT) != bool(self.ttl_units):
            raise ValueError("ttl_units must be nonzero exactly when time_in_force is GTT")
        if self.builder_fee_bps and not self.builder_id:
            raise ValueError("builder_fee_bps requires builder_id")
        return self


class OpenOrder(WireModel):
    order_id: str
    market_id: MarketID
    account: EthereumAddress
    resting_order_id: UInt64
    wide_order_id: UInt64
    side: OrderSide
    size_steps: Annotated[int, Field(ge=0, lt=2**32)]
    price_ticks: Annotated[int, Field(ge=0, lt=2**24)]
    order_type: OrderType
    time_in_force: TimeInForce
    post_only: bool
    reduce_only: bool
    client_order_id: UInt64 | None = None

    @field_validator("client_order_id", mode="before")
    @classmethod
    def empty_client_id(cls, value: object) -> object:
        return None if value == "" else value


class OpenOrdersResponse(WireModel):
    orders: tuple[OpenOrder, ...]
    market_id: Annotated[int, Field(ge=0)]
    account: EthereumAddress
    total_orders: Annotated[int, Field(ge=0)]


OrderStatus = Literal[
    "ORDER_STATUS_NONE", "ORDER_STATUS_OPEN", "ORDER_STATUS_FILLED", "ORDER_STATUS_CANCELLED"
]


class Order(WireModel):
    id: str
    market_id: MarketID
    sender: EthereumAddress
    side: Literal["BUY", "SELL"]
    type: Literal["LIMIT", "MARKET"]
    time_in_force: Literal["GTC", "GTT", "FOK", "IOC"]
    status: OrderStatus
    price: NonnegativeDecimal
    size: NonnegativeDecimal
    filled_size: NonnegativeDecimal
    avg_price: NonnegativeDecimal
    post_only: bool
    reduce_only: bool
    resting_order_id: UInt64 | None = None
    wide_order_id: UInt64 | None = None
    client_order_id: UInt64 | None = None
    created_at: int | None = None
    block_number: int | None = None
    log_index: int | None = None
    tx_hash: str | None = None
    cancel_requested: bool = False
    fee_bps: Annotated[int, Field(ge=0)] | None = None
    stop_price: NonnegativeDecimal | None = None

    @field_validator("client_order_id", "stop_price", mode="before")
    @classmethod
    def empty_optional_number(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def order_id(self) -> str:
        return self.id

    @property
    def terminal(self) -> bool:
        return self.status in ("ORDER_STATUS_FILLED", "ORDER_STATUS_CANCELLED")


class OrderResponse(WireModel):
    order: Order


class OrderHistory(WireModel):
    orders: tuple[Order, ...]
    page: Annotated[int, Field(ge=1)]
    has_next_page: bool


class BlockchainData(WireModel):
    block_number: int
    log_index: int
    tx_hash: str


class Fill(WireModel):
    id: str
    market_id: MarketID
    order_id: str
    side: Literal["BUY", "SELL"]
    price: NonnegativeDecimal
    size: NonnegativeDecimal
    fee: FiniteDecimal
    time: Annotated[int, Field(ge=0)]
    liquidity_indicator: Literal["MAKER", "TAKER"]
    client_order_id: UInt64 | None = None
    realized_pnl: FiniteDecimal | None = None
    realized_pnl_percentage: FiniteDecimal | None = None
    avg_price: NonnegativeDecimal | None = None
    leverage: NonnegativeDecimal | None = None
    position_side: Literal["BUY", "SELL", ""] | None = None
    margin_mode: Literal[0, 1] | None = None
    blockchain_data: BlockchainData | None = None
    is_liquidation: bool = False
    is_otc: bool = False

    @field_validator(
        "client_order_id",
        "realized_pnl",
        "realized_pnl_percentage",
        "avg_price",
        "leverage",
        mode="before",
    )
    @classmethod
    def empty_optional_number(cls, value: object) -> object:
        return None if value == "" else value


class TradeHistory(WireModel):
    trades: tuple[Fill, ...]
    page: Annotated[int, Field(ge=1)]
    has_next_page: bool
    market_id: Annotated[int, Field(ge=0)]
    wallet_address: EthereumAddress


class FundingRateRecord(WireModel):
    """One settled funding period. Rates are per period (the market's funding interval)."""

    funding_rate: FiniteDecimal
    accumulated_funding: FiniteDecimal
    index_price: NonnegativeDecimal
    start_time: Annotated[int, Field(ge=0)]
    end_time: Annotated[int, Field(ge=0)]
    block_number: Annotated[int, Field(ge=0)] | None = None
    block_time: Annotated[int, Field(ge=0)] | None = None
    tx_hash: str | None = None


class FundingRateHistory(WireModel):
    """Settlements newest first. Timestamps are Unix nanoseconds."""

    market_id: MarketID
    records: tuple[FundingRateRecord, ...]
    page: Annotated[int, Field(ge=1)]
    has_next_page: bool


CandleInterval = Literal["1m", "5m", "15m", "1h", "4h", "1d", "1w"]


class Candle(WireModel):
    """OHLCV bucket starting at ``time`` (Unix nanoseconds), as RISEx reports it.

    RISEx sets ``open`` to the previous candle's ``close``, while ``high``/``low`` cover
    only trades inside the bucket, so ``open`` can sit outside ``[low, high]``. Buckets
    without trades are omitted rather than returned empty.
    """

    market_id: MarketID
    interval: CandleInterval
    time: Annotated[int, Field(ge=0)]
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: NonnegativeDecimal


class CandlesResponse(WireModel):
    data: tuple[Candle, ...]


class OrderSubmission(WireModel):
    order_id: str
    tx_hash: str
    block_number: int
    sc_order_id: UInt64
    filled_quantity_wei: Annotated[int, Field(ge=0)] | None = Field(
        default=None, validation_alias="filled_quantity"
    )
    filled_percent: NonnegativeDecimal | None = None
    message: str = ""

    @field_validator("filled_quantity_wei", "filled_percent", mode="before")
    @classmethod
    def empty_fill(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def filled_quantity(self) -> Decimal | None:
        from .units import from_wei

        return from_wei(self.filled_quantity_wei) if self.filled_quantity_wei is not None else None


class Cancellation(WireModel):
    success: bool
    tx_hash: str
    block_number: int


class OrdersEvent(ChannelEvent):
    channel: Literal["orders"]
    data: tuple[Order, ...]


class PositionsEvent(ChannelEvent):
    channel: Literal["positions"]
    data: tuple[Position, ...]


class FillsEvent(ChannelEvent):
    channel: Literal["fills"]
    type: Literal["update"]
    market_id: MarketID
    data: Fill


class AccountSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    balances: Balances
    positions: tuple[Position, ...]
    open_orders: tuple[OpenOrder, ...]


class SubmissionResolution(BaseModel):
    model_config = ConfigDict(frozen=True)
    orders: tuple[Order, ...]
    nonce_consumed: bool | None
    # True only when identifiers located the order; absence is never proof of failure.
    resolved: bool


PublicChannel = Literal["orderbook", "trades", "oracle"]
PrivateChannel = Literal["orders", "positions", "fills"]
Channel = PublicChannel | PrivateChannel
PublicDataEvent = OrderbookEvent | TradeEvent | OracleEvent
PrivateDataEvent = OrdersEvent | PositionsEvent | FillsEvent
DataEvent = PublicDataEvent | PrivateDataEvent
PublicStreamEvent = PublicDataEvent | ConnectionEvent
StreamEvent = DataEvent | ConnectionEvent
