"""Python testing/fuzzing harness for Solana programs, backed by litesvm."""

import random as _random

from rich.console import Console as _Console

# A markup-disabled Rich console, re-exported as `print` so tests can
# `from solana_fuzzer import print` and have `__rich__` objects (notably
# CallTrace) render colored. Markup is OFF by default — a stray "[..]" in a
# label / log / arbitrary string would otherwise be parsed as Rich markup and
# can raise. Pass markup=True explicitly to a call to opt back in.
_console = _Console(markup=False)
print = _console.print  # noqa: A001  (intentional builtin shadow for tests)

from ._native import (
    Account,
    AccountMeta,
    CallTrace,
    Clock,
    EpochRewards,
    EpochSchedule,
    Instruction,
    LiteSVM,
    Pubkey,
    Rent,
    TracedInstruction,
    TransactionResult,
    readonly,
    signer,
    writable,
    writable_signer,
)
from ._native import default_svm as _default_svm
from . import _codec as _codec  # noqa: F401
from ._codec import *  # noqa: F401,F403  (width aliases u8..u256/i8..i256/f32/f64/char/pubkey,
#                                          Opt/COption/BorshEnum/variant/BorshStruct, encode/decode,
#                                          MetaLike, the metadata records + decorators, etc.)
from . import _addresses as _addresses  # noqa: F401
from ._addresses import *  # noqa: F401,F403  (SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID, RENT_SYSVAR, …)
from . import _labels as _labels  # noqa: F401  (label store; Account.label delegates here)
from . import _programs as _programs  # noqa: F401  (registers built-in decoders)
from . import call_trace as _call_trace  # noqa: F401  (renderer used by CallTrace.__rich__)
from ._interface import DecodedInstruction, ReturnDataError, decode_instruction
from .fuzzing import FuzzTest, flow, invariant
from ._errors import (
    AnchorError,
    ProgramError,
    SolanaError,
    SystemProgramError,
    TokenError,
    TransactionFailed,
    UnknownError,
    may_revert,
    must_revert,
    register_errors,
)

#: The process-global SVM, created once in Rust. This is the implicit target
#: for ``Account(...)`` when no ``svm=`` is passed. Make separate chains with
#: ``LiteSVM()``.
svm = _default_svm()

#: Process-global random source. The ``solana-fuzzer test`` CLI reseeds this
#: deterministically before every test (base seed + test node id), so test
#: randomness is reproducible regardless of run order.
random = _random.Random()

__all__ = [
    "Account",
    "AccountMeta",
    "AnchorError",
    "CallTrace",
    "Clock",
    "DecodedInstruction",
    "EpochRewards",
    "EpochSchedule",
    "FuzzTest",
    "Instruction",
    "LiteSVM",
    "ProgramError",
    "Pubkey",
    "Rent",
    "ReturnDataError",
    "SolanaError",
    "SystemProgramError",
    "TokenError",
    "TracedInstruction",
    "TransactionFailed",
    "TransactionResult",
    "UnknownError",
    "decode_instruction",
    "flow",
    "invariant",
    "may_revert",
    "must_revert",
    "register_errors",
    "print",
    "random",
    "readonly",
    "signer",
    "svm",
    "writable",
    "writable_signer",
    *_codec.__all__,   # width aliases + carriers + codec/builder public surface
    *_addresses.__all__,   # well-known program / sysvar Pubkey constants
]
