"""Signature-verification precompiles: the ``SignedMessage`` claim, its optional
cross-instruction placement (``Ref`` / ``Inline``), the wire assembler, and the
raw ``pack`` hatch shared by the ``ed25519`` / ``secp256k1`` / ``secp256r1`` modules.

A precompile instruction asks the runtime to verify one or more
``(identity, signature, message)`` tuples. :class:`SignedMessage` is that tuple —
the *unit* every builder consumes. Produce one with ``account.sign(msg)``
(ed25519) or ``secp256k1.Key.sign(msg)`` / ``secp256r1.Key.sign(msg)``, then pass
it (or several) to the curve module's ``verify(*claims)``. Forge a bad claim by
constructing :class:`SignedMessage` directly or via ``dataclasses.replace`` — the
first-class fuzzing move (mirrors ``signer()`` / ``writable()`` for account metas).

By default every component (identity, signature, message) is stored **inline** in
the precompile instruction. ``sm.at(message=Ref(other_ix))`` instead points a
component at another instruction's data — the mechanism programs use to bind a
precompile verification to their own instruction (instruction introspection),
without duplicating the bytes. Because the target's transaction index and byte
offset aren't known until assembly, an entry with a ``Ref`` (and *any* inline
secp256k1 entry, which must name its own position — that curve has no self
sentinel) resolves late, at ``account.tx`` / ``.simulate`` time.

Wire layout (per the agave precompile crates):

* **ed25519 / secp256r1** — 2-byte header ``[count, 0]``, then ``count`` 14-byte
  offset structs (all ``u16``), then the referenced data; ``0xFFFF`` = "this
  instruction" (position-independent inline).
* **secp256k1** — 1-byte header ``[count]``, then ``count`` 11-byte offset structs
  (``u8`` indices), then the data; the signature blob carries a trailing
  recovery-id byte. No self sentinel — inline entries bind their own index late.
"""

from __future__ import annotations

import dataclasses
import struct
from dataclasses import dataclass
from typing import Optional, Sequence, Union

from ._interface import DecodedInstruction, ProgramInterface, register
from ._native import Instruction, Pubkey

#: Instruction-index sentinel meaning "this very instruction" (ed25519/secp256r1).
CURRENT_INSTRUCTION = 0xFFFF

SIGNATURE_SIZE = 64


@dataclass(frozen=True)
class Inline:
    """Placement marker: keep this component's bytes inline in the precompile
    instruction (the default for every component)."""


#: Shared default placement instance.
INLINE = Inline()


@dataclass(frozen=True)
class Ref:
    """Placement: this component's bytes live in **another** instruction of the
    transaction. Name the target instruction with ``target=`` (resolved to its
    transaction index at assembly) or a raw ``index=`` (fuzz hatch). Give
    ``offset=``/``size=`` for an explicit slice, or omit ``offset`` to have the
    builder find the component's bytes in the target's data (must occur exactly
    once)."""

    target: Optional[object] = None
    offset: Optional[int] = None
    size: Optional[int] = None
    index: Optional[int] = None


Placement = Union[Inline, Ref]


@dataclass(frozen=True)
class SignedMessage:
    """One ``(identity, signature, message)`` claim for a precompile to verify.

    ``curve`` is ``"ed25519"`` / ``"secp256k1"`` / ``"secp256r1"``; ``identity`` is
    the curve's verifier key (32-byte ed25519 pubkey, 20-byte secp256k1 Ethereum
    address, or 33-byte compressed secp256r1 pubkey); ``signature`` is the 64-byte
    compact signature; ``recovery_id`` is set only for secp256k1. ``bytes(sm)`` and
    ``sm.signature`` both give the raw signature. The ``*_at`` fields carry each
    component's placement (see :meth:`at`)."""

    curve: str
    identity: bytes
    signature: bytes
    message: bytes
    recovery_id: Optional[int] = None
    signature_at: Placement = INLINE
    identity_at: Placement = INLINE
    message_at: Placement = INLINE

    def __bytes__(self) -> bytes:
        return bytes(self.signature)

    def at(
        self,
        *,
        signature: Optional[Placement] = None,
        identity: Optional[Placement] = None,
        message: Optional[Placement] = None,
    ) -> "SignedMessage":
        """Return a copy of this claim with one or more components repointed at
        another instruction, e.g. ``sm.at(message=Ref(program_ix))``. Unspecified
        components keep their current placement."""
        return dataclasses.replace(
            self,
            signature_at=self.signature_at if signature is None else signature,
            identity_at=self.identity_at if identity is None else identity,
            message_at=self.message_at if message is None else message,
        )


