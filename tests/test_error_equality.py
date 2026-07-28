"""Regression tests for TransactionFailed.__eq__ / __hash__.

Uses the built-in SPL Token catalog rather than a generated program: the
semantics under test belong to `_errors_base`, and the catalog already supplies
the shape they need — two sibling errors with distinct codes under a per-program
base (`TokenError`), itself under the `ProgramError` category. Generated
`pytypes` errors have the same shape (see `test_gen.py`), so they compare
identically.
"""
from wake_sol import ProgramError, TokenError
from wake_sol._errors_base import TransactionFailed


def test_instance_and_class_forms():
    caught = TokenError.InsufficientFunds(instruction_index=0)
    assert caught == TokenError.InsufficientFunds()   # instance: type + code
    assert caught == TokenError.InsufficientFunds     # class: isinstance
    assert caught == TokenError                       # program base
    assert caught == ProgramError                     # category base
    assert caught == TransactionFailed                # root
    assert TokenError.InsufficientFunds == caught     # reflected


def test_negatives_and_foreign_types():
    caught = TokenError.InsufficientFunds(instruction_index=0)
    assert caught != TokenError.OwnerMismatch()
    assert caught != TokenError.OwnerMismatch
    assert caught != 1 and caught != "x" and caught is not None
    assert not (caught == object)      # non-error class -> NotImplemented -> False


def test_positional_detail_ignored_but_readable():
    a = TokenError.InsufficientFunds(instruction_index=0)
    b = TokenError.InsufficientFunds(instruction_index=3, account_index=2)
    assert a == b                                     # where != which
    assert a.instruction_index == 0 and b.instruction_index == 3


def test_hashable_and_consistent():
    a = TokenError.InsufficientFunds(instruction_index=0)
    b = TokenError.InsufficientFunds()
    assert hash(a) == hash(b)
    assert len({a, b}) == 1
    assert len({a, TokenError.OwnerMismatch()}) == 2
