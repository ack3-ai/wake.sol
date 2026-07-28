"""Ed25519 signature-verification precompile (``Ed25519SigVerify11…``).

Sign a message with any keypair-bearing ``Account`` and drop the resulting claim
into ``verify``::

    alice = Account.new()
    ix = ed25519.verify(alice.sign(b"hello"))
    payer.tx(ix)

``verify`` batches: ``ed25519.verify(a.sign(m1), b.sign(m2))`` puts both in one
instruction. Forge a bad claim with ``SignedMessage(...)`` / ``dataclasses.replace``
and it verifies to failure. ``pack`` is the raw offsets-table hatch.
"""

from __future__ import annotations

from typing import Sequence

from ._addresses import ED25519_PROGRAM_ID
from ._native import Instruction
from ._precompiles import (
    CURRENT_INSTRUCTION,
    CurveSpec,
    Offsets,
    PrecompileInstruction,
    SignedMessage,
    build_verify,
    pack as _pack,
    register_precompile,
)

PROGRAM_ID = ED25519_PROGRAM_ID

_SPEC = CurveSpec(
    name="ed25519",
    program_id=str(PROGRAM_ID),
    identity_size=32,
    index_width=2,
    has_recovery=False,
    inline_ix=CURRENT_INSTRUCTION,
)


def verify(*claims: SignedMessage) -> "Instruction | PrecompileInstruction":
    """Build an ed25519 precompile instruction verifying each claim. Fully-inline
    (the common case) it is a concrete, position-independent `Instruction`; if any
    claim places a component with `.at(...=Ref(...))`, it is a deferred
    `PrecompileInstruction` resolved at `account.tx` / `.simulate` time."""
    return build_verify(_SPEC, claims)


def pack(count: int, entries: Sequence[Offsets], data: bytes = b"") -> Instruction:
    """Raw hatch: assemble ``[count] + entries + data`` verbatim (no validation)."""
    return _pack(_SPEC, count, entries, data)


register_precompile(_SPEC)