@dataclass(frozen=True)
class Offsets:
    """The raw per-entry offset struct, for the ``pack`` hatch. ``*_ix`` are the
    instruction indices (``CURRENT_INSTRUCTION`` for inline on ed25519/secp256r1);
    offsets are byte positions within the referenced instruction's data. No
    *semantic* validation — deliberately able to encode pathological layouts
    (out-of-range offsets, a size that lies, indices pointing anywhere).

    The one hard limit is the wire format: every field is packed, so each value
    must fit its slot — ``0..=65535`` for the ``u16`` offsets/size, and
    ``0..=255`` for secp256k1's ``u8`` instruction indices. Out-of-range values
    raise ``struct.error`` at pack time rather than silently truncating."""

    signature_offset: int
    identity_offset: int
    message_offset: int
    message_size: int
    signature_ix: int = CURRENT_INSTRUCTION
    identity_ix: int = CURRENT_INSTRUCTION
    message_ix: int = CURRENT_INSTRUCTION


@dataclass(frozen=True)
class CurveSpec:
    """Per-curve wire constants. ``inline_ix`` is the index a fully-inline entry
    uses: ``CURRENT_INSTRUCTION`` where the curve has a self sentinel, or ``None``
    for secp256k1 (which must resolve its own transaction index late)."""

    name: str
    program_id: str
    identity_size: int
    index_width: int  # 2 = u16 (ed25519/secp256r1), 1 = u8 (secp256k1)
    has_recovery: bool  # secp256k1: append a recovery-id byte after the signature
    inline_ix: Optional[int]

    @property
    def offsets_size(self) -> int:
        return 14 if self.index_width == 2 else 11

    @property
    def header_size(self) -> int:
        return 2 if self.index_width == 2 else 1

    @property
    def pubkey(self) -> Pubkey:
        return Pubkey(self.program_id)


def _pack_offsets(spec: CurveSpec, o: Offsets) -> bytes:
    """Serialize one offset struct in the curve's exact field layout."""
    if spec.index_width == 2:  # ed25519 / secp256r1: all u16
        return struct.pack(
            "<HHHHHHH",
            o.signature_offset, o.signature_ix,
            o.identity_offset, o.identity_ix,
            o.message_offset, o.message_size, o.message_ix,
        )
    # secp256k1: u16 offsets, u8 instruction indices
    return struct.pack(
        "<HBHBHHB",
        o.signature_offset, o.signature_ix,
        o.identity_offset, o.identity_ix,
        o.message_offset, o.message_size, o.message_ix,
    )


def _signature_blob(spec: CurveSpec, sm: SignedMessage) -> bytes:
    """The signature bytes as stored: 64 bytes, plus the recovery-id byte for
    secp256k1."""
    sig = bytes(sm.signature)
    if len(sig) != SIGNATURE_SIZE:
        raise ValueError(
            f"{spec.name}: signature must be {SIGNATURE_SIZE} bytes, got {len(sig)}"
        )
    if spec.has_recovery:
        if sm.recovery_id is None:
            raise ValueError(f"{spec.name}: claim is missing recovery_id")
        return sig + bytes([sm.recovery_id])
    return sig


def _check_claim(spec: CurveSpec, sm: SignedMessage) -> None:
    if sm.curve != spec.name:
        raise ValueError(
            f"{spec.name}.verify got a {sm.curve!r} claim; curves cannot be mixed "
            "in one precompile instruction"
        )
    if len(bytes(sm.identity)) != spec.identity_size:
        raise ValueError(
            f"{spec.name}: identity must be {spec.identity_size} bytes, "
            f"got {len(bytes(sm.identity))}"
        )


