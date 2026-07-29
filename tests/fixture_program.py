"""Hand-authored fixture program mirroring generator output — the canonical
acceptance artifact. Exercises the full supported type surface and the unified
builder convention (data positional, accounts keyword-only, decoder derived from
the signature). Imported by ``test_codec.py``; importing it registers the
program.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Annotated, Optional

from wake_sol._codec import (
    AccountSlot,
    BorshEnumMeta,
    BorshMeta,
    InstructionMeta,
    Kind,
    Opt,
    Serialization,
    as_meta,
    build_interface_from_module,
    build_metas,
    compile_layout,
    encode_ix_layout,
    f64,
    i32,
    i64,
    instruction,
    pubkey,
    slot,
    u8,
    u16,
    u32,
    u64,
    u128,
    variant,
)
from wake_sol._interface import register
from wake_sol._native import Instruction, Pubkey

PROGRAM_ID = Pubkey(7)
PROGRAM_NAME = "Fixture Program"


# --------------------------------------------------------------------------- #
# 1. enums
# --------------------------------------------------------------------------- #
class Side(IntEnum):          # all-unit -> IntEnum (value = declaration index)
    Bid = 0
    Ask = 1


class Action(metaclass=BorshEnumMeta):   # data-carrying -> BorshEnum
    @variant(0)
    @dataclass
    class Noop:
        pass                  # unit variant -> still an (empty) dataclass

    @variant(1)
    @dataclass
    class Move:
        x: i64                # named-field variant
        y: i64

    @variant(2)
    @dataclass
    class Label:
        _0: str               # single-field TUPLE variant -> still wrapped, _0

    @variant(3)
    @dataclass
    class Pair:
        _0: u32               # multi-field tuple variant
        _1: pubkey


# --------------------------------------------------------------------------- #
# 2. structs
# --------------------------------------------------------------------------- #
@dataclass
class Inner:                  # nested named struct
    a: u16
    b: bool


@dataclass
class Pixel:                  # tuple struct (no `name` keys -> _0, _1)
    _0: u8
    _1: u8


@dataclass
class Empty:                  # unit / empty struct
    pass


@dataclass
class AllTypes:              # the full scalar + container surface
    id: u64
    price: u128
    tick: i32
    ratio: f64
    is_bid: bool
    memo: str
    payload: bytes
    maker: pubkey
    referrer: Optional[pubkey]            # option (typing.Union spelling)
    delegate: pubkey | None               # option (PEP 604 spelling)
    tags: list[u16]                       # vec
    checksum: Annotated[bytes, 32]        # [u8;32] -> fixed-length bytes
    samples: Annotated[list[i64], 4]      # array<i64,4> -> list, no prefix
    expiry: Opt[Opt[u64]]                 # Option<Option<u64>>, non-collapsing
    side: Side                            # IntEnum
    action: Action                        # BorshEnum
    inner: Inner                          # nested struct


# --------------------------------------------------------------------------- #
# 3. account (carries type-level metadata + an 8-byte discriminator)
# --------------------------------------------------------------------------- #
@dataclass
class Position:
    owner: pubkey
    amount: u64
    bump: u8

    __borsh_meta__ = BorshMeta(
        ser=Serialization.BORSH,
        kind=Kind.ACCOUNT,
        is_account_root=True,
        discriminator=b"\xaa\xc0\x97\x2c\x1b\x3f\x10\x52",
        discriminator_len=8,
    )


# --------------------------------------------------------------------------- #
# 4. instructions (the builder signature is the single source of truth)
# --------------------------------------------------------------------------- #
DISC_SWAP = b"\x01\x02\x03\x04\x05\x06\x07\x08"
DISC_STORE = b"\x11\x12\x13\x14\x15\x16\x17\x18"
DISC_NOOP = b"\x21\x22\x23\x24\x25\x26\x27\x28"

# Encode layouts — compiled once at import (the encode-side mirror of the decode
# layout built at registration); the builder methods feed them to
# `encode_ix_layout`, so no annotation is lowered on the encode hot path.
_ENC_do_swap = compile_layout(u64, Side)
_ENC_store = compile_layout(AllTypes)


class Fixture:
    program_id = PROGRAM_ID

    @classmethod
    @instruction(InstructionMeta(
        name="do_swap",
        discriminator=DISC_SWAP,
        accounts=(
            AccountSlot("user", is_signer=True, is_writable=True),
            AccountSlot("pool", is_writable=True),
            AccountSlot("referrer", is_optional=True),    # interior optional
        ),
    ))
    def do_swap(cls, amount_in: u64, side: Side, *,
                user, pool, referrer=None, remaining_accounts=()) -> Instruction:
        data = encode_ix_layout(DISC_SWAP, _ENC_do_swap, amount_in, side)
        metas = build_metas(
            PROGRAM_ID,
            slot(user, True, True, False),
            slot(pool, False, True, False),
            slot(referrer, False, False, True),     # None -> program-ID sentinel
        )
        metas += [as_meta(m) for m in remaining_accounts]
        return Instruction(PROGRAM_ID, metas, data)

    @classmethod
    @instruction(InstructionMeta(
        name="store",
        discriminator=DISC_STORE,
        accounts=(AccountSlot("account", is_writable=True),),
    ))
    def store(cls, cfg: AllTypes, *, account, remaining_accounts=()) -> Instruction:
        data = encode_ix_layout(DISC_STORE, _ENC_store, cfg)
        metas = build_metas(PROGRAM_ID, slot(account, False, True, False))
        metas += [as_meta(m) for m in remaining_accounts]
        return Instruction(PROGRAM_ID, metas, data)

    @classmethod
    @instruction(InstructionMeta(
        name="noop",
        discriminator=DISC_NOOP,
        accounts=(AccountSlot("account"),),
    ))
    def noop(cls, *, account, remaining_accounts=()) -> Instruction:
        data = encode_ix_layout(DISC_NOOP, ())
        metas = build_metas(PROGRAM_ID, slot(account, False, False, False))
        metas += [as_meta(m) for m in remaining_accounts]
        return Instruction(PROGRAM_ID, metas, data)


# 5. self-register (import side effect populates the global REGISTRY)
INTERFACE = register(
    build_interface_from_module(__name__, Fixture, PROGRAM_ID, PROGRAM_NAME)
)
