[← Index](./index.md)

# 7 · Programs & addresses

## Built-in builders: `svm.system`, `svm.token`

The System, SPL Token, and Associated-Token programs ship as hand-written builders, following the same **data-first, accounts-keyword-only** convention as generated programs.

```python
# System
svm.system.transfer(lamports, *, from_, to)
svm.system.create_account(lamports, space, owner, *, from_, to)
svm.system.assign(owner, *, account)
svm.system.allocate(space, *, account)

# SPL Token
svm.token.initialize_mint2(decimals, mint_authority, *, mint)
svm.token.initialize_account3(owner, *, account, mint)
svm.token.transfer_checked(amount, decimals, *, source, mint, destination, authority)
svm.token.mint_to_checked(amount, decimals, *, mint, account, authority)
svm.token.burn_checked(amount, decimals, *, account, mint, authority)
svm.token.approve(amount, *, source, delegate, authority)
svm.token.close_account(*, account, destination, owner)

# Associated Token Account helpers
svm.token.ata_address(owner, mint)            # -> Pubkey
svm.token.create_ata(funder, owner, mint)     # -> Instruction (CPIs System + Token)
```

Each returns an `Instruction` you hand to `account.tx(...)` / `simulate(...)`. They decode in call traces just like generated programs. `svm.system.program_id` / `svm.token.program_id` give the program addresses.

## Well-known address constants

Programs and sysvars are importable as `Pubkey` constants from the top level — no need to hand-type base58:

```python
from wake_sol import (
    SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID, RENT_SYSVAR, CLOCK_SYSVAR,
    INSTRUCTIONS_SYSVAR, RECENT_BLOCKHASHES_SYSVAR, SLOT_HASHES_SYSVAR,
    STAKE_HISTORY_SYSVAR, EPOCH_SCHEDULE_SYSVAR, REWARDS_SYSVAR,
    ED25519_PROGRAM_ID, SECP256K1_PROGRAM_ID, SECP256R1_PROGRAM_ID,
)
```

`SYSTEM_PROGRAM_ID == Pubkey(0)`. "Rent" is a **sysvar** (a read-only account), not a program — use `RENT_SYSVAR` as an account; compute rent-exemption with `svm.minimum_balance_for_rent_exemption(...)` (see [§5](05-svm-and-sysvars.md)). The three `*_PROGRAM_ID`s at the end are the signature-verification **precompiles** (always enabled) — build instructions for them with the `ed25519` / `secp256k1` / `secp256r1` modules ([§9](09-signing-and-precompiles.md)).

## Generated programs (`pytypes`)

For your own / dependency Anchor programs, `wake-sol gen` reads the IDL JSON and emits a self-registering Python module per program into a `pytypes/` package. Importing a generated module registers the program, so `decode_instruction` (and call traces) decode it, and you get a typed builder:

```python
from pytypes.my_program import MyProgram, PROGRAM_ID, SomeAccount

ix = MyProgram.do_thing(amount, side, *, payer=alice, target=pda)
alice.tx(ix)
```

Highlights of the generated surface:

- **Builders are classmethods** — call `MyProgram.do_thing(...)` directly; there is no instance state to construct. (`MyProgram().do_thing(...)` still works, but the `()` does nothing.)
- **Builders** follow the data-first / accounts-keyword-only convention; account params are `MetaLike` ([§6](06-types-and-encoding.md)).
- **Fixed-address accounts default automatically** — `system_program`, token programs, and sysvars are filled from the IDL's `address` (or a well-known name), so you usually don't pass them.
- **Types** are `@dataclass`es subclassing `BorshStruct`, so `SomeAccount(...).encode()` / `SomeAccount.decode(bytes)` work for seeding/reading account state ([§2](02-accounts.md), [§6](06-types-and-encoding.md)).
- Under pytest, a top-level `pytypes/` package is **auto-imported**, so generated programs are registered with no wiring.

The generator is deterministic and pins provenance for every emitted module (see the `__provenance__` block and `pytypes/_manifest.json`). Quick start:

```bash
wake-sol gen --target-idl ./target/idl --out ./pytypes   # generate builders for these programs
wake-sol gen check                                       # CI drift gate
wake-sol gen list                                        # show discovered programs
```

**`--target-idl` vs `--idls`.** `--target-idl` (default `target/idl`) is the set of programs you want *builders and decoders generated for* — your own program(s), the audit target. `--idls` (default `idls`) is a **dependency**-IDL root used only to *resolve types* referenced by the targets; nothing is generated for dep-only programs. So point `--target-idl` at what you're testing. `--only <base58>` narrows generation to specific program addresses (repeatable).

The remaining flags (all listed by `wake-sol gen --help`):

| Flag | Meaning |
|---|---|
| `--check` | regenerate in memory and diff against `--out`; writes nothing, exits 2 on drift (what `gen check` runs) |
| `--strict` | a per-program generation refusal becomes a hard failure instead of a refusing stub |
| `-v` | per-program generation log on stderr (repeatable) |
