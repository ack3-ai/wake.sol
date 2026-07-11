"""Inert metadata records + decorators consumed by the registration walk.

These carry the facts annotations cannot express (serialization mode,
discriminator, account-root marker, optional/PDA account slots). The trusted
codec only *consumes* them; the generator/facts-extractor populates them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Serialization(Enum):
    BORSH = "borsh"
    BYTEMUCK = "bytemuck"              # -> codec refuses (use C-layout/facts)
    BYTEMUCK_UNSAFE = "bytemuckUnsafe"  # -> codec refuses
    CUSTOM = "custom"                 # -> codec refuses (layout not in IDL)
    BINCODE = "bincode"               # -> built-in decoder (System)
    PACK = "pack"                     # -> built-in decoder (SPL Token)


#: §4 spells the enum ``SerKind``; keep the alias so either name resolves.
SerKind = Serialization


class Kind(Enum):
    STRUCT = "struct"
    ACCOUNT = "account"
    EVENT = "event"
    ENUM = "enum"


@dataclass(frozen=True)
class BorshMeta:
    """Type-level metadata, attached as the ``__borsh_meta__`` class-var."""

    ser: Serialization
    kind: Kind
    is_account_root: bool = False
    discriminator: bytes = b""
    discriminator_len: int = 0          # MUST equal len(discriminator)
    assumed_int128_align: int = 8       # 8 or 16; cross-checked vs account length
    option_tag_width: int = 1           # 1 (Borsh) or 4 (COption engine ext.)


@dataclass(frozen=True)
class Seed:
    """A single PDA seed (const/arg/account). Carried inertly in v1 (no derive)."""

    kind: str            # "const" | "arg" | "account"
    value: object

    @staticmethod
    def const(value: bytes) -> "Seed":
        return Seed("const", value)

    @staticmethod
    def arg(path: str) -> "Seed":
        return Seed("arg", path)

    @staticmethod
    def account(name: str) -> "Seed":
        return Seed("account", name)


@dataclass(frozen=True)
class IdlPda:
    """Inert PDA recipe (seeds + optional seeds-program). Auto-derive deferred."""

    seeds: tuple = ()
    program: Optional[object] = None


@dataclass(frozen=True)
class AccountSlot:
    """One instruction account slot, in IDL declaration order."""

    name: str
    is_signer: bool = False
    is_writable: bool = False
    is_optional: bool = False        # absent -> program-ID sentinel at this slot
    pda: Optional[IdlPda] = None     # carried inertly in v1


@dataclass(frozen=True)
class InstructionMeta:
    name: str
    discriminator: bytes
    accounts: tuple = ()                       # tuple[AccountSlot, ...], IDL order
    serialization: Serialization = Serialization.BORSH
    discriminator_len: Optional[int] = None    # defaults to len(discriminator)
    returns_type: Optional[object] = None      # IDL `returns` type, if any


def instruction(meta: InstructionMeta):
    """Attach an :class:`InstructionMeta` to a builder method. Read at
    registration to derive both the dispatch entry and the decoder."""

    def deco(fn):
        fn.__pytypes_ix__ = meta
        return fn

    return deco


def event(discriminator: bytes):
    """Attach an 8-byte event discriminator to an event ``@dataclass``."""

    def deco(cls):
        cls.__event_disc__ = discriminator
        return cls

    return deco


class ProgramError:
    """Minimal base for generated per-program error enums. The full
    errors/events runtime subsystem (§10) is deferred."""


class AccountFlagOverride(UserWarning):
    """Warned when an explicit ``AccountMeta``'s flags differ from the IDL slot."""


class GenError(Exception):
    """Generation-time refusal: a layout that cannot be represented (refuse,
    don't guess) — e.g. a bare ``int``/``float`` field or an untagged union."""


class RefuseToDecode(Exception):
    """A matched type cannot be decoded by the trusted engine (e.g. non-borsh
    serialization with no built-in override). Surfaced, never swallowed."""


_DISC_PREFIX = {"instruction": "global", "account": "account", "event": "event"}


def anchor_discriminator(kind: str, name: str) -> bytes:
    """Compute an 8-byte Anchor discriminator from a prefixed name. Used ONLY as
    the pre-0.30 fallback when the IDL carries no discriminator array (§5.8); a
    present array (0.30+) or a custom/zero-length discriminator is taken
    verbatim, never recomputed."""
    prefix = _DISC_PREFIX[kind]
    return hashlib.sha256(f"{prefix}:{name}".encode()).digest()[:8]
