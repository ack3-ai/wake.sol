"""Secp256k1 (Ethereum-style) signature-verification precompile
(``KeccakSecp256k11…``).

Keys are **not** Solana accounts — the verifier identity is a 20-byte
keccak-derived Ethereum address::

    k = secp256k1.Key.new()          # seeded from solana_fuzzer.random (reproducible)
    ix = secp256k1.verify(k.sign(b"withdraw:42"))
    payer.tx(ix)

`verify` batches variadically. Unlike ed25519/secp256r1, this precompile has **no
"current instruction" sentinel**, so an inline entry must reference its own
position in the transaction — therefore `verify(...)` returns a
`PrecompileInstruction` that binds its index at `account.tx` / `.simulate` time
(or via `.resolve(index)`). Forge a bad claim with `SignedMessage(...)` /
`dataclasses.replace`; `pack` is the raw offsets-table hatch.
"""

from __future__ import annotations

from typing import Sequence

from ._addresses import SECP256K1_PROGRAM_ID
from ._native import Instruction
from ._native import secp256k1_eth_address as _eth_address
from ._native import secp256k1_secret_from_seed as _secret_from_seed
from ._native import secp256k1_sign as _sign
from ._precompiles import (
    CurveSpec,
    Offsets,
    PrecompileInstruction,
    SignedMessage,
    pack as _pack,
    register_precompile,
)

PROGRAM_ID = SECP256K1_PROGRAM_ID

_SPEC = CurveSpec(
    name="secp256k1",
    program_id=str(PROGRAM_ID),
    identity_size=20,
    index_width=1,
    has_recovery=True,
    inline_ix=None,  # no self sentinel → resolved late
)


class Key:
    """A secp256k1 (Ethereum) signing key. Not a Solana account; its verifier
    identity is a 20-byte Ethereum address (``.eth_address``)."""

    __slots__ = ("_secret",)

    def __init__(self, secret: bytes) -> None:
        _eth_address(bytes(secret))  # validates the scalar; raises if invalid
        self._secret = bytes(secret)

    @staticmethod
    def new() -> "Key":
        """A fresh key, seeded from ``solana_fuzzer.random`` (reproducible from
        ``--seed``, like ``Account.new()``)."""
        import solana_fuzzer

        seed = bytes(solana_fuzzer.random.randbytes(32))
        return Key(_secret_from_seed(seed))

    @staticmethod
    def from_secret(secret: bytes) -> "Key":
        """A key from a known 32-byte secret scalar."""
        return Key(secret)

    @property
    def secret(self) -> bytes:
        return self._secret

    @property
    def eth_address(self) -> bytes:
        """The 20-byte Ethereum address the precompile checks against."""
        return _eth_address(self._secret)

    @property
    def identity(self) -> bytes:
        """Uniform alias for ``eth_address`` (the cross-curve claim identity)."""
        return self.eth_address

    def sign(self, message: bytes) -> SignedMessage:
        """Sign ``message`` (keccak256-hashed internally), returning a claim
        carrying the recovery id the precompile needs."""
        sig, recovery_id = _sign(self._secret, bytes(message))
        return SignedMessage("secp256k1", self.eth_address, sig, bytes(message), recovery_id)

    def __repr__(self) -> str:
        return f"secp256k1.Key(eth_address=0x{self.eth_address.hex()})"


def verify(*claims: SignedMessage) -> PrecompileInstruction:
    """Build a secp256k1 precompile instruction verifying each claim. Always a
    deferred `PrecompileInstruction` (this curve has no self sentinel, so inline
    entries bind their own index at assembly); pass it to `account.tx` /
    `.simulate`."""
    return PrecompileInstruction(_SPEC, claims)


def pack(count: int, entries: Sequence[Offsets], data: bytes = b"") -> Instruction:
    """Raw hatch: assemble ``[count] + entries + data`` verbatim (no validation)."""
    return _pack(_SPEC, count, entries, data)


register_precompile(_SPEC)
