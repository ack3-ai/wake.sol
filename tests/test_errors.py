"""Network-free tests for the structured-error subsystem (design/pytypes §10.2).

Covers the raise-by-default contract: a failed tx raises the resolved
``TransactionFailed`` subclass (caught by type), carrying the error scalars flat
and the execution receipt as ``.tx``. Splits into: ``build`` resolution + the
exception hierarchy (unit), the real-failure raise path + result↔exception cycle
(a native ``Custom`` error, hermetic), and the gen emitter → ``register_errors``
→ resolver chain (a tiny generated IDL with ``errors[]``).
"""

import importlib
import json
import sys

import pytest

from solana_fuzzer import (
    Account,
    AnchorError,
    ProgramError,
    SolanaError,
    SystemProgramError,
    TokenError,
    TransactionFailed,
    UnknownError,
    may_revert,
    must_revert,
    svm,
)
from solana_fuzzer import _errors as E
from solana_fuzzer._addresses import SYSTEM_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
from solana_fuzzer._gen import run_gen
from solana_fuzzer import Pubkey

_SYSTEM = str(SYSTEM_PROGRAM_ID)
_TOKEN = str(TOKEN_PROGRAM_ID)


# --- build() resolution + hierarchy (unit) --------------------------------- #

def test_native_error_by_name():
    e = E.build(native="InvalidAccountData", instruction_index=1)
    assert isinstance(e, SolanaError.InvalidAccountData)
    assert isinstance(e, SolanaError) and isinstance(e, TransactionFailed)
    assert not isinstance(e, ProgramError) and not isinstance(e, AnchorError)
    assert e.code is None and e.instruction_index == 1


def test_native_param_carrying():
    e = E.build(native="InsufficientFundsForRent", account_index=3)
    assert isinstance(e, SolanaError.InsufficientFundsForRent)
    assert e.account_index == 3


def test_anchor_framework_by_code():
    e = E.build(code=2001)
    assert isinstance(e, AnchorError.ConstraintHasOne)
    assert isinstance(e, AnchorError) and not isinstance(e, ProgramError)
    assert e.code == 2001


def test_unknown_code_is_bare():
    # A code matched by nobody: surfaced bare, never invented (refuse-don't-guess).
    high = E.build(code=59999)          # >= 6000, no program registered
    assert isinstance(high, UnknownError) and high.code == 59999
    low = E.build(code=1)               # < 6000, not Anchor, no program_id -> still bare
    assert isinstance(low, UnknownError) and low.code == 1


# --- builtin (native) program errors, resolved program-scoped -------------- #

def test_builtin_error_resolves_program_scoped():
    # The whole point: the SAME code disambiguates by the failing program.
    sys_err = E.build(code=0, program_id=_SYSTEM, instruction_index=0)
    assert isinstance(sys_err, SystemProgramError.AccountAlreadyInUse)
    assert isinstance(sys_err, SystemProgramError) and isinstance(sys_err, ProgramError)
    assert sys_err.code == 0 and sys_err.instruction_index == 0
    assert sys_err.msg  # carries the on-chain message

    tok_err = E.build(code=0, program_id=_TOKEN)
    assert isinstance(tok_err, TokenError.NotRentExempt)
    assert not isinstance(tok_err, SystemProgramError)   # code 0, but a different program

    # System's Custom(1) is ResultWithNegativeLamports, not Token's InsufficientFunds.
    assert isinstance(E.build(code=1, program_id=_SYSTEM),
                      SystemProgramError.ResultWithNegativeLamports)
    assert isinstance(E.build(code=1, program_id=_TOKEN), TokenError.InsufficientFunds)


def test_token_2022_shares_token_catalog():
    e = E.build(code=17, program_id=str(TOKEN_2022_PROGRAM_ID))
    assert isinstance(e, TokenError.AccountFrozen)


def test_builtin_code_not_mislabeled_as_anchor():
    # A builtin Custom code that happens to land in the Anchor range must NOT be
    # read as an Anchor error — program-scoping resolves it within the builtin only.
    e = E.build(code=2001, program_id=_SYSTEM)   # 2001 == AnchorError.ConstraintHasOne
    assert isinstance(e, UnknownError) and e.code == 2001
    assert not isinstance(e, AnchorError)


def test_unknown_builtin_code_is_bare():
    # A known builtin program but an uncatalogued code: bare, not a wrong guess and
    # not a fall-through to the Anchor/user tables.
    e = E.build(code=999, program_id=_SYSTEM)
    assert isinstance(e, UnknownError) and e.code == 999