def _has_refs(claims: Sequence[SignedMessage]) -> bool:
    return any(
        isinstance(getattr(sm, name + "_at"), Ref)
        for sm in claims
        for name in ("identity", "signature", "message")
    )


def _resolve_ref(ref: Ref, value: bytes, ix_list: Optional[Sequence]) -> tuple:
    """Resolve a ``Ref`` to ``(instruction_index, byte_offset)`` against the final
    transaction instruction list."""
    if (ref.target is None) == (ref.index is None):
        raise ValueError("Ref needs exactly one of target= (an instruction) or index= (raw)")

    if ref.index is not None:
        idx = ref.index
        target_data = None
        if ix_list is not None and 0 <= idx < len(ix_list):
            t = ix_list[idx]
            target_data = bytes(t.data) if isinstance(t, Instruction) else None
    else:
        if ix_list is None:
            raise ValueError(
                "Ref(target=...) needs transaction context; build the instruction "
                "via account.tx / account.simulate"
            )
        idx = next((j for j, other in enumerate(ix_list) if other is ref.target), None)
        if idx is None:
            raise ValueError("Ref target is not an instruction in this transaction")
        t = ix_list[idx]
        if not isinstance(t, Instruction):
            raise ValueError(
                "Ref target must be a concrete Instruction, not another precompile result"
            )
        target_data = bytes(t.data)

    if ref.offset is not None:
        return idx, ref.offset
    if target_data is None:
        raise ValueError("Ref without offset= needs a resolvable Instruction target to search")
    first = target_data.find(value)
    if first < 0:
        raise ValueError("Ref: component bytes not found in the target instruction; pass offset=")
    if target_data.find(value, first + 1) >= 0:
        raise ValueError("Ref: component bytes occur more than once in the target; pass offset=")
    return idx, first


def build_instruction_data(
    spec: CurveSpec,
    claims: Sequence[SignedMessage],
    self_index: int,
    ix_list: Optional[Sequence] = None,
) -> bytes:
    """Assemble a precompile instruction's data. Inline components are packed into
    this instruction (referencing ``spec.inline_ix``, or ``self_index`` for a
    curve with no self sentinel); ``Ref`` components reference another instruction,
    resolved against ``ix_list``."""
    count = len(claims)
    data_start = spec.header_size + spec.offsets_size * count
    body = bytearray()
    offset_structs = bytearray()
    cursor = data_start
    inline_ix = spec.inline_ix if spec.inline_ix is not None else self_index

    for sm in claims:
        _check_claim(spec, sm)
        components = {
            "identity": bytes(sm.identity),
            "signature": _signature_blob(spec, sm),
            "message": bytes(sm.message),
        }
        placed = {}
        for name in ("identity", "signature", "message"):
            place = getattr(sm, name + "_at")
            value = components[name]
            if isinstance(place, Ref):
                idx, off = _resolve_ref(place, value, ix_list)
                size = place.size if place.size is not None else len(value)
                placed[name] = (off, idx, size)
            else:  # Inline
                placed[name] = (cursor, inline_ix, len(value))
                body += value
                cursor += len(value)

        offset_structs += _pack_offsets(spec, Offsets(
            signature_offset=placed["signature"][0], signature_ix=placed["signature"][1],
            identity_offset=placed["identity"][0], identity_ix=placed["identity"][1],
            message_offset=placed["message"][0], message_ix=placed["message"][1],
            message_size=placed["message"][2],
        ))

    header = bytes([count]) + (b"\x00" if spec.header_size == 2 else b"")
    return header + bytes(offset_structs) + bytes(body)


