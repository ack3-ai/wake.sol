"""Python testing/fuzzing harness for Solana programs, backed by litesvm."""

import random as _random

from ordered_set import OrderedSet as OrderedSet
from rich.console import Console as _Console

# A markup-disabled Rich console, re-exported as `print` so tests can
# `from wake_sol import print` and have `__rich__` objects (notably
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
    FeeStructure,
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
from . import _units as _units  # noqa: F401
from ._units import LAMPORTS_PER_SOL, sol
from . import _labels as _labels  # noqa: F401  (label store; Account.label delegates here)
from . import _programs as _programs  # noqa: F401  (registers built-in decoders)
from . import ed25519 as ed25519  # noqa: F401  (precompile module; registers decoder)
from . import secp256k1 as secp256k1  # noqa: F401  (precompile module; registers decoder)
from . import secp256r1 as secp256r1  # noqa: F401  (precompile module; registers decoder)
from ._precompiles import Inline, Offsets, PrecompileInstruction, Ref, SignedMessage
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

#: Process-global random source. The pytest plugin (active under plain
#: ``pytest`` as well as ``wake-sol test``) reseeds this deterministically
#: before every test (base seed + test node id), so test randomness is
#: reproducible regardless of run order.
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
    "FeeStructure",
    "FuzzTest",
    "Instruction",
    "LAMPORTS_PER_SOL",
    "LiteSVM",
    "Inline",
    "Offsets",
    # A plain `set` iterates in hash order and `Account` hashes differ every run,
    # so iterating one in a flow or invariant breaks seed reproducibility. Use
    # this instead — it is the reason it is exported from here.
    "OrderedSet",
    "PrecompileInstruction",
    "Ref",
    "SignedMessage",
    "ed25519",
    "secp256k1",
    "secp256r1",
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
    "sol",
    "svm",
    "writable",
    "writable_signer",
    # `_codec` also exports a `ProgramError` — the inert IDL-metadata record, not
    # the exception base. The `._errors` import above (which comes last) is what
    # `wake_sol.ProgramError` binds to; drop the codec name so it isn't listed
    # twice. Reach the metadata record as `wake_sol._codec.ProgramError`.
    *(n for n in _codec.__all__ if n != "ProgramError"),
    *_addresses.__all__,   # well-known program / sysvar Pubkey constants
]
