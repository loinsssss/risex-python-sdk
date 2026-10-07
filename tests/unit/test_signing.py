import base64

import pytest
from eth_keys import keys
from eth_utils.crypto import keccak

from risex import AuthenticationError, LocalSigner
from risex.auth import sign
from risex.signing import (
    STRUCTS,
    cancel_action_hash,
    cancel_all_action_hash,
    compact_signature,
    pack_order,
    place_action_hash,
    resting_order_id,
    typed_data,
)


def word(value):
    return int(value).to_bytes(32, "big")


def golden_witness(domain, message):
    # Independent ABI-word digest, checked against the contract's published typehash.
    domain_hash = keccak(
        keccak(
            text=(
                "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
            )
        )
        + keccak(text=domain["name"])
        + keccak(text=domain["version"])
        + word(domain["chainId"])
        + word(int(domain["verifyingContract"], 16))
    )
    witness_hash = keccak(
        bytes.fromhex("055e6bcbf2ba5ff1c2ba5dc95b6648a5de6aaab3185251a34e3b88c11e116821")
        + word(int(message["account"], 16))
        + word(int(message["target"], 16))
        + message["hash"]
        + word(message["nonceAnchor"])
        + word(message["nonceBitmap"])
        + word(message["deadline"])
    )
    return keccak(b"\x19\x01" + domain_hash + witness_hash)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("RegisterSigner", "a526f63b3968e56ae1b177ce9b3dc29766e0891e6397a9c23cf8c53ee8fc8f62"),
        ("VerifySigner", "4d298dcceb691695f582cc337308236426a0c97201a31834625e8eadc44d4230"),
        ("RevokeSigner", "36db7f392f548b56f37d89469115d138685addf06be45684f9e5b0e8b5d28000"),
        ("VerifyWitness", "055e6bcbf2ba5ff1c2ba5dc95b6648a5de6aaab3185251a34e3b88c11e116821"),
    ],
)
def test_typehash_matches_authorization_contract(name, expected):
    fields = ",".join(f"{field['type']} {field['name']}" for field in STRUCTS[name])
    assert keccak(text=f"{name}({fields})").hex() == expected


async def test_signature_matches_independent_digest_and_key(backend, signer):
    domain = {
        "name": backend.domain["name"],
        "version": backend.domain["version"],
        "chainId": int(backend.domain["chain_id"]),
        "verifyingContract": backend.domain["verifying_contract"],
    }
    message = {
        "account": backend.account,
        "target": backend.system["addresses"]["router"],
        "hash": bytes.fromhex("42" * 32),
        "nonceAnchor": 1,
        "nonceBitmap": 207,
        "deadline": 2_000_000_000,
    }
    digest = golden_witness(domain, message)
    independent = keys.PrivateKey(bytes.fromhex("22" * 32)).sign_msg_hash(digest)
    signature = await sign(signer, typed_data(domain, "VerifyWitness", message))
    assert signature[:64] == independent.to_bytes()[:64]
    assert signature[64] == independent.v + 27
    compact = base64.b64decode(compact_signature(signature))
    assert len(compact) == 64
    s = int.from_bytes(compact[32:], "big")
    restored = compact[:32] + (s & ((1 << 255) - 1)).to_bytes(32, "big") + bytes([s >> 255])
    recovered = keys.Signature(restored).recover_public_key_from_msg_hash(digest)
    assert recovered.to_checksum_address() == signer.address
    # Domain/target mistakes must change recovery, rather than passing a self-consistent test.
    wrong = golden_witness(domain, {**message, "target": domain["verifyingContract"]})
    assert keys.Signature(restored).recover_public_key_from_msg_hash(wrong) != recovered


@pytest.mark.parametrize("v", [27, 28])
def test_compact_parity_bit(v):
    signature = word(1) + word(2) + bytes([v])
    compact = base64.b64decode(compact_signature(signature))
    assert int.from_bytes(compact[32:], "big") == 2 | ((v - 27) << 255)


def test_action_hash_word_layout_includes_optional_fee_correctly():
    packed = pack_order(
        market_id=1,
        size_steps=200,
        price_ticks=550248,
        side=0,
        post_only=True,
        reduce_only=False,
        stp_mode=0,
        order_type=1,
        time_in_force=0,
    )
    # Golden layout from the official integration guide, with version/reserved bits.
    assert packed == (1 << 70) + (200 << 38) + (550248 << 14) + (34 << 6) + 2
    selector = keccak(text="RISE_PERPS_PLACE_ORDER_V1")
    assert place_action_hash(packed) == keccak(
        selector + word(1) + word(packed) + word(0) + word(0) + word(0)
    )
    assert place_action_hash(
        packed, builder_id=7, builder_fee_bps=10, client_order_id=99, ttl_units=8
    ) == keccak(selector + word(0x17) + word(packed) + word(7) + word(10) + word(99) + word(8))


def test_cancel_commits_to_resting_id_not_composite_id():
    order_id = "0x00000000000058cb000000000162fec5000000000000026e"
    assert resting_order_id(order_id) == 11365
    assert cancel_action_hash(1, 11365) == keccak(
        keccak(text="RISE_PERPS_CANCEL_ORDER_V1") + word(1) + word(11365)
    )
    assert cancel_all_action_hash(1) == keccak(
        keccak(text="RISE_PERPS_CANCEL_ALL_ORDERS_V1") + word(1)
    )


@pytest.mark.parametrize("time_in_force", [2, 3])
def test_native_market_order_packing_uses_zero_price(time_in_force):
    args = dict(
        market_id=1,
        size_steps=29,
        price_ticks=0,
        side=1,
        post_only=False,
        reduce_only=True,
        stp_mode=0,
        order_type=0,
        time_in_force=time_in_force,
    )
    flags = 1 | (1 << 2) | (time_in_force << 6)
    assert pack_order(**args) == (1 << 70) | (29 << 38) | (flags << 6) | 2
    with pytest.raises(ValueError, match="zero price_ticks"):
        pack_order(**{**args, "price_ticks": 600000})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("market_id", 2**16),
        ("size_steps", 2**32),
        ("price_ticks", 2**24),
        ("stp_mode", 3),
        ("side", 2),
        ("market_id", 0),
        ("size_steps", 0),
        ("price_ticks", 0),
        ("order_type", True),
    ],
)
def test_packing_rejects_overflow_without_masking(field, value):
    values = dict(
        market_id=1,
        size_steps=1,
        price_ticks=1,
        side=0,
        post_only=False,
        reduce_only=False,
        stp_mode=0,
        order_type=1,
        time_in_force=0,
    )
    values[field] = value
    with pytest.raises((ValueError, TypeError)):
        pack_order(**values)


async def test_injected_signer_must_recover_its_claimed_address(backend, signer):
    class WrongSigner:
        address = backend.account

        async def sign_typed_data(self, data):
            return await signer.sign_typed_data(data)

    domain = {
        "name": "RISEx",
        "version": "1",
        "chainId": 4153,
        "verifyingContract": backend.domain["verifying_contract"],
    }
    data = typed_data(
        domain, "VerifySigner", {"account": backend.account, "nonceAnchor": 1, "nonceBitmap": 0}
    )
    with pytest.raises(AuthenticationError):
        await sign(WrongSigner(), data)


def test_key_repr_and_errors_do_not_print_private_key(signer):
    assert "22" * 32 not in repr(signer)
    with pytest.raises(ValueError, match="Invalid private key") as error:
        LocalSigner("invalid-secret-should-not-be-printed")
    assert "invalid-secret" not in str(error.value)