def build_verify(spec: CurveSpec, claims: Sequence[SignedMessage]):
    """The shared ``verify(*claims)`` implementation. Returns a concrete
    ``Instruction`` for the fully-inline, self-sentinel case (ed25519/secp256r1),
    else a deferred :class:`PrecompileInstruction` (secp256k1, or any ``Ref``)."""
    claims = tuple(claims)
    if spec.inline_ix is None or _has_refs(claims):
        return PrecompileInstruction(spec, claims)
    data = build_instruction_data(spec, claims, self_index=0, ix_list=None)
    return Instruction(spec.pubkey, [], data)


def pack(spec: CurveSpec, count: int, entries: Sequence[Offsets], data: bytes) -> Instruction:
    """The raw hatch: serialize ``[count] + entries + data`` **verbatim**, with no
    offset computation and no bounds/count validation — for constructing
    deliberately-malformed instructions (wrong indices, count/entry mismatch,
    zero entries, out-of-range offsets). ``count`` is written to the header
    independently of ``len(entries)``, so the two can disagree on purpose."""
    header = bytes([count & 0xFF]) + (b"\x00" if spec.header_size == 2 else b"")
    blob = bytearray(header)
    for o in entries:
        blob += _pack_offsets(spec, o)
    blob += bytes(data)
    return Instruction(spec.pubkey, [], bytes(blob))


class PrecompileInstruction:
    """A precompile ``verify(...)`` result whose instruction indices cannot be
    fixed until the transaction is assembled — a secp256k1 inline entry must name
    its own transaction position (that curve has no self sentinel), and any
    ``Ref`` names another instruction resolved by position. Pass it to
    ``account.tx`` / ``account.simulate`` (which know the final order), or resolve
    it explicitly with ``.resolve(self_index, ix_list)``. Claims are validated up
    front, so a bad claim raises here, not at send time."""

    __slots__ = ("_spec", "_claims")

    def __init__(self, spec: CurveSpec, claims: Sequence[SignedMessage]) -> None:
        claims = tuple(claims)
        for sm in claims:
            _check_claim(spec, sm)
            _signature_blob(spec, sm)  # eager size/recovery validation
        self._spec = spec
        self._claims = claims

    @property
    def program_id(self) -> Pubkey:
        return self._spec.pubkey

    def resolve(self, self_index: int, ix_list: Optional[Sequence] = None) -> Instruction:
        """Concrete instruction, binding inline components to ``self_index`` and
        any ``Ref`` against ``ix_list`` (the full transaction instruction list)."""
        data = build_instruction_data(self._spec, self._claims, self_index, ix_list)
        return Instruction(self._spec.pubkey, [], data)

    def __repr__(self) -> str:
        return f"PrecompileInstruction({self._spec.name}, {len(self._claims)} claim(s))"


def materialize(ixs: Sequence) -> list:
    """Resolve a mixed list of ``Instruction``s and ``PrecompileInstruction``s into
    concrete ``Instruction``s, binding each deferred precompile to its final index
    and resolving cross-references against the whole list. Called by
    ``Account.tx`` / ``Account.simulate``."""
    ixs = list(ixs)
    out = []
    for i, ix in enumerate(ixs):
        if isinstance(ix, Instruction):
            out.append(ix)
        elif isinstance(ix, PrecompileInstruction):
            out.append(ix.resolve(i, ixs))
        else:
            raise TypeError(
                f"tx()/simulate() got {type(ix).__name__}; expected an Instruction "
                "or a precompile verify(...) result"
            )
    return out


class _PrecompileInterface(ProgramInterface):
    """Call-trace decoder: reports the entry count parsed from the instruction
    header. Precompiles carry no accounts and an opaque offsets/data blob."""

    _DISPLAY = {
        "ed25519": "Ed25519 Program",
        "secp256k1": "Secp256k1 Program",
        "secp256r1": "Secp256r1 Program",
    }

    def __init__(self, spec: CurveSpec) -> None:
        super().__init__(spec.program_id, self._DISPLAY[spec.name])
        self._spec = spec

    def decode(self, data: bytes, n_accounts: int) -> DecodedInstruction:
        count = data[0] if data else 0
        return DecodedInstruction(self.name, "verify", {"signatures": count}, [])


def register_precompile(spec: CurveSpec) -> None:
    register(_PrecompileInterface(spec))
