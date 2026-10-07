"""Permit and WebSocket authentication payloads using an injected signer."""

from __future__ import annotations

import re
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data

from .exceptions import AuthenticationError
from .models import ProtocolMetadata
from .nonce import Nonce
from .signing import (
    Signer,
    address,
    compact_signature,
    signature_hex,
    typed_data,
    uint,
)


async def sign(signer: Signer, data: dict[str, Any]) -> bytes:
    signature = await signer.sign_typed_data(data)
    signature_hex(signature)  # Validates encoding/canonicality before any submission.
    recovered = Account.recover_message(encode_typed_data(full_message=data), signature=signature)
    if address(recovered) != address(signer.address):
        raise AuthenticationError("Injected signer returned a signature from a different address")
    return signature


async def login_payload(
    metadata: ProtocolMetadata, *, account: str, signer: Signer, nonce: str, deadline: int
) -> dict[str, Any]:
    """Sign the nonce as uint256; retain its original hex encoding on the wire."""
    normalized = nonce.removeprefix("0x")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", normalized):
        raise AuthenticationError("Login nonce must be a 32-byte hex string")
    uint(deadline, 32, "deadline")
    selected = address(account)
    signature = await sign(
        signer,
        typed_data(
            metadata.domain.signing_values(),
            "Login",
            {"account": selected, "nonce": int(normalized, 16), "deadline": deadline},
        ),
    )
    payload = {
        "account": selected,
        "nonce": nonce,
        "deadline": deadline,
        "signature": signature_hex(signature),
    }
    if address(signer.address) != selected:
        payload["signer"] = address(signer.address)
    return payload


async def permit(
    metadata: ProtocolMetadata,
    *,
    account: str,
    signer: Signer,
    action_hash: bytes,
    nonce: Nonce,
    deadline: int,
) -> dict[str, Any]:
    uint(deadline, 32, "deadline")
    message = {
        "account": address(account),
        "target": metadata.system.addresses.router,
        "hash": action_hash,
        "nonceAnchor": nonce.anchor,
        "nonceBitmap": nonce.bitmap_index,
        "deadline": deadline,
    }
    signature = await sign(
        signer, typed_data(metadata.domain.signing_values(), "VerifyWitness", message)
    )
    return {
        "account": address(account),
        "signer": address(signer.address),
        "nonce_anchor": str(nonce.anchor),
        "nonce_bitmap_index": nonce.bitmap_index,
        "deadline": deadline,
        "signature": compact_signature(signature),
    }


async def websocket_auth(
    metadata: ProtocolMetadata,
    *,
    account: str,
    signer: Signer,
    server_nonce: str,
) -> dict[str, Any]:
    normalized = server_nonce.removeprefix("0x").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise AuthenticationError("Server auth nonce must be a 32-byte hex string")
    message = "WebSocket Authentication"
    data = typed_data(
        metadata.domain.signing_values(),
        "RegisterV2",
        {"signer": address(signer.address), "message": message, "nonce": int(normalized, 16)},
    )
    signature = await sign(signer, data)
    return {
        "method": "auth_v2",
        "params": {
            "account": address(account),
            "signer": address(signer.address),
            "message": message,
            "nonce": normalized,
            "signature": signature_hex(signature),
        },
    }
