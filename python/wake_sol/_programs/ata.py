"""Built-in decoder for the Associated Token Account program (1-byte tag)."""

from __future__ import annotations

from .._interface import ProgramInterface, register

ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

# Account order shared by Create / CreateIdempotent.
_CREATE_ACCOUNTS = ["funder", "ata", "owner", "mint", "system_program", "token_program"]

_iface = ProgramInterface(ATA_PROGRAM, "Associated Token Account Program")
_iface.add("create", b"\x00", _CREATE_ACCOUNTS)
_iface.add("create_idempotent", b"\x01", _CREATE_ACCOUNTS)
_iface.add(
    "recover_nested",
    b"\x02",
    ["nested_ata", "nested_mint", "destination_ata", "owner_ata", "owner_mint", "wallet", "token_program"],
)
register(_iface)
