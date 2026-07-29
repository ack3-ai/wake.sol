"""Built-in SPL Token / Token-2022 — Python builder + decoder (1-byte tag, packed).

Ported to the unified convention: data args positional, accounts keyword-only.
Token is **not** Borsh (SPL Pack), so each instruction records
``serialization=PACK`` and the hand-written decode closures are supplied as
``decode_overrides``. Wire bytes are unchanged (golden-byte tested). Token-2022
shares the classic base instruction set; both program ids register it.
"""

from __future__ import annotations

import struct
from typing import Optional, Sequence

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
    u8,
    u64,
)
from .._interface import register
from .._native import AccountMeta, Instruction, Pubkey

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = Pubkey("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM = Pubkey("11111111111111111111111111111111")


def _pk(x) -> bytes:
    return Pubkey(x).to_bytes()


# --------------------------------------------------------------------------- #
# decode closures (SPL Pack); reused verbatim as decode_overrides
# --------------------------------------------------------------------------- #
def _none(_d: bytes) -> dict:
    return {}


def _amount(d: bytes) -> dict:
    (amount,) = struct.unpack_from("<Q", d, 0)
    return {"amount": amount}


def _amount_decimals(d: bytes) -> dict:
    amount, decimals = struct.unpack_from("<QB", d, 0)
    return {"amount": amount, "decimals": decimals}


def _initialize_mint(d: bytes) -> dict:
    out = {"decimals": d[0], "mint_authority": Pubkey(bytes(d[1:33]))}
    if len(d) > 33 and d[33] == 1:
        out["freeze_authority"] = Pubkey(bytes(d[34:66]))
    else:
        out["freeze_authority"] = None
    return out


def _owner(d: bytes) -> dict:
    return {"owner": Pubkey(bytes(d[0:32]))}


# --------------------------------------------------------------------------- #
# builder (svm.token)
# --------------------------------------------------------------------------- #
class Token:
    """Builder namespace for SPL Token instructions (reached via ``svm.token``)."""

    program_id = Pubkey(TOKEN_PROGRAM)

    # ---- instructions (registered) ----
    @classmethod
    @instruction(InstructionMeta(
        name="initialize_mint2", discriminator=b"\x14", serialization=Serialization.PACK,
        accounts=(AccountSlot("mint", is_writable=True),),
    ))
    def initialize_mint2(cls, decimals: u8, mint_authority: pubkey,
                         freeze_authority: Optional[pubkey] = None, *,
                         mint: MetaLike,
                         remaining_accounts: Sequence[MetaLike] = ()) -> Instruction:
        data = bytes([0x14, decimals]) + _pk(mint_authority)
        data += (b"\x01" + _pk(freeze_authority)) if freeze_authority is not None else b"\x00"
        metas = build_metas(cls.program_id, slot(mint, False, True, False))
        metas += [as_meta(m) for m in remaining_accounts]
        return Instruction(cls.program_id, metas, data)

    @classmethod
    @instruction(InstructionMeta(
        name="initialize_account3", discriminator=b"\x12", serialization=Serialization.PACK,
        accounts=(AccountSlot("account", is_writable=True), AccountSlot("mint")),
    ))
    def initialize_account3(cls, owner: pubkey, *, account: MetaLike, mint: MetaLike,
                            remaining_accounts: Sequence[MetaLike] = ()) -> Instruction:
        data = b"\x12" + _pk(owner)
        metas = build_metas(cls.program_id,
                            slot(account, False, True, False),
                            slot(mint, False, False, False))
        metas += [as_meta(m) for m in remaining_accounts]
        return Instruction(cls.program_id, metas, data)

    @classmethod
    @instruction(InstructionMeta(
        name="transfer_checked", discriminator=b"\x0c", serialization=Serialization.PACK,
        accounts=(AccountSlot("source", is_writable=True), AccountSlot("mint"),
                  AccountSlot("destination", is_writable=True),
                  AccountSlot("authority", is_signer=True)),
    ))
    def transfer_checked(cls, amount: u64, decimals: u8, *,
                         source: MetaLike, mint: MetaLike, destination: MetaLike,
                         authority: MetaLike,
                         remaining_accounts: Sequence[MetaLike] = ()) -> Instruction:
        data = b"\x0c" + struct.pack("<QB", amount, decimals)
        metas = build_metas(cls.program_id,
                            slot(source, False, True, False),
                            slot(mint, False, False, False),
                            slot(destination, False, True, False),
                            slot(authority, True, False, False))
        metas += [as_meta(m) for m in remaining_accounts]
        return Instruction(cls.program_id, metas, data)

    @classmethod
    @instruction(InstructionMeta(
        name="transfer", discriminator=b"\x03", serialization=Serialization.PACK,
        accounts=(AccountSlot("source", is_writable=True),
                  AccountSlot("destination", is_writable=True),
                  AccountSlot("authority", is_signer=True)),
    ))
    def transfer(cls, amount: u64, *, source: MetaLike, destination: MetaLike,
                 authority: MetaLike,
                 remaining_accounts: Sequence[MetaLike] = ()) -> Instruction:
        data = b"\x03" + struct.pack("<Q", amount)
        metas = build_metas(cls.program_id,
                            slot(source, False, True, False),
                            slot(destination, False, True, False),
                            slot(authority, True, False, False))
        metas += [as_meta(m) for m in remaining_accounts]
        return Instruction(cls.program_id, metas, data)

    @classmethod
    @instruction(InstructionMeta(
        name="mint_to_checked", discriminator=b"\x0e", serialization=Serialization.PACK,
        accounts=(AccountSlot("mint", is_writable=True),
                  AccountSlot("account", is_writable=True),
                  AccountSlot("authority", is_signer=True)),
    ))
    def mint_to_checked(cls, amount: u64, decimals: u8, *,
                        mint: MetaLike, account: MetaLike, authority: MetaLike,
                        remaining_accounts: Sequence[MetaLike] = ()) -> Instruction:
        data = b"\x0e" + struct.pack("<QB", amount, decimals)
        metas = build_metas(cls.program_id,
                            slot(mint, False, True, False),
                            slot(account, False, True, False),
                            slot(authority, True, False, False))
        metas += [as_meta(m) for m in remaining_accounts]
        return Instruction(cls.program_id, metas, data)

    @classmethod
    @instruction(InstructionMeta(
        name="burn_checked", discriminator=b"\x0f", serialization=Serialization.PACK,
        accounts=(AccountSlot("account", is_writable=True),
                  AccountSlot("mint", is_writable=True),
                  AccountSlot("authority", is_signer=True)),
    ))
    def burn_checked(cls, amount: u64, decimals: u8, *,
                     account: MetaLike, mint: MetaLike, authority: MetaLike,
                     remaining_accounts: Sequence[MetaLike] = ()) -> Instruction:
        data = b"\x0f" + struct.pack("<QB", amount, decimals)
        metas = build_metas(cls.program_id,
                            slot(account, False, True, False),
                            slot(mint, False, True, False),
                            slot(authority, True, False, False))
        metas += [as_meta(m) for m in remaining_accounts]
        return Instruction(cls.program_id, metas, data)

    @classmethod
    @instruction(InstructionMeta(
        name="close_account", discriminator=b"\x09", serialization=Serialization.PACK,
        accounts=(AccountSlot("account", is_writable=True),
                  AccountSlot("destination", is_writable=True),
                  AccountSlot("owner", is_signer=True)),
    ))
    def close_account(cls, *, account: MetaLike, destination: MetaLike, owner: MetaLike,
                      remaining_accounts: Sequence[MetaLike] = ()) -> Instruction:
        data = b"\x09"
        metas = build_metas(cls.program_id,
                            slot(account, False, True, False),
                            slot(destination, False, True, False),
                            slot(owner, True, False, False))
        metas += [as_meta(m) for m in remaining_accounts]
        return Instruction(cls.program_id, metas, data)

    @classmethod
    @instruction(InstructionMeta(
        name="approve", discriminator=b"\x04", serialization=Serialization.PACK,
        accounts=(AccountSlot("source", is_writable=True), AccountSlot("delegate"),
                  AccountSlot("authority", is_signer=True)),
    ))
    def approve(cls, amount: u64, *, source: MetaLike, delegate: MetaLike,
                authority: MetaLike,
                remaining_accounts: Sequence[MetaLike] = ()) -> Instruction:
        data = b"\x04" + struct.pack("<Q", amount)
        metas = build_metas(cls.program_id,
                            slot(source, False, True, False),
                            slot(delegate, False, False, False),
                            slot(authority, True, False, False))
        metas += [as_meta(m) for m in remaining_accounts]
        return Instruction(cls.program_id, metas, data)

    @classmethod
    @instruction(InstructionMeta(
        name="revoke", discriminator=b"\x05", serialization=Serialization.PACK,
        accounts=(AccountSlot("source", is_writable=True),
                  AccountSlot("authority", is_signer=True)),
    ))
    def revoke(cls, *, source: MetaLike, authority: MetaLike,
               remaining_accounts: Sequence[MetaLike] = ()) -> Instruction:
        data = b"\x05"
        metas = build_metas(cls.program_id,
                            slot(source, False, True, False),
                            slot(authority, True, False, False))
        metas += [as_meta(m) for m in remaining_accounts]
        return Instruction(cls.program_id, metas, data)

    # ---- ATA convenience (targets the Associated Token Account program) ----
    @classmethod
    def ata_address(cls, owner: MetaLike, mint: MetaLike) -> Pubkey:
        addr, _bump = Pubkey.find_program_address(
            [_pk(owner), _pk(cls.program_id), _pk(mint)], ATA_PROGRAM)
        return addr

    @classmethod
    def create_ata(cls, funder: MetaLike, owner: MetaLike, mint: MetaLike) -> Instruction:
        ata = cls.ata_address(owner, mint)
        metas = [
            AccountMeta(funder, True, True),
            AccountMeta(ata, False, True),
            AccountMeta(owner, False, False),
            AccountMeta(mint, False, False),
            AccountMeta(SYSTEM_PROGRAM, False, False),
            AccountMeta(cls.program_id, False, False),
        ]
        return Instruction(ATA_PROGRAM, metas, b"\x00")   # ATA Create discriminator


# --------------------------------------------------------------------------- #
# registration (classic + Token-2022 share the base instruction set)
# --------------------------------------------------------------------------- #
_OVERRIDES = {
    "initialize_mint2": _initialize_mint,
    "initialize_account3": _owner,
    "transfer_checked": _amount_decimals,
    "transfer": _amount,
    "mint_to_checked": _amount_decimals,
    "burn_checked": _amount_decimals,
    "close_account": _none,
    "approve": _amount,
    "revoke": _none,
}


def _make_iface(program_id: str, name: str):
    iface = build_interface_from_module(__name__, Token, program_id, name,
                                        decode_overrides=_OVERRIDES)
    # decode-only instructions (no builder method)
    iface.add("initialize_mint", b"\x00", ["mint", "rent"], _initialize_mint)
    iface.add("initialize_account", b"\x01", ["account", "mint", "owner", "rent"])
    iface.add("initialize_multisig", b"\x02", ["multisig", "rent"])
    iface.add("set_authority", b"\x06", ["account", "authority"])
    iface.add("mint_to", b"\x07", ["mint", "account", "authority"], _amount)
    iface.add("burn", b"\x08", ["account", "mint", "authority"], _amount)
    iface.add("freeze_account", b"\x0a", ["account", "mint", "authority"])
    iface.add("thaw_account", b"\x0b", ["account", "mint", "authority"])
    iface.add("approve_checked", b"\x0d", ["source", "mint", "delegate", "authority"], _amount_decimals)
    iface.add("initialize_account2", b"\x10", ["account", "mint", "rent"], _owner)
    iface.add("sync_native", b"\x11", ["account"])
    iface.add("initialize_multisig2", b"\x13", ["multisig"])
    iface.add("get_account_data_size", b"\x15", ["mint"])
    iface.add("initialize_immutable_owner", b"\x16", ["account"])
    return register(iface)


_make_iface(TOKEN_PROGRAM, "Token Program")
_make_iface(TOKEN_2022_PROGRAM, "Token-2022 Program")
