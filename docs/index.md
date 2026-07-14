# solana-fuzzer — Harness Guide

A Python testing/fuzzing harness for Solana programs, backed by [litesvm](https://github.com/LiteSVM/litesvm) through a Rust/pyo3 extension. You drive a fast in-process SVM from Python: fund accounts, build & send (or simulate) transactions, and inspect a decoded call trace — with typed instruction builders generated from Anchor IDLs.

This guide covers the **runtime harness API**. For the IDL → Python *generator* (`solana-fuzzer gen`), see the design spec under [../design/pytypes-generator-spec/](../design/pytypes-generator-spec/index.md).

```python
from solana_fuzzer import svm, Account

alice, bob = Account.new(), Account.new()
svm.airdrop(alice, 1_000_000_000)
res = alice.tx(svm.system.transfer(1_000_000, from_=alice, to=bob))
print(res.call_trace)          # decoded, colored call tree
assert res.success
```

## Contents

- **[1 · Getting started](01-getting-started.md)** — install, the global `svm`, fund an account, send your first transaction, read the result.
- **[2 · Accounts](02-accounts.md)** — the `Account` handle: existence vs. keypair, creating/viewing/deriving, reading state, funding, labels.
- **[3 · Transactions](03-transactions.md)** — `account.tx(...)` / `account.simulate(...)`, the signing model (`signers=`), v0 transactions (`lookup_tables=`), and the `TransactionResult`.
- **[4 · Call traces](04-call-traces.md)** — the decoded instruction tree, per-node program logs, errors, and Rich rendering.
- **[5 · The SVM & sysvars](05-svm-and-sysvars.md)** — `LiteSVM` config, feature gates, blockhash/slot control, rent, and typed sysvar get/set (clock time-travel, etc.).
- **[6 · Types & encoding](06-types-and-encoding.md)** — width aliases (`u64`, `.max`), `pubkey`, `Opt`/`BorshEnum`, `BorshStruct.encode()/.decode()`, `MetaLike`.
- **[7 · Programs & addresses](07-programs-and-addresses.md)** — built-in builders (`svm.system`, `svm.token`), well-known address constants, and generated (`pytypes`) programs.
- **[8 · Fuzzing](08-fuzzing.md)** — the stateful `FuzzTest` engine: `@flow` / `@invariant`, randomized sequences, reproducibility from the seed, and reading the flow-stats output.
- **[9 · Signing & precompiles](09-signing-and-precompiles.md)** — detached signing (`account.sign`, `secp256k1`/`secp256r1` keys), the `SignedMessage` claim, and the ed25519/secp256k1/secp256r1 verify-instruction builders (with cross-instruction `Ref`s).
- **[10 · Return values & events](10-return-values-and-events.md)** — decoded return data (`return_value`, `decode_return`) and events (`res.events`, the `⚡` assertion surface).
- **[11 · Errors](11-errors.md)** — the raise-by-default contract, the typed `TransactionFailed` hierarchy, `must_revert` / `may_revert`, and registering custom program errors.
- **[12 · Address Lookup Tables](12-lookup-tables.md)** — the `create_lookup_table` cheatcode, the official ALT-program builders, and v0 transactions via `lookup_tables=`.
- **[13 · Mainnet forking](13-forking.md)** — `svm.fork(...)`, offline/cache replay, `exclude=` for auditing your own build, and pinning programs (`fork_programs` / `forked_accounts`).
- **[14 · Parallel running](14-parallel-running.md)** — `solana-fuzzer test -P N`: N workers of the same suite (N seeds) or sharded (`--dist uniform`), per-worker seeds & logs, and aggregated results.

## Conventions used throughout

- **Instruction builders are data-first, accounts keyword-only.** Every builder method takes the instruction's data args positionally (in IDL order) and its accounts as keyword-only params: `ix(arg1, arg2, *, account_a, account_b=None, remaining_accounts=())`. This holds for the built-ins (`svm.system`, `svm.token`) and for every generated program.
- **Addresses are `MetaLike`.** Anywhere an account is expected you can pass a `Pubkey`, an `Account`, an explicit `AccountMeta`, or a base58 `str` / 32 raw `bytes` / big-endian `int` — the builder coerces it (see [§6](06-types-and-encoding.md)).
- **One import root.** Almost everything is re-exported from the top-level `solana_fuzzer` package, so `from solana_fuzzer import svm, Account, Pubkey, u64, RENT_SYSVAR, …` works.
