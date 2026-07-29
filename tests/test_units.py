"""`sol()` — SOL to lamports, exactly or not at all."""

from decimal import Decimal

import pytest

from wake_sol import LAMPORTS_PER_SOL, Account, sol, svm


def test_whole_amounts():
    assert sol(1) == LAMPORTS_PER_SOL == 1_000_000_000
    assert sol(0) == 0
    assert sol(2) == 2 * LAMPORTS_PER_SOL
    assert sol(1_000_000) == 10**15


def test_fractional_amounts_as_str_and_decimal():
    assert sol("1.5") == 1_500_000_000
    assert sol("0.5") == 500_000_000
    assert sol("0.000000001") == 1                     # one lamport
    assert sol(Decimal("2.5")) == 2_500_000_000
    assert sol(Decimal("0.000000001")) == 1
    # a magnitude no float holds to lamport precision
    assert sol("9500000.000000001") == 9_500_000_000_000_001


def test_every_millisol_converts_exactly():
    for i in range(1000):
        assert sol(f"1.{i:03d}") == 1_000_000_000 + i * 1_000_000


def test_floats_are_refused():
    """A binary float is not the decimal amount it reads as; the message says so
    and names the spelling that works."""
    for bad in (1.5, 1.001, 0.1 + 0.2, 1e-12, float("nan")):
        with pytest.raises(TypeError, match="does not take floats"):
            sol(bad)
    with pytest.raises(TypeError, match=r'sol\("1.5"\)'):
        sol(1.5)


def test_sub_lamport_amounts_raise():
    """No rounding: the chain has no unit finer than a lamport, and quietly
    dropping the remainder would change what an assertion is about."""
    for bad in ("0.0000000004", "0.0000000015", "1.0000000004", Decimal("1e-12")):
        with pytest.raises(ValueError, match="not a whole number of them"):
            sol(bad)


def test_negative_amounts_are_allowed():
    """A unit conversion, not a validator — an expected delta is often negative."""
    assert sol(-1) == -LAMPORTS_PER_SOL
    assert sol("-0.5") == -500_000_000


def test_bad_amounts_raise():
    for bad in ("", "1 SOL", "abc", None, object()):
        with pytest.raises(ValueError, match="as an amount of SOL"):
            sol(bad)
    for bad in ("nan", "inf", Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError, match="not a finite amount"):
            sol(bad)


def test_reaches_the_svm():
    alice = Account.new()
    svm.airdrop(alice, sol("1.5"))
    assert alice.lamports == 1_500_000_000
    alice.lamports -= sol("0.5")
    assert alice.lamports == sol(1)