def test_unknown_program_id_falls_through_to_code_keyed():
    # program_id that is not a known builtin: unchanged code-keyed resolution.
    some_program = str(Pubkey(bytes([9] * 32)))
    assert isinstance(E.build(code=2001, program_id=some_program), AnchorError.ConstraintHasOne)
    assert isinstance(E.build(code=1, program_id=some_program), UnknownError)


def test_program_error_registration_and_resolution():
    class MyProgError(ProgramError):
        pass

    class DoomViolated(MyProgError):
        code = 61234
        msg = "doom was violated"

    E.register_errors(MyProgError)
    e = E.build(code=61234, instruction_index=0)
    assert isinstance(e, DoomViolated)
    assert isinstance(e, MyProgError) and isinstance(e, ProgramError)
    assert e.code == 61234 and e.msg == "doom was violated"


def test_repr_is_a_clean_oneliner():
    e = E.build(code=2001, instruction_index=2)
    r = repr(e)
    assert "\n" not in r and r.startswith("<ConstraintHasOne")


# --- real failure: raise path + receipt + cycle (hermetic) ----------------- #

def test_failed_tx_raises_with_linked_receipt():
    alice = Account.new()
    bob = Account.new()
    svm.airdrop(alice, 2_000_000)
    with pytest.raises(TransactionFailed) as exc:
        alice.tx(svm.system.transfer(10**18, from_=alice, to=bob))  # overspend -> fails
    e = exc.value
    # System returns Custom(1), attributed to the System program -> named error.
    assert isinstance(e, SystemProgramError.ResultWithNegativeLamports) and e.code == 1
    assert e.instruction_index == 0
    assert e.tx.success is False                 # the failed receipt
    assert e.tx.error is e                        # the result<->exception cycle
    assert len(e.tx.call_trace) >= 1              # trace reachable via .tx
    assert e.call_trace is not None               # ...and via the .call_trace shortcut
    assert len(e.call_trace) == len(e.tx.call_trace)
    assert not bob.exists                         # nothing committed (it raised)


def test_account_already_in_use_resolves_to_system_error():
    # The "already initialized" failure — what Anchor `init` on an existing account
    # produces via a System allocate CPI: InstructionError::Custom(0). Attributed to
    # the System program, it resolves to the named error, not a bare UnknownError.
    payer = Account.new()
    svm.airdrop(payer, 5_000_000_000)
    target = Account.new()
    rent = svm.minimum_balance_for_rent_exemption(64)
    assert payer.tx(svm.system.create_account(
        rent, 64, Pubkey(bytes([3] * 32)), from_=payer, to=target)).success
    # distinct args so the retry isn't deduped tx-level as AlreadyProcessed
    with must_revert(SystemProgramError.AccountAlreadyInUse) as e:
        payer.tx(svm.system.create_account(
            rent, 32, Pubkey(bytes([4] * 32)), from_=payer, to=target))
    assert e.value.code == 0 and not isinstance(e.value, UnknownError)


def test_call_trace_shortcut_without_receipt_raises():
    # An error built directly (no runtime raise) has no linked receipt.
    e = E.build(code=2001)
    assert e.tx is None
    with pytest.raises(ValueError, match="no transaction receipt"):
        _ = e.call_trace


def test_success_returns_result_not_exception():
    alice = Account.new()
    bob = Account.new()
    svm.airdrop(alice, 1_000_000_000)
    # a rent-exempt amount, so the transfer to a fresh account actually succeeds
    res = alice.tx(svm.system.transfer(5_000_000, from_=alice, to=bob))
    assert res.success is True and res.error is None
    assert res.signature is not None


def test_catch_by_base_category():
    alice = Account.new()
    svm.airdrop(alice, 2_000_000)
    # Catching a base category still works (the specific subclass matches it).
    with pytest.raises(TransactionFailed):
        alice.tx(svm.system.transfer(10**18, from_=alice, to=Account.new()))
    with pytest.raises(ProgramError):   # SystemProgramError is a ProgramError
        alice.tx(svm.system.transfer(10**18, from_=alice, to=Account.new()))
    with pytest.raises(SystemProgramError):
        alice.tx(svm.system.transfer(10**18, from_=alice, to=Account.new()))


# --- must_revert / may_revert context managers ----------------------------- #

def _overspend():
    """A tx that always fails: System overspend -> InstructionError Custom(1),
    attributed to the System program -> SystemProgramError.ResultWithNegativeLamports
    (code 1), instruction_index 0."""
    alice = Account.new()
    svm.airdrop(alice, 2_000_000)
    alice.tx(svm.system.transfer(10**18, from_=alice, to=Account.new()))


