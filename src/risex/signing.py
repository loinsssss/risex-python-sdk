"""Exact RISEx EIP-712 structs and action hashes, independent of HTTP."""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from eth_abi.abi import encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils.address import is_address, to_checksum_address
from eth_utils.crypto import keccak

DOMAIN_FIELDS = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]

STRUCTS = {
    "CancelAllTpslOrders": [
        {"name": "account", "type": "address"},
        {"name": "marketId", "type": "uint64"},
        {"name": "deadline", "type": "uint32"},
    ],
    "Login": [
        {"name": "account", "type": "address"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint32"},
    ],
    "VerifyWitness": [
        {"name": "account", "type": "address"},
        {"name": "target", "type": "address"},
        {"name": "hash", "type": "bytes32"},
        {"name": "nonceAnchor", "type": "uint48"},
        {"name": "nonceBitmap", "type": "uint8"},
        {"name": "deadline", "type": "uint32"},
    ],
    "RegisterSigner": [
        {"name": "account", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "message", "type": "string"},
        {"name": "expiration", "type": "uint32"},
        {"name": "nonceAnchor", "type": "uint48"},
        {"name": "nonceBitmap", "type": "uint8"},
    ],
    "VerifySigner": [
        {"name": "account", "type": "address"},
        {"name": "nonceAnchor", "type": "uint48"},
        {"name": "nonceBitmap", "type": "uint8"},
    ],
    "RevokeSigner": [
        {"name": "account", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "nonceAnchor", "type": "uint48"},
        {"name": "nonceBitmap", "type": "uint8"},
    ],
    "RegisterV2": [
        {"name": "signer", "type": "address"},
        {"name": "message", "type": "string"},
        {"name": "nonce", "type": "uint256"},
    ],
}


def address(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or not is_address(value):
        raise ValueError("Expected a 0x-prefixed 20-byte Ethereum address")
    return str(to_checksum_address(value))


def uint(value: int, bits: int, name: str) -> int:
    if type(value) is not int or not 0 <= value < 2**bits:
        raise ValueError(f"{name} must be a uint{bits} integer")
    return value


def order_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{48}", value):
        raise ValueError("order_id must be a 0x-prefixed 24-byte composite order ID")
    return value


def resting_order_id(value: str) -> int:
    """Composite IDs encode WideOrderID in their first eight bytes."""
    return int.from_bytes(bytes.fromhex(order_id(value)[2:])[:8], "big") >> 1


def typed_data(
    domain: Mapping[str, Any], primary_type: str, message: Mapping[str, Any]
) -> dict[str, Any]:
    fields = STRUCTS[primary_type]
    return {
        "types": {"EIP712Domain": DOMAIN_FIELDS, primary_type: fields},
        "primaryType": primary_type,
        "domain": dict(domain),
        "message": dict(message),
    }


def signature_hex(signature: bytes) -> str:
    _validate_signature(signature)
    return "0x" + signature.hex()


def _validate_signature(signature: bytes) -> None:
    if not isinstance(signature, bytes) or len(signature) != 65 or signature[-1] not in (27, 28):
        raise ValueError("Signer must return 65-byte r+s+v with v=27 or v=28")
    # Compact encoding requires the canonical lower-half s used by Ethereum signers.
    curve_order = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:64], "big")
    if not 0 < r < curve_order or not 0 < s <= curve_order // 2:
        raise ValueError("Signer returned a noncanonical signature")


def compact_signature(signature: bytes) -> str:
    _validate_signature(signature)
    s = int.from_bytes(signature[32:64], "big")
    if signature[-1] == 28:
        s |= 1 << 255
    return base64.b64encode(signature[:32] + s.to_bytes(32, "big")).decode("ascii")


@runtime_checkable
class Signer(Protocol):
    @property
    def address(self) -> str: ...

    async def sign_typed_data(self, data: Mapping[str, Any]) -> bytes: ...


class LocalSigner:
    """A local key signer. Its repr contains only its public address."""

    def __init__(self, private_key: str | bytes) -> None:
        try:
            self._account = Account.from_key(private_key)
        except (TypeError, ValueError):
            raise ValueError("Invalid private key") from None

    @property
    def address(self) -> str:
        return str(self._account.address)

    def __repr__(self) -> str:
        return f"LocalSigner(address={self.address!r})"

    async def sign_typed_data(self, data: Mapping[str, Any]) -> bytes:
        signed = self._account.sign_message(encode_typed_data(full_message=dict(data)))
        return bytes(signed.signature)


