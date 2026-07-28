"""Curated well-known Solana program / sysvar addresses, keyed by a normalized
account name. Used as a **build-side convenience** to default a generated
builder's account parameter when the IDL does *not* pin an ``address`` itself.

DESIGN NOTE — read before extending (this is the "documented somewhere for
later"). This name->address map is a deliberate, narrow exception to the
project's refuse-don't-guess invariant, and it is safe *only* because of the
guard-rails below:

  * **Authoritative source wins.** It is consulted ONLY when the IDL account has
    no ``address`` field. An IDL-declared address (Anchor 0.30/0.31 emits one
    for ``Program<System>``, the token programs, sysvars, …) always takes
    precedence — that path is not a guess at all.
  * **Build-side only.** The result is just a default *argument value*. It never
    influences decoding or account-role labeling, so it can never cause a
    plausible-but-wrong *decode* (the failure mode the invariant guards against).
  * **Always overridable.** Pass the account explicitly and the default is
    ignored — exactly like any Python default.
  * **Name->program is not 1:1.** ``token_program`` may mean SPL Token *or*
    Token-2022 depending on the program; we map the common case (SPL Token).
    Programs that differ should — and modern Anchor IDLs do — declare the
    ``address`` explicitly, which overrides this map. When in doubt, a program
    simply omits the well-known name and the caller passes the account.

FOR LATER: if this heuristic ever proves too loose, gate it behind a ``gen``
flag (e.g. ``--no-infer-accounts``) or move the table into a facts sidecar so it
is reviewed *data* rather than baked-in code. Until then it stays small and
conservative.
"""

from __future__ import annotations

from .._addresses import (
    ASSOCIATED_TOKEN_PROGRAM_ID, CLOCK_SYSVAR, EPOCH_SCHEDULE_SYSVAR,
    INSTRUCTIONS_SYSVAR, RECENT_BLOCKHASHES_SYSVAR, RENT_SYSVAR, REWARDS_SYSVAR,
    SLOT_HASHES_SYSVAR, STAKE_HISTORY_SYSVAR, SYSTEM_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID,
)

#: normalized account name -> canonical base58 address. Addresses come from the
#: single source of truth, `wake_sol._addresses`; this table only adds the
#: name aliases (e.g. both spellings of token-2022) the IDL might use.
NAME_TO_ADDRESS = {
    "systemprogram": str(SYSTEM_PROGRAM_ID),
    "tokenprogram": str(TOKEN_PROGRAM_ID),
    "tokenprogram2022": str(TOKEN_2022_PROGRAM_ID),
    "token2022program": str(TOKEN_2022_PROGRAM_ID),
    "associatedtokenprogram": str(ASSOCIATED_TOKEN_PROGRAM_ID),
    "rent": str(RENT_SYSVAR),
    "clock": str(CLOCK_SYSVAR),
    "instructions": str(INSTRUCTIONS_SYSVAR),
    "recentblockhashes": str(RECENT_BLOCKHASHES_SYSVAR),
    "slothashes": str(SLOT_HASHES_SYSVAR),
    "stakehistory": str(STAKE_HISTORY_SYSVAR),
    "epochschedule": str(EPOCH_SCHEDULE_SYSVAR),
    "rewards": str(REWARDS_SYSVAR),
}


def _norm(name: str) -> str:
    return name.replace("_", "").replace("-", "").lower()


def resolve(name: str):
    """Canonical base58 address for a well-known account *name*, else ``None``.
    Heuristic — see the module docstring. Caller must prefer an IDL-declared
    ``address`` over this."""
    return NAME_TO_ADDRESS.get(_norm(name))