def test_must_revert_by_type_exposes_value():
    with must_revert(SystemProgramError.ResultWithNegativeLamports) as e:
        _overspend()
    assert isinstance(e.value, SystemProgramError.ResultWithNegativeLamports)
    assert e.value.code == 1
    assert e.value.tx.success is False and e.value.tx.error is e.value


def test_must_revert_by_int_code():
    # bare int matches the raised error's .code, program-agnostic
    with must_revert(1) as e:
        _overspend()
    assert e.value.code == 1


def test_must_revert_by_base_category():
    with must_revert(TransactionFailed):
        _overspend()
    with must_revert():  # no args == "must revert with anything"
        _overspend()


def test_must_revert_raises_if_body_succeeds():
    alice = Account.new()
    svm.airdrop(alice, 1_000_000_000)
    with pytest.raises(AssertionError, match="succeeded"):
        with must_revert(UnknownError):
            alice.tx(svm.system.transfer(5_000_000, from_=alice, to=Account.new()))


def test_must_revert_wrong_type_propagates():
    # a revert that doesn't match the expected type bubbles out unchanged
    with pytest.raises(SystemProgramError.ResultWithNegativeLamports):
        with must_revert(AnchorError.ConstraintHasOne):
            _overspend()


def test_must_revert_wrong_code_propagates():
    with pytest.raises(SystemProgramError.ResultWithNegativeLamports):
        with must_revert(6100):
            _overspend()


def test_may_revert_allows_success():
    alice = Account.new()
    svm.airdrop(alice, 1_000_000_000)
    with may_revert(UnknownError) as e:
        alice.tx(svm.system.transfer(5_000_000, from_=alice, to=Account.new()))
    assert e.value is None  # nothing reverted


def test_may_revert_captures_matching_revert():
    with may_revert(SystemProgramError.ResultWithNegativeLamports) as e:
        _overspend()
    assert isinstance(e.value, SystemProgramError.ResultWithNegativeLamports)


def test_may_revert_wrong_type_propagates():
    with pytest.raises(SystemProgramError.ResultWithNegativeLamports):
        with may_revert(AnchorError.ConstraintHasOne):
            _overspend()


def test_revert_cm_rejects_bad_args():
    for bad in ("some string", ValueError, True):
        with pytest.raises(TypeError):
            with must_revert(bad):
                pass


# --- gen emitter -> register_errors -> resolver (hermetic) ----------------- #

@pytest.fixture
def tiny_program(tmp_path):
    """Generate a minimal program module from an IDL carrying ``errors[]`` and
    import it (which registers its error classes)."""
    addr = str(Pubkey(bytes([7] * 32)))
    idl = {
        "address": addr,
        "metadata": {"name": "tiny_prog"},
        "instructions": [],
        "errors": [
            {"code": 6100, "name": "TooSmall", "msg": "value is too small"},
            {"code": 6101, "name": "TooBig"},  # no msg
        ],
    }
    root = tmp_path / "gen"
    idl_dir = root / "idls"
    idl_dir.mkdir(parents=True)
    (idl_dir / f"{addr}.json").write_text(json.dumps(idl))
    assert run_gen(target_idls=(str(idl_dir),), dep_idls=("/nonexistent",),
                   out=str(root / "pytypes")) == 0
    sys.path.insert(0, str(root))
    for n in [n for n in sys.modules if n == "pytypes" or n.startswith("pytypes.")]:
        del sys.modules[n]
    try:
        yield importlib.import_module("pytypes").tiny_prog
    finally:
        sys.path.remove(str(root))
        for n in [n for n in sys.modules if n == "pytypes" or n.startswith("pytypes.")]:
            del sys.modules[n]


def test_generated_error_classes_and_hierarchy(tiny_program):
    assert issubclass(tiny_program.TinyProgError, ProgramError)
    assert issubclass(tiny_program.TooSmall, tiny_program.TinyProgError)
    assert tiny_program.TooSmall.code == 6100
    assert tiny_program.TooSmall.msg == "value is too small"
    assert tiny_program.TooBig.code == 6101
    assert tiny_program.TooBig.msg is None


def test_generated_errors_resolve_via_build(tiny_program):
    # After import (register_errors ran), build() resolves the codes to the
    # generated classes — exactly what the Rust raise path does on failure.
    e = E.build(code=6100, instruction_index=1)
    assert isinstance(e, tiny_program.TooSmall)
    assert e.code == 6100 and e.instruction_index == 1
    assert isinstance(e, tiny_program.TinyProgError) and isinstance(e, ProgramError)
