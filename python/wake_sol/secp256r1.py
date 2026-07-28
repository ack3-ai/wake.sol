"""Secp256r1 (NIST P-256, passkeys/WebAuthn) signature-verification precompile
(``Secp256r1SigVerify1…``).

Keys are **not** Solana accounts — the verifier identity is a 33-byte compressed
public key::

    k = secp256r1.Key.new()          # seeded from wake_sol.random (reproducible)
    ix = secp256r1.verify(k.sign(b"authorize"))
    payer.tx(ix)

Signatures are low-S normalized (the precompile rejects high-S). `verify` batches
variadically and is position-independent (this curve has a self sentinel), so it
returns a concrete `Instruction`. Forge a bad claim with `SignedMessage(...)` /
`dataclasses.replace`; `pack` is the raw offsets-table hatch.
"""

from __future__ import annotations

from typing import Sequence

from ._addresses import SECP256R1_PROGRAM_ID
from ._native import Instruction
from ._native import secp256r1_public_key as _public_key
from ._native import secp256r1_secret_from_seed as _secret_from_seed
from ._native import secp256r1_sign as _sign
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

PROGRAM_ID = SECP256R1_PROGRAM_ID

_SPEC = CurveSpec(
    name="secp256r1",
    program_id=str(PROGRAM_ID),
    identity_size=33,
    index_width=2,
    has_recovery=False,
    inline_ix=CURRENT_INSTRUCTION,
)


class Key:
    """A secp256r1 (P-256) signing key. Not a Solana account; its verifier
    identity is a 33-byte compressed public key (``.public_key``)."""

    __slots__ = ("_secret",)

    def __init__(self, secret: bytes) -> None:
        _public_key(bytes(secret))  # validates the scalar; raises if invalid
        self._secret = bytes(secret)

    @staticmethod
    def new() -> "Key":
        """A fresh key, seeded from ``wake_sol.random`` (reproducible from
        ``--seed``, like ``Account.new()``)."""
        import wake_sol

        seed = bytes(wake_sol.random.randbytes(32))
        return Key(_secret_from_seed(seed))

    @staticmethod
    def from_secret(secret: bytes) -> "Key":
        """A key from a known 32-byte secret scalar."""
        return Key(secret)

    @property
    def secret(self) -> bytes:
        return self._secret

    @property
    def public_key(self) -> bytes:
        """The 33-byte compressed public key the precompile checks against."""
        return _public_key(self._secret)

    @property
    def identity(self) -> bytes:
        """Uniform alias for ``public_key`` (the cross-curve claim identity)."""
        return self.public_key

    def sign(self, message: bytes) -> SignedMessage:
        """Sign ``message`` (SHA-256, low-S normalized)."""
        sig = _sign(self._secret, bytes(message))
        return SignedMessage("secp256r1", self.public_key, sig, bytes(message), None)

    def __repr__(self) -> str:
        return f"secp256r1.Key(public_key=0x{self.public_key.hex()})"


def verify(*claims: SignedMessage) -> "Instruction | PrecompileInstruction":
    """Build a secp256r1 precompile instruction verifying each claim. Fully-inline
    it is a concrete, position-independent `Instruction`; with a `.at(...=Ref(...))`
    placement it is a deferred `PrecompileInstruction`."""
    return build_verify(_SPEC, claims)


def pack(count: int, entries: Sequence[Offsets], data: bytes = b"") -> Instruction:
    """Raw hatch: assemble ``[count] + entries + data`` verbatim (no validation)."""
    return _pack(_SPEC, count, entries, data)


register_precompile(_SPEC)
