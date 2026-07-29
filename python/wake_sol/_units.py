"""SOL → lamports conversion.

Every amount in the harness is raw lamports (`u64`), which is what the runtime
speaks; this is the one place that knows the decimal scale.

`sol()` converts exactly or not at all: an amount that is not a whole number of
lamports raises, and `float` is refused outright. A binary float does not hold a
decimal SOL amount exactly — multiplying one by 10**9 truncates ~2% of ordinary
three-decimal amounts by a lamport, so `1.001` would fund 1000999999 — and
rounding the difference away would quietly change the value a balance assertion
is about. Fractions therefore arrive as strings or `Decimal`, whose digits are
the digits that were written.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

#: Lamports in one SOL. The chain's unit is the lamport; SOL is a display scale.
LAMPORTS_PER_SOL = 1_000_000_000

_SCALE = Decimal(LAMPORTS_PER_SOL)


def sol(amount: int | str | Decimal) -> int:
    """Convert `amount` SOL to lamports.

    ``svm.airdrop(alice, sol(1))`` instead of ``svm.airdrop(alice, 1_000_000_000)``.
    Fractions go in as strings — ``sol("1.5")``, ``sol("0.000000001")`` (one
    lamport) — or as a `Decimal`.

    Raises `ValueError` unless the amount lands on a whole number of lamports:
    the chain has no finer unit, and rounding to reach one would silently change
    the amount. Raises `TypeError` for a `float`, which cannot carry a decimal
    amount exactly (see the module docstring).

    Negative amounts are allowed: this converts units, it does not validate, and
    an expected *delta* is often negative. Whatever consumes the lamports
    (`airdrop`, a `u64` builder arg) enforces its own range.
    """
    if isinstance(amount, float):
        raise TypeError(
            f"sol() does not take floats — {amount!r} is not exactly the decimal "
            f'amount it reads as. Quote it: sol("{amount}"), pass a Decimal, or '
            f"give the lamport count directly."
        )

    try:
        as_decimal = Decimal(amount)
    except (InvalidOperation, ValueError, TypeError) as e:
        raise ValueError(f"cannot read {amount!r} as an amount of SOL") from e

    if not as_decimal.is_finite():
        raise ValueError(f"{amount!r} is not a finite amount of SOL")

    lamports = as_decimal * _SCALE
    if lamports != lamports.to_integral_value():
        raise ValueError(
            f"{amount!r} SOL is {lamports} lamports, not a whole number of them — "
            f"one lamport is 0.000000001 SOL, the smallest amount that exists"
        )
    return int(lamports)


__all__ = ["LAMPORTS_PER_SOL", "sol"]
