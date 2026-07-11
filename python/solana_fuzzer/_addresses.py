"""Canonical well-known Solana program / sysvar addresses, as ``Pubkey``
constants. The single source of truth for these; the generator's name->address
table (`_gen/wellknown.py`) and the label map derive from / mirror these.

Sysvars are read-only accounts holding cluster parameters (rent, clock, …). To
*compute* rent-exemption you don't read `RENT_SYSVAR` yourself — use
``svm.minimum_balance_for_rent_exemption(data_len)``.
"""

from __future__ import annotations

from ._native import Pubkey

# --- programs ---
SYSTEM_PROGRAM_ID = Pubkey("11111111111111111111111111111111")
TOKEN_PROGRAM_ID = Pubkey("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

# --- signature-verification precompiles (native, always present) ---
ED25519_PROGRAM_ID = Pubkey("Ed25519SigVerify111111111111111111111111111")
SECP256K1_PROGRAM_ID = Pubkey("KeccakSecp256k11111111111111111111111111111")
SECP256R1_PROGRAM_ID = Pubkey("Secp256r1SigVerify1111111111111111111111111")

# --- sysvars (read-only accounts, not programs) ---
RENT_SYSVAR = Pubkey("SysvarRent111111111111111111111111111111111")
CLOCK_SYSVAR = Pubkey("SysvarC1ock11111111111111111111111111111111")
INSTRUCTIONS_SYSVAR = Pubkey("Sysvar1nstructions1111111111111111111111111")
RECENT_BLOCKHASHES_SYSVAR = Pubkey("SysvarRecentB1ockHashes11111111111111111111")
SLOT_HASHES_SYSVAR = Pubkey("SysvarS1otHashes111111111111111111111111111")
STAKE_HISTORY_SYSVAR = Pubkey("SysvarStakeHistory1111111111111111111111111")
EPOCH_SCHEDULE_SYSVAR = Pubkey("SysvarEpochSchedu1e111111111111111111111111")
REWARDS_SYSVAR = Pubkey("SysvarRewards111111111111111111111111111111")

__all__ = [
    "SYSTEM_PROGRAM_ID",
    "TOKEN_PROGRAM_ID",
    "TOKEN_2022_PROGRAM_ID",
    "ASSOCIATED_TOKEN_PROGRAM_ID",
    "ED25519_PROGRAM_ID",
    "SECP256K1_PROGRAM_ID",
    "SECP256R1_PROGRAM_ID",
    "RENT_SYSVAR",
    "CLOCK_SYSVAR",
    "INSTRUCTIONS_SYSVAR",
    "RECENT_BLOCKHASHES_SYSVAR",
    "SLOT_HASHES_SYSVAR",
    "STAKE_HISTORY_SYSVAR",
    "EPOCH_SCHEDULE_SYSVAR",
    "REWARDS_SYSVAR",
]