def pack_order(
    *,
    market_id: int,
    size_steps: int,
    price_ticks: int,
    side: int,
    post_only: bool,
    reduce_only: bool,
    stp_mode: int,
    order_type: int,
    time_in_force: int,
) -> int:
    uint(market_id, 16, "market_id")
    uint(size_steps, 32, "size_steps")
    uint(price_ticks, 24, "price_ticks")
    uint(side, 1, "side")
    uint(stp_mode, 2, "stp_mode")
    uint(order_type, 1, "order_type")
    uint(time_in_force, 2, "time_in_force")
    if market_id == 0 or size_steps == 0 or stp_mode == 3:
        raise ValueError("Order requires positive market and size, and a supported STP mode")
    if type(post_only) is not bool or type(reduce_only) is not bool:
        raise TypeError("Order flags must be bool")
    if order_type == 0:
        if price_ticks != 0:
            raise ValueError(
                "Market orders require zero price_ticks; use LIMIT IOC/FOK for a bound"
            )
        if time_in_force not in (2, 3) or post_only:
            raise ValueError("Market orders require FOK/IOC and cannot be post_only")
    elif price_ticks == 0:
        raise ValueError("Limit orders require positive price_ticks")
    flags = (
        side
        | (int(post_only) << 1)
        | (int(reduce_only) << 2)
        | (stp_mode << 3)
        | (order_type << 5)
        | (time_in_force << 6)
    )
    return (market_id << 70) | (size_steps << 38) | (price_ticks << 14) | (flags << 6) | (1 << 1)


def place_action_hash(
    order_data: int,
    *,
    builder_id: int = 0,
    builder_fee_bps: int = 0,
    client_order_id: int = 0,
    ttl_units: int = 0,
) -> bytes:
    uint(order_data, 88, "order_data")
    uint(builder_id, 16, "builder_id")
    uint(builder_fee_bps, 16, "builder_fee_bps")
    uint(client_order_id, 64, "client_order_id")
    uint(ttl_units, 16, "ttl_units")
    flags = (
        0x01
        | (0x02 if builder_id else 0)
        | (0x04 if client_order_id else 0)
        | (0x10 if ttl_units else 0)
    )
    types = ["bytes32", "uint8", "uint88", "uint16"]
    values: list[Any] = [keccak(text="RISE_PERPS_PLACE_ORDER_V1"), flags, order_data, builder_id]
    if builder_fee_bps:
        types.append("uint16")
        values.append(builder_fee_bps)
    types.extend(["uint64", "uint16"])
    values.extend([client_order_id, ttl_units])
    return bytes(keccak(encode(types, values)))


def cancel_action_hash(market_id: int, resting_id: int) -> bytes:
    uint(market_id, 16, "market_id")
    uint(resting_id, 64, "resting_order_id")
    if market_id == 0:
        raise ValueError("market_id must be positive")
    return bytes(
        keccak(
            encode(
                ["bytes32", "uint256", "uint256"],
                [keccak(text="RISE_PERPS_CANCEL_ORDER_V1"), market_id, resting_id],
            )
        )
    )


def cancel_all_action_hash(market_id: int) -> bytes:
    uint(market_id, 16, "market_id")
    if market_id == 0:
        raise ValueError("market_id must be positive")
    return bytes(
        keccak(
            encode(
                ["bytes32", "uint256"],
                [keccak(text="RISE_PERPS_CANCEL_ALL_ORDERS_V1"), market_id],
            )
        )
    )


def account_setting_action_hash(market_id: int, value: int, *, setting: str) -> bytes:
    """Exact account-setting ABI; only the two documented uint8 settings."""
    uint(market_id, 16, "market_id")
    uint(value, 8, "value")
    if market_id == 0:
        raise ValueError("market_id must be positive")
    if setting == "leverage" and value > 0:
        selector = "RISE_PERPS_UPDATE_LEVERAGE_V1"
    elif setting == "margin_mode" and value in (0, 1):
        selector = "RISE_PERPS_UPDATE_MARGIN_MODE_V1"
    else:
        raise ValueError("Unsupported account setting or value")
    return bytes(
        keccak(encode(["bytes32", "uint16", "uint8"], [keccak(text=selector), market_id, value]))
    )
