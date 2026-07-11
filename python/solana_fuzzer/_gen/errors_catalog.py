"""Generator for the built-in error catalogs (native / Anchor / System / Token).

The four data tables below are the **single source of truth** for the bundled
error classes. Running this module emits, deterministically:

  * ``solana_fuzzer/_errors_catalog/{solana,anchor,system,token}.py`` — one real
    ``class`` statement per error (statically visible to type checkers, unlike a
    runtime ``type()`` call), plus the ``BY_NAME`` / ``BY_CODE`` lookup maps that
    ``_errors.build()`` consumes; and
  * the ``if TYPE_CHECKING:`` member-import blocks inside ``_errors_base.py``
    (between the ``>>>GEN:<cat>>>>`` markers) that give ``AnchorError.ConstraintSeeds``
    et al. their static type, while the runtime attaches them via ``setattr`` in
    ``_errors.py``.

Why generated rather than hand-written: the catalogs are ~200 near-identical
classes mirroring upstream enums, and the class list, the lookup maps, and the
member blocks must never drift from one another. This mirrors the house style
already used for per-program IDL errors in ``_gen/emit.py``.

Run ``python -m solana_fuzzer._gen.errors_catalog`` to regenerate, or with
``--check`` to exit non-zero if the checked-in output is stale (CI drift gate).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Sequence

# --------------------------------------------------------------------------- #
# data tables (single source of truth)
# --------------------------------------------------------------------------- #

# --- native runtime catalog (deduped InstructionError ∪ TransactionError) --- #
# ``AccountBorrowOutstanding`` exists at both levels; it appears once (matching
# is by name, and both levels emit the same name — level is in .instruction_index).
_SOLANA_NAMES = (
    # instruction-level (solana_instruction_error::InstructionError, minus Custom)
    "GenericError", "InvalidArgument", "InvalidInstructionData", "InvalidAccountData",
    "AccountDataTooSmall", "InsufficientFunds", "IncorrectProgramId",
    "MissingRequiredSignature", "AccountAlreadyInitialized", "UninitializedAccount",
    "UnbalancedInstruction", "ModifiedProgramId", "ExternalAccountLamportSpend",
    "ExternalAccountDataModified", "ReadonlyLamportChange", "ReadonlyDataModified",
    "DuplicateAccountIndex", "ExecutableModified", "RentEpochModified",
    "NotEnoughAccountKeys", "AccountDataSizeChanged", "AccountNotExecutable",
    "AccountBorrowFailed", "AccountBorrowOutstanding", "DuplicateAccountOutOfSync",
    "InvalidError", "ExecutableDataModified", "ExecutableLamportChange",
    "ExecutableAccountNotRentExempt", "UnsupportedProgramId", "CallDepth",
    "MissingAccount", "ReentrancyNotAllowed", "MaxSeedLengthExceeded", "InvalidSeeds",
    "InvalidRealloc", "ComputationalBudgetExceeded", "PrivilegeEscalation",
    "ProgramEnvironmentSetupFailure", "ProgramFailedToComplete", "ProgramFailedToCompile",
    "Immutable", "IncorrectAuthority", "BorshIoError", "AccountNotRentExempt",
    "InvalidAccountOwner", "ArithmeticOverflow", "UnsupportedSysvar", "IllegalOwner",
    "MaxAccountsDataAllocationsExceeded", "MaxAccountsExceeded",
    "MaxInstructionTraceLengthExceeded", "BuiltinProgramsMustConsumeComputeUnits",
    # transaction-level (solana_transaction_error::TransactionError; AccountBorrowOutstanding deduped)
    "AccountInUse", "AccountLoadedTwice", "AccountNotFound", "ProgramAccountNotFound",
    "InsufficientFundsForFee", "InvalidAccountForFee", "AlreadyProcessed",
    "BlockhashNotFound", "CallChainTooDeep", "MissingSignatureForFee",
    "InvalidAccountIndex", "SignatureFailure", "InvalidProgramForExecution",
    "SanitizeFailure", "ClusterMaintenance", "WouldExceedMaxBlockCostLimit",
    "UnsupportedVersion", "InvalidWritableAccount", "WouldExceedMaxAccountCostLimit",
    "WouldExceedAccountDataBlockLimit", "TooManyAccountLocks", "AddressLookupTableNotFound",
    "InvalidAddressLookupTableOwner", "InvalidAddressLookupTableData",
    "InvalidAddressLookupTableIndex", "InvalidRentPayingAccount",
    "WouldExceedMaxVoteCostLimit", "WouldExceedAccountDataTotalLimit",
    "DuplicateInstruction", "InsufficientFundsForRent", "MaxLoadedAccountsDataSizeExceeded",
    "InvalidLoadedAccountsDataSizeLimit", "ResanitizationNeeded",
    "ProgramExecutionTemporarilyRestricted", "UnbalancedTransaction",
    "ProgramCacheHitMaxLimit", "CommitCancelled",
)

# --- Anchor framework catalog (bundled; append-only stable across versions) - #
_ANCHOR_TABLE = (
    ("InstructionMissing", 100), ("InstructionFallbackNotFound", 101),
    ("InstructionDidNotDeserialize", 102), ("InstructionDidNotSerialize", 103),
    ("IdlInstructionStub", 1000), ("IdlInstructionInvalidProgram", 1001),
    ("IdlAccountNotEmpty", 1002), ("EventInstructionStub", 1500),
    ("ConstraintMut", 2000), ("ConstraintHasOne", 2001), ("ConstraintSigner", 2002),
    ("ConstraintRaw", 2003), ("ConstraintOwner", 2004), ("ConstraintRentExempt", 2005),
    ("ConstraintSeeds", 2006), ("ConstraintExecutable", 2007), ("ConstraintState", 2008),
    ("ConstraintAssociated", 2009), ("ConstraintAssociatedInit", 2010),
    ("ConstraintClose", 2011), ("ConstraintAddress", 2012), ("ConstraintZero", 2013),
    ("ConstraintTokenMint", 2014), ("ConstraintTokenOwner", 2015),
    ("ConstraintMintMintAuthority", 2016), ("ConstraintMintFreezeAuthority", 2017),
    ("ConstraintMintDecimals", 2018), ("ConstraintSpace", 2019),
    ("ConstraintAccountIsNone", 2020), ("ConstraintTokenTokenProgram", 2021),
    ("ConstraintMintTokenProgram", 2022), ("ConstraintAssociatedTokenTokenProgram", 2023),
    ("ConstraintMintGroupPointerExtension", 2024),
    ("ConstraintMintGroupPointerExtensionAuthority", 2025),
    ("ConstraintMintGroupPointerExtensionGroupAddress", 2026),
    ("ConstraintMintGroupMemberPointerExtension", 2027),
    ("ConstraintMintGroupMemberPointerExtensionAuthority", 2028),
    ("ConstraintMintGroupMemberPointerExtensionMemberAddress", 2029),
    ("ConstraintMintMetadataPointerExtension", 2030),
    ("ConstraintMintMetadataPointerExtensionAuthority", 2031),
    ("ConstraintMintMetadataPointerExtensionMetadataAddress", 2032),
    ("ConstraintMintCloseAuthorityExtension", 2033),
    ("ConstraintMintCloseAuthorityExtensionAuthority", 2034),
    ("ConstraintMintPermanentDelegateExtension", 2035),
    ("ConstraintMintPermanentDelegateExtensionDelegate", 2036),
    ("ConstraintMintTransferHookExtension", 2037),
    ("ConstraintMintTransferHookExtensionAuthority", 2038),
    ("ConstraintMintTransferHookExtensionProgramId", 2039),
    ("RequireViolated", 2500), ("RequireEqViolated", 2501), ("RequireKeysEqViolated", 2502),
    ("RequireNeqViolated", 2503), ("RequireKeysNeqViolated", 2504),
    ("RequireGtViolated", 2505), ("RequireGteViolated", 2506),
    ("AccountDiscriminatorAlreadySet", 3000), ("AccountDiscriminatorNotFound", 3001),
    ("AccountDiscriminatorMismatch", 3002), ("AccountDidNotDeserialize", 3003),
    ("AccountDidNotSerialize", 3004), ("AccountNotEnoughKeys", 3005),
    ("AccountNotMutable", 3006), ("AccountOwnedByWrongProgram", 3007),
    ("InvalidProgramId", 3008), ("InvalidProgramExecutable", 3009),
    ("AccountNotSigner", 3010), ("AccountNotSystemOwned", 3011),
    ("AccountNotInitialized", 3012), ("AccountNotProgramData", 3013),
    ("AccountNotAssociatedTokenAccount", 3014), ("AccountSysvarMismatch", 3015),
    ("AccountReallocExceedsLimit", 3016), ("AccountDuplicateReallocs", 3017),
    ("DeclaredProgramIdMismatch", 4100), ("TryingToInitPayerAsProgramAccount", 4101),
    ("InvalidNumericConversion", 4102), ("Deprecated", 5000),
)

# (code, name, message) — messages mirror the on-chain enums.
_SYSTEM_ERRORS = (
    (0, "AccountAlreadyInUse", "an account with the same address already exists"),
    (1, "ResultWithNegativeLamports", "account does not have enough SOL to perform the operation"),
    (2, "InvalidProgramId", "cannot assign account to this program id"),
    (3, "InvalidAccountDataLength", "cannot allocate account data of this length"),
    (4, "MaxSeedLengthExceeded", "length of requested seed is too long"),
    (5, "AddressWithSeedMismatch", "provided address does not match the address derived from seed"),
    (6, "NonceNoRecentBlockhashes", "advancing stored nonce requires a populated RecentBlockhashes sysvar"),
    (7, "NonceBlockhashNotExpired", "stored nonce is still in recent_blockhashes"),
    (8, "NonceUnexpectedBlockhashValue", "specified nonce does not match stored nonce"),
)

# SPL Token classic variants (0..=19); Token-2022 shares these and adds more.
_TOKEN_ERRORS = (
    (0, "NotRentExempt", "lamport balance below rent-exempt threshold"),
    (1, "InsufficientFunds", "insufficient funds"),
    (2, "InvalidMint", "invalid mint"),
    (3, "MintMismatch", "account not associated with this mint"),
    (4, "OwnerMismatch", "owner does not match"),
    (5, "FixedSupply", "fixed supply"),
    (6, "AlreadyInUse", "already in use"),
    (7, "InvalidNumberOfProvidedSigners", "invalid number of provided signers"),
    (8, "InvalidNumberOfRequiredSigners", "invalid number of required signers"),
    (9, "UninitializedState", "state is uninitialized"),
    (10, "NativeNotSupported", "instruction does not support native tokens"),
    (11, "NonNativeHasBalance", "non-native account can only be closed if its balance is zero"),
    (12, "InvalidInstruction", "invalid instruction"),
    (13, "InvalidState", "state is invalid for requested operation"),
    (14, "Overflow", "operation overflowed"),
    (15, "AuthorityTypeNotSupported", "account does not support specified authority type"),
    (16, "MintCannotFreeze", "this token mint cannot freeze accounts"),
    (17, "AccountFrozen", "account is frozen"),
    (18, "MintDecimalsMismatch", "the provided decimals value different from the mint decimals"),
    (19, "NonNativeNotSupported", "instruction does not support non-native tokens"),
)

# --------------------------------------------------------------------------- #
# emit
# --------------------------------------------------------------------------- #

_HEADER = "# @generated by solana_fuzzer._gen.errors_catalog — do not edit.\n"


def _catalog_module(base: str, doc: str, classes: list[str], map_name: str,
                    key_type: str, entries: Sequence[tuple[object, str]]) -> str:
    """Assemble one catalog module: header + imports + one class per entry +
    the ``{key: class}`` lookup map. ``entries`` is ``[(key, class_name), ...]``."""
    blocks = [
        f'{_HEADER}"""{doc}\n\nGenerated from ``solana_fuzzer._gen.errors_catalog`` — do not edit."""',
        "from __future__ import annotations",
        f"from .._errors_base import {base}",
        *classes,
    ]
    map_lines = [f"{map_name}: dict[{key_type}, type[{base}]] = {{"]
    for key, name in entries:
        map_lines.append(f"    {key!r}: {name},")
    map_lines.append("}")
    blocks.append("\n".join(map_lines))
    return "\n\n\n".join(blocks) + "\n"


def _emit_solana() -> str:
    classes = [f"class {n}(SolanaError):\n    pass" for n in _SOLANA_NAMES]
    entries = [(n, n) for n in _SOLANA_NAMES]
    return _catalog_module(
        "SolanaError",
        "Native Solana runtime error catalog (name-keyed; no numeric code).",
        classes, "BY_NAME", "str", entries,
    )


def _emit_anchor() -> str:
    classes = [f"class {n}(AnchorError):\n    code = {c}" for n, c in _ANCHOR_TABLE]
    entries = [(c, n) for n, c in _ANCHOR_TABLE]
    return _catalog_module(
        "AnchorError",
        "Anchor framework error catalog (built-in codes < 6000).",
        classes, "BY_CODE", "int", entries,
    )


def _emit_coded_msg(base: str, doc: str, table: tuple) -> str:
    classes = [f"class {n}({base}):\n    code = {c}\n    msg = {m!r}" for c, n, m in table]
    entries = [(c, n) for c, n, _ in table]
    return _catalog_module(base, doc, classes, "BY_CODE", "int", entries)


def _emit_init() -> str:
    return (
        f'{_HEADER}"""Static catalogs of the built-in error classes '
        "(native / Anchor / System / Token).\n\n"
        "One real ``class`` per bundled error, so ``AnchorError.ConstraintSeeds`` "
        "and friends are\nvisible to type checkers. Generated from "
        '``solana_fuzzer._gen.errors_catalog`` — do not edit."""\n'
    )


