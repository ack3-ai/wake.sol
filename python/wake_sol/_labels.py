"""Account/program label resolution for call traces.

A pubkey's display ``repr`` resolves in priority order: a well-known program or
sysvar name, then an explicitly assigned label, then a truncated base58 of the
address. IDL-derived *role* names (``payer``, ``mint``, …) are separate — they
come from the decoded instruction's ``account_names`` and key into this resolver
for the value.
"""

from __future__ import annotations

from typing import Optional

#: Built-in identity labels for well-known programs and sysvars.
WELL_KNOWN: dict[str, str] = {
    "11111111111111111111111111111111": "System Program",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "Token Program",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "Token-2022 Program",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "Associated Token Account Program",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo": "Memo Program (v1)",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr": "Memo Program",
    "ComputeBudget111111111111111111111111111111": "Compute Budget Program",
    "Stake11111111111111111111111111111111111111": "Stake Program",
    "Vote111111111111111111111111111111111111111": "Vote Program",
    "Ed25519SigVerify111111111111111111111111111": "Ed25519 Program",
    "KeccakSecp256k11111111111111111111111111111": "Secp256k1 Program",
    "Secp256r1SigVerify1111111111111111111111111": "Secp256r1 Program",
    "SysvarRent111111111111111111111111111111111": "Sysvar: Rent",
    "SysvarC1ock11111111111111111111111111111111": "Sysvar: Clock",
    "SysvarRecentB1ockHashes11111111111111111111": "Sysvar: RecentBlockhashes",
    "Sysvar1nstructions1111111111111111111111111": "Sysvar: Instructions",
    "SysvarStakeHistory1111111111111111111111111": "Sysvar: StakeHistory",
    "SysvarEpochSchedu1e111111111111111111111111": "Sysvar: EpochSchedule",
    "SysvarS1otHashes111111111111111111111111111": "Sysvar: SlotHashes",
    "SysvarRewards111111111111111111111111111111": "Sysvar: Rewards",
}

#: User-assigned identity labels (pubkey base58 -> name).
_LABELS: dict[str, str] = {}

#: When True, unlabeled addresses render in full (no ``3Ftw…HBaY`` ellipsis).
#: The pytest plugin flips this on under ``-v`` (verbose).
_FULL_ADDRESSES: bool = False


def set_full_addresses(value: bool) -> None:
    """Render unlabeled addresses in full (``True``) or truncated (``False``)."""
    global _FULL_ADDRESSES
    _FULL_ADDRESSES = bool(value)


def set_label(pubkey, name: str) -> None:
    """Assign an identity label to a pubkey. Internal — the public entry point is
    the ``Account.label`` setter, which delegates here."""
    _LABELS[str(pubkey)] = name


def get_label(pubkey) -> Optional[str]:
    """The explicitly assigned label for a pubkey, or ``None``. Backs the
    ``Account.label`` getter (which is the raw value, not the resolved display)."""
    return _LABELS.get(str(pubkey))


def clear_labels() -> None:
    """Drop all user-assigned labels (the pytest plugin calls this per test)."""
    _LABELS.clear()


def program_name(pubkey) -> Optional[str]:
    """The known name for a program/sysvar pubkey, or ``None``."""
    s = str(pubkey)
    return WELL_KNOWN.get(s) or _LABELS.get(s)


def resolve_label(pubkey) -> str:
    """A pubkey's display string: well-known, else assigned, else truncated."""
    s = str(pubkey)
    if s in WELL_KNOWN:
        return WELL_KNOWN[s]
    if s in _LABELS:
        return _LABELS[s]
    if _FULL_ADDRESSES or len(s) <= 9:
        return s
    return f"{s[:4]}…{s[-4:]}"
