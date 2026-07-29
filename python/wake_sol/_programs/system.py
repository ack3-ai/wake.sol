"""Built-in System Program — Python builder + decoder (bincode, 4-byte LE u32 tag).

Ported to the unified convention: data args positional (IDL order, alias-typed),
accounts keyword-only. System is **not** Borsh, so each instruction records
``serialization=BINCODE``; the generic engine refuses-in-engine and the
hand-written decode closures below are supplied as ``decode_overrides``. Wire
bytes are unchanged from the previous Rust builder (golden-byte tested).
"""

from __future__ import annotations

import struct
from typing import Sequence

from .._codec import (
    AccountSlot,
    InstructionMeta,
    MetaLike,
    Serialization,
    as_meta,
    build_interface_from_module,
    build_metas,
    instruction,
    pubkey,
    slot,
    u64,
)
from .._interface import register
from .._native import Instruction, Pubkey

SYSTEM_PROGRAM = "11111111111111111111111111111111"
PROGRAM_ID = Pubkey(SYSTEM_PROGRAM)


def _tag(i: int) -> bytes:
    return struct.pack("<I", i)


def _pk(x) -> bytes:
    return Pubkey(x).to_bytes()


# --------------------------------------------------------------------------- #
# decode closures (bincode); reused verbatim as decode_overrides
# --------------------------------------------------------------------------- #
def _create_account(d: bytes) -> dict:
    lamports, space = struct.unpack_from("<QQ", d, 0)
    return {"lamports": lamports, "space": space, "owner": Pubkey(bytes(d[16:48]))}


def _assign(d: bytes) -> dict:
    return {"owner": Pubkey(bytes(d[0:32]))}


def _transfer(d: bytes) -> dict:
    (lamports,) = struct.unpack_from("<Q", d, 0)
    return {"lamports": lamports}


def _allocate(d: bytes) -> dict:
    (space,) = struct.unpack_from("<Q", d, 0)
    return {"space": space}


def _create_account_with_seed(d: bytes) -> dict:
    base = Pubkey(bytes(d[0:32]))
    (slen,) = struct.unpack_from("<Q", d, 32)
    off = 40
    seed = d[off : off + slen].decode("utf-8", "replace")
    off += slen
    lamports, space = struct.unpack_from("<QQ", d, off)
    off += 16
    return {
        "base": base,
        "seed": seed,
        "lamports": lamports,
        "space": space,
        "owner": Pubkey(bytes(d[off : off + 32])),
    }


# --------------------------------------------------------------------------- #
# builder (svm.system)
# --------------------------------------------------------------------------- #
class System:
    """Builder namespace for System Program instructions (reached via ``svm.system``)."""

    program_id = PROGRAM_ID

    @classmethod
    @instruction(InstructionMeta(
        name="transfer", discriminator=_tag(2), serialization=Serialization.BINCODE,
        accounts=(AccountSlot("from", is_signer=True, is_writable=True),
                  AccountSlot("to", is_writable=True)),
    ))
    def transfer(cls, lamports: u64, *, from_: MetaLike, to: MetaLike,
                 remaining_accounts: Sequence[MetaLike] = ()) -> Instruction:
        data = _tag(2) + struct.pack("<Q", lamports)
        metas = build_metas(PROGRAM_ID,
                            slot(from_, True, True, False),
                            slot(to, False, True, False))
        metas += [as_meta(m) for m in remaining_accounts]
        return Instruction(PROGRAM_ID, metas, data)

    @classmethod
    @instruction(InstructionMeta(
        name="create_account", discriminator=_tag(0), serialization=Serialization.BINCODE,
        accounts=(AccountSlot("from", is_signer=True, is_writable=True),
                  AccountSlot("to", is_signer=True, is_writable=True)),
    ))
    def create_account(cls, lamports: u64, space: u64, owner: pubkey, *,
                       from_: MetaLike, to: MetaLike,
                       remaining_accounts: Sequence[MetaLike] = ()) -> Instruction:
        data = _tag(0) + struct.pack("<QQ", lamports, space) + _pk(owner)
        metas = build_metas(PROGRAM_ID,
                            slot(from_, True, True, False),
                            slot(to, True, True, False))
        metas += [as_meta(m) for m in remaining_accounts]
        return Instruction(PROGRAM_ID, metas, data)

    @classmethod
    @instruction(InstructionMeta(
        name="assign", discriminator=_tag(1), serialization=Serialization.BINCODE,
        accounts=(AccountSlot("account", is_signer=True, is_writable=True),),
    ))
    def assign(cls, owner: pubkey, *, account: MetaLike,
               remaining_accounts: Sequence[MetaLike] = ()) -> Instruction:
        data = _tag(1) + _pk(owner)
        metas = build_metas(PROGRAM_ID, slot(account, True, True, False))
        metas += [as_meta(m) for m in remaining_accounts]
        return Instruction(PROGRAM_ID, metas, data)

    @classmethod
    @instruction(InstructionMeta(
        name="allocate", discriminator=_tag(8), serialization=Serialization.BINCODE,
        accounts=(AccountSlot("account", is_signer=True, is_writable=True),),
    ))
    def allocate(cls, space: u64, *, account: MetaLike,
                 remaining_accounts: Sequence[MetaLike] = ()) -> Instruction:
        data = _tag(8) + struct.pack("<Q", space)
        metas = build_metas(PROGRAM_ID, slot(account, True, True, False))
        metas += [as_meta(m) for m in remaining_accounts]
        return Instruction(PROGRAM_ID, metas, data)


# --------------------------------------------------------------------------- #
# registration: builders -> derived interface (decode via overrides); plus the
# decode-only instructions (no builder) added directly.
# --------------------------------------------------------------------------- #
_OVERRIDES = {
    "transfer": _transfer,
    "create_account": _create_account,
    "assign": _assign,
    "allocate": _allocate,
}

_iface = build_interface_from_module(__name__, System, PROGRAM_ID, "System Program",
                                     decode_overrides=_OVERRIDES)
_iface.add("create_account_with_seed", _tag(3), ["from", "to", "base"], _create_account_with_seed)
_iface.add("advance_nonce_account", _tag(4), ["nonce", "recent_blockhashes", "authority"])
_iface.add("withdraw_nonce_account", _tag(5), ["nonce", "to", "recent_blockhashes", "rent", "authority"])
_iface.add("initialize_nonce_account", _tag(6), ["nonce", "recent_blockhashes", "rent"])
_iface.add("authorize_nonce_account", _tag(7), ["nonce", "authority"])
register(_iface)
