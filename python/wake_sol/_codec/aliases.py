"""Lowercase width-carrying primitive aliases and their identity-keyed specs.

Each width is a single distinct runtime object — a real ``int`` (or ``float``)
**subclass** carrying ``min`` / ``max`` / ``bits`` / ``signed`` class attributes
(à la wake's ``uintN``/``intN``), so you can write ``u256.max``, ``i32.min``,
``u8.bits`` directly. The codec recovers the width by *identity* (``ann is u64``
/ the ``_INT_SPECS`` lookup), and constructing a value range-checks it
(``u8(300)`` raises) — though the load-bearing check is still at encode time.

**Type-checker view (friendliness).** To a static type-checker every width is
just ``int`` (and ``f32``/``f64`` are ``float``) — see the ``TYPE_CHECKING``
block below. So widths are freely interchangeable and bare literals are
accepted: passing a ``u64`` (e.g. ``account.lamports``) or a plain ``int`` into
a ``u32`` parameter does **not** highlight red. Distinguishing widths
statically only produced friction (``u32 != u64``) without catching real bugs;
the width that matters is enforced at **runtime**, where ``IntNode.write``
range-checks every value (``value.to_bytes(width, signed=…)`` → ``BorshError``
on overflow / a negative into an unsigned). So the rule is: *accept any int
statically, fail loudly at encode time if it doesn't fit.* (Trade-off, same as
wake: because widths look like ``int`` to the checker, ``u256.max`` resolves at
**runtime** but a strict checker won't statically know the ``.max`` attribute.)

``pubkey`` is the native ``Pubkey`` class itself (resolved by class identity,
32 raw bytes — **not** a ``u256``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .._native import Pubkey

if TYPE_CHECKING:
    # Every width collapses to a plain builtin for the checker (wake does the
    # same): interchangeable, accepts literals, no spurious width errors.
    u8 = int
    u16 = int
    u32 = int
    u64 = int
    u128 = int
    u256 = int
    i8 = int
    i16 = int
    i32 = int
    i64 = int
    i128 = int
    i256 = int
    char = int
    f32 = float
    f64 = float
else:
    class _Int(int):
        """Runtime base for the integer aliases: a real ``int`` subclass that
        range-checks on construction and exposes ``min``/``max``/``bits``/
        ``signed`` as class attributes."""

        __slots__ = ()
        bits = 0
        signed = False
        min = 0
        max = 0

        def __new__(cls, value=0):
            v = super().__new__(cls, value)
            if cls.bits and not (cls.min <= int(v) <= cls.max):
                raise ValueError(
                    f"{cls.__name__}({int(v)}) out of range [{cls.min}, {cls.max}]")
            return v

    def _int_alias(name, bits, signed):
        lo, hi = ((-(1 << (bits - 1)), (1 << (bits - 1)) - 1)
                  if signed else (0, (1 << bits) - 1))
        return type(name, (_Int,), {
            "__slots__": (), "bits": bits, "signed": signed, "min": lo, "max": hi,
        })

    class _Float(float):
        """Runtime base for the float aliases: a ``float`` subclass exposing
        ``bits``/``min``/``max`` (the finite IEEE-754 range)."""

        __slots__ = ()
        bits = 0
        signed = True
        min = 0.0
        max = 0.0

    def _float_alias(name, bits, hi):
        return type(name, (_Float,), {
            "__slots__": (), "bits": bits, "signed": True, "min": -hi, "max": hi,
        })

    # --- unsigned integers ---
    u8 = _int_alias("u8", 8, False)
    u16 = _int_alias("u16", 16, False)
    u32 = _int_alias("u32", 32, False)
    u64 = _int_alias("u64", 64, False)
    u128 = _int_alias("u128", 128, False)
    u256 = _int_alias("u256", 256, False)   # engine-extension only; not Anchor-emittable

    # --- signed integers ---
    i8 = _int_alias("i8", 8, True)
    i16 = _int_alias("i16", 16, True)
    i32 = _int_alias("i32", 32, True)
    i64 = _int_alias("i64", 64, True)
    i128 = _int_alias("i128", 128, True)
    i256 = _int_alias("i256", 256, True)    # engine-extension only; not Anchor-emittable

    # --- floats ---
    f32 = _float_alias("f32", 32, 3.4028234663852886e38)
    f64 = _float_alias("f64", 64, 1.7976931348623157e308)

    # --- char: non-Anchor extension path only (4-byte u32 codepoint) ---
    char = _int_alias("char", 32, False)

#: ``pubkey`` is the native ``Pubkey`` class, re-exported lowercase. It is the
#: SAME object as ``Pubkey`` (``ann is pubkey`` == ``ann is Pubkey``).
pubkey = Pubkey

#: alias identity -> (byte_width, is_signed)
_INT_SPECS = {
    u8: (1, False), u16: (2, False), u32: (4, False), u64: (8, False),
    u128: (16, False), u256: (32, False),
    i8: (1, True), i16: (2, True), i32: (4, True), i64: (8, True),
    i128: (16, True), i256: (32, True),
}
#: alias identity -> byte_width
_FLOAT_SPECS = {f32: 4, f64: 8}


def int_spec(alias):
    """``(n_bytes, is_signed)`` for a known integer alias, else ``None``."""
    return _INT_SPECS.get(alias)


def float_spec(alias):
    """``n_bytes`` for a known float alias, else ``None``."""
    return _FLOAT_SPECS.get(alias)