# names per category, in table order, for the member blocks
_CATEGORIES = {
    "solana": list(_SOLANA_NAMES),
    "anchor": [n for n, _ in _ANCHOR_TABLE],
    "system": [n for _, n, _ in _SYSTEM_ERRORS],
    "token": [n for _, n, _ in _TOKEN_ERRORS],
}


def _member_block(module: str, names: list[str], indent: str = "        ") -> str:
    """The ``from ._errors_catalog.<module> import (name as name, ...)`` body that
    goes inside a base class's ``if TYPE_CHECKING:`` block (indent = 8 spaces)."""
    inner = indent + "    "
    lines = [f"{indent}from ._errors_catalog.{module} import ("]
    lines += [f"{inner}{n} as {n}," for n in names]
    lines.append(f"{indent})")
    return "\n".join(lines)


def _fill_member_blocks(text: str) -> str:
    """Replace the content between each ``>>>GEN:<cat>>>>`` / ``<<<GEN:<cat><<<``
    marker pair in ``_errors_base.py`` with a freshly generated member block."""
    for cat, names in _CATEGORIES.items():
        pattern = re.compile(
            rf"( *# >>>GEN:{cat}>>>\n).*?(\n *# <<<GEN:{cat}<<<)", re.DOTALL
        )
        body = _member_block(cat, names)
        if not pattern.search(text):
            raise SystemExit(f"marker for category {cat!r} not found in _errors_base.py")
        text = pattern.sub(lambda m: m.group(1) + body + m.group(2), text)
    return text


