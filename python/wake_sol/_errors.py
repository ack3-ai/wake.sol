"""Structured transaction errors (user-facing guide: ``docs/11-errors.md``).

A failed transaction **raises** — the raised object *is* the specific error, a
subclass of :class:`TransactionFailed`. Catch it by type::

    with must_fail(MyProgram.TooSmall):              # exact
        payer.tx(ix)
    with must_fail(AnchorError.ConstraintHasOne):    # a framework constraint
        payer.tx(ix)
    with must_fail(MyProgram.Error):                 # any error from that program
        payer.tx(ix)

Hierarchy (the vocabulary you pass to ``must_fail`` / ``may_fail`` / catch)::

    TransactionFailed                     # any failed tx
    ├─ SolanaError.<Variant>              # runtime native errors (name-keyed)
    ├─ AnchorError.<Name>                 # framework codes (< 6000), bundled
    ├─ ProgramError                       # base for program-defined codes
    │   ├─ <Program>Error.<Name>          # generated per IDL errors[] (>= 6000)
    │   ├─ SystemProgramError.<Name>      # builtin: native System program
    │   └─ TokenError.<Name>              # builtin: SPL Token / Token-2022
    └─ UnknownError                       # a Custom(code) matched by nobody

The base classes live in :mod:`._errors_base`; the concrete leaf classes are real
``class`` statements in the generated :mod:`._errors_catalog` package (so type
checkers see ``AnchorError.ConstraintSeeds`` etc.). This module attaches those
leaves onto their bases at import (the ``setattr`` loop below) and owns the
raise-path resolver :func:`build`.

The raised instance carries the error-intrinsic scalars flat (``code``,
``instruction_index``, ``account_index``) and links the execution receipt as
``.tx`` (a ``TransactionResult``): ``ex.tx.logs``, ``ex.tx.call_trace`` — the last
also reachable as the ``ex.call_trace`` shortcut.
Matching is by *type*. User-program and Anchor codes are resolved *code-keyed* and
CPI-depth-independent (the ``Custom`` code bubbles up unchanged); two user programs
sharing a code resolve to whichever is registered last. Builtin (native) programs —
System and SPL Token/Token-2022 — are the exception: their error enums start at 0,
so they collide with each other and fall inside the Anchor range. A ``Custom`` code
from a builtin is therefore resolved *program-scoped*, attributed to the program
that produced it (recovered from the call trace and passed to ``build`` as
``program_id``), so System's ``Custom(0)`` and Token's ``Custom(0)`` stay distinct.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from ._addresses import SYSTEM_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
from ._errors_base import (
    AnchorError as AnchorError,
    ProgramError as ProgramError,
    SolanaError as SolanaError,
    SystemProgramError as SystemProgramError,
    TokenError as TokenError,
    TransactionFailed as TransactionFailed,
    UnknownError as UnknownError,
)
from ._errors_catalog.anchor import BY_CODE as _ANCHOR_BY_CODE
from ._errors_catalog.solana import BY_NAME as _SOLANA_BY_NAME
from ._errors_catalog.system import BY_CODE as _SYSTEM_BY_CODE
from ._errors_catalog.token import BY_CODE as _TOKEN_BY_CODE

ANCHOR_USER_ERROR_OFFSET = 6000

# --- attach the generated leaves onto their bases -------------------------- #
def _attach_leaves() -> None:
    """Bind each generated leaf as an attribute of its base — e.g.
    ``AnchorError.ConstraintSeeds`` — the runtime side of the ``if TYPE_CHECKING:``
    member declarations in ``_errors_base``. Same names can coexist across catalogs
    (e.g. Solana/Token ``InsufficientFunds``): distinct classes on distinct bases,
    never sharing a namespace."""
    for base, leaves in (
        (SolanaError, _SOLANA_BY_NAME.values()),
        (AnchorError, _ANCHOR_BY_CODE.values()),
        (SystemProgramError, _SYSTEM_BY_CODE.values()),
        (TokenError, _TOKEN_BY_CODE.values()),
    ):
        for cls in leaves:
            setattr(base, cls.__name__, cls)


_attach_leaves()

# Originating-program address (base58) -> that program's {code: class} table.
_BUILTIN_BY_PROGRAM: dict[str, dict[int, type]] = {
    str(SYSTEM_PROGRAM_ID): _SYSTEM_BY_CODE,
    str(TOKEN_PROGRAM_ID): _TOKEN_BY_CODE,
    str(TOKEN_2022_PROGRAM_ID): _TOKEN_BY_CODE,  # base variants shared with SPL Token
}


# --- per-program registry (populated by generated modules at import) ------- #
_PROGRAM_BY_CODE: dict[int, type] = {}


def register_errors(base_cls: type) -> None:
    """Register a generated ``<Program>Error`` base so its ``Custom`` codes
    resolve to their specific subclasses on the raise path. Last registration
    wins on a code collision — so re-importing a program refreshes its classes,
    and (rarely) two programs sharing a code resolve to the last imported."""
    for sub in base_cls.__subclasses__():
        code = getattr(sub, "code", None)
        if code is not None:
            _PROGRAM_BY_CODE[code] = sub


def build(code=None, native=None, instruction_index=None, account_index=None,
          program_id=None):
    """Resolve raw error fields to the most specific ``TransactionFailed``
    subclass and instantiate it. Called by the Rust failure path; the runtime
    sets ``.tx`` afterwards. Resolution:

    * a native name → ``SolanaError`` subclass;
    * a ``Custom`` code from a known builtin program (``program_id`` is System /
      SPL Token / Token-2022) → that program's table, program-scoped — an
      uncatalogued builtin code stays ``UnknownError`` rather than being misread as
      an Anchor/user code;
    * otherwise per-program table → Anchor table (< 6000) → ``UnknownError``.

    ``program_id`` is the base58 of the program the runtime attributed the failure
    to; ``None`` (e.g. an error built by hand, or an untraced path) falls straight
    through to the code-keyed resolution."""
    if native is not None:
        cls = _SOLANA_BY_NAME.get(native, SolanaError)
        return cls(instruction_index=instruction_index, account_index=account_index)
    if code is not None:
        builtin = _BUILTIN_BY_PROGRAM.get(program_id) if program_id is not None else None
        if builtin is not None:
            cls = builtin.get(code)  # program-scoped: no fall-through to Anchor/user tables
            if cls is None:
                return UnknownError(code=code, instruction_index=instruction_index,
                                    account_index=account_index)
            return cls(code=code, instruction_index=instruction_index,
                       account_index=account_index)
        cls = _PROGRAM_BY_CODE.get(code)
        if cls is None and code < ANCHOR_USER_ERROR_OFFSET:
            cls = _ANCHOR_BY_CODE.get(code)
        if cls is None:
            return UnknownError(code=code, instruction_index=instruction_index,
                                account_index=account_index)
        return cls(code=code, instruction_index=instruction_index,
                   account_index=account_index)
    return TransactionFailed(instruction_index=instruction_index,
                             account_index=account_index)


# --- failure context managers ---------------------------------------------- #
class ExceptionWrapper:
    """Handle yielded by :func:`must_fail` / :func:`may_fail`; its ``value`` is
    the caught :class:`TransactionFailed` (``None`` if the body succeeded)."""

    value: TransactionFailed | None = None


def _split_expected(expected: tuple) -> tuple[tuple[type, ...], frozenset[int]]:
    """Partition the CM arguments into exception *types* (matched by ``isinstance``)
    and bare int *codes* (matched against the raised error's ``.code``). No args
    means "any failed tx". Anything else is refused rather than silently ignored."""
    if not expected:
        return (TransactionFailed,), frozenset()
    types: list[type] = []
    codes: set[int] = set()
    for x in expected:
        if isinstance(x, bool):  # bool is an int subclass; not a valid error code
            raise TypeError(f"expected a TransactionFailed subclass or int code, got {x!r}")
        if isinstance(x, type) and issubclass(x, TransactionFailed):
            types.append(x)
        elif isinstance(x, int):
            codes.add(x)
        else:
            raise TypeError(
                "must_fail/may_fail takes TransactionFailed subclasses or int "
                f"error codes; got {x!r}"
            )
    return tuple(types), frozenset(codes)


def _matches(e: TransactionFailed, types: tuple[type, ...], codes: frozenset[int]) -> bool:
    return isinstance(e, types) or (e.code is not None and e.code in codes)


@contextmanager
def must_fail(*expected: type | int) -> Iterator[ExceptionWrapper]:
    """Assert the body fails with one of ``expected`` (a ``TransactionFailed``
    subclass, matched by type, or a bare int matched against the error's ``code``).
    No args means "must fail with anything". Raises ``AssertionError`` if the
    body succeeds; re-raises a failure that doesn't match. The caught exception is
    available as the wrapper's ``value``::

        with must_fail(AnchorError.ConstraintHasOne):
            payer.tx(ix)
        with must_fail(6100) as e:        # any program/Anchor code == 6100
            payer.tx(ix)
        assert e.value.instruction_index == 0
    """
    types, codes = _split_expected(expected)
    w = ExceptionWrapper()
    try:
        yield w
    except TransactionFailed as e:
        if not _matches(e, types, codes):
            raise
        w.value = e
        return
    raise AssertionError(f"expected a failure matching {list(expected) or [TransactionFailed]}, "
                         "but the transaction succeeded")


@contextmanager
def may_fail(*expected: type | int) -> Iterator[ExceptionWrapper]:
    """Like :func:`must_fail`, but the body is allowed to succeed. If it *does*
    fail, the error must match one of ``expected`` (else it re-raises) — useful
    for fuzzing inputs that may or may not fail. The caught error (or ``None``) is
    the wrapper's ``value``."""
    types, codes = _split_expected(expected)
    w = ExceptionWrapper()
    try:
        yield w
    except TransactionFailed as e:
        if not _matches(e, types, codes):
            raise
        w.value = e