def _planned_outputs() -> dict[Path, str]:
    pkg = Path(__file__).resolve().parent.parent
    cat = pkg / "_errors_catalog"
    base = pkg / "_errors_base.py"
    return {
        cat / "__init__.py": _emit_init(),
        cat / "solana.py": _emit_solana(),
        cat / "anchor.py": _emit_anchor(),
        cat / "system.py": _emit_coded_msg(
            "SystemProgramError", "Native System Program error catalog.", _SYSTEM_ERRORS),
        cat / "token.py": _emit_coded_msg(
            "TokenError", "SPL Token / Token-2022 error catalog.", _TOKEN_ERRORS),
        base: _fill_member_blocks(base.read_text()),
    }


def regenerate(check: bool = False) -> int:
    outputs = _planned_outputs()
    if check:
        stale = [p for p, txt in outputs.items()
                 if not p.exists() or p.read_text() != txt]
        if stale:
            names = ", ".join(p.name for p in stale)
            print(f"[errors_catalog] stale/missing: {names}\n"
                  f"  run: python -m solana_fuzzer._gen.errors_catalog", file=sys.stderr)
            return 2
        print("[errors_catalog] up to date")
        return 0
    for path, txt in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(txt)
        print(f"[errors_catalog] wrote {path.relative_to(path.parents[1])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit 2 if checked-in output is stale (do not write)")
    args = ap.parse_args(argv)
    return regenerate(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
