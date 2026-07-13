[← Index](./index.md)

# 13 · Mainnet forking

Forking lets a test run against **real mainnet state**: dependency accounts hydrate from a Solana RPC endpoint the first time they're touched, and are cached to disk so the same run replays deterministically **offline** afterwards. It's the backbone of the audit workflow — fork the real program's dependencies, swap in your own build, and drive it.

```python
svm.fork(url, *, exclude=(), cache="./fork-cache", offline=False)
```

## How it hydrates

With forking on, any account the SVM needs but doesn't have locally is fetched from `url` (an RPC endpoint) on **first touch** — the full account set of each transaction, plus any account you read directly (`Account(addr).lamports`, etc.). Fetched accounts are written under `cache` (default `./fork-cache`, one JSON file per address, including negative "confirmed-absent" entries) so a later run needs no network.

Two precedence rules make this predictable:

- **Local always wins.** Anything you `set_account` / `add_program` is never overwritten by the fork — your seeded state and your own program build take priority over mainnet.
- **The feature set is yours, not the chain's.** Forking uses the SVM's own feature set (mainnet parity by default; diverge via the constructor's `activate=` / `deactivate=`, see [§5](05-svm-and-sysvars.md)) — it is *not* fetched from the forked chain.

## Offline replay (CI)

With `offline=True` (or no `url`), nothing touches the network: state is served from the cache, and a **cache miss is a hard error** (never a silent empty account). This is how a forked scenario runs in CI — commit the cache, replay hermetically:

```python
svm = LiteSVM()
svm.fork(cache="./fork-cache", offline=True)      # no url -> snapshot only

acc = Account(some_mainnet_address, svm)
assert acc.exists and acc.lamports == ...          # served from ./fork-cache
```

The typical loop: run the scenario **once online** to populate the cache, then switch to `offline=True` for repeatable runs.

## Auditing your own build: `exclude`

`exclude` lists program IDs to treat as *not on mainnet*. For each: its bytecode is **not** forked (you deploy your own build with `add_program`), and any account it **owns** starts **blank**. So the audited program reinitializes from scratch while its real mainnet dependencies fork in around it, untouched:

```python
svm = LiteSVM()
svm.fork(url, exclude=[MY_PROGRAM])       # don't fork MY_PROGRAM's code or its owned state
svm.add_program(MY_PROGRAM, my_patched_elf)   # deploy the build under audit instead
# ...its real dependencies (pools, mints, config) hydrate on touch as usual
```

See `examples/marinade_audit/` for the full composition against a live program (fork → pin → `gen` pytypes → decode real bytes → drive an instruction).

## Pinning programs for offline replay: `fork_programs` / `forked_accounts`

`svm.forked_accounts()` returns the on-chain accounts hydrated so far, as read-only `Account` views (every *present* address the fork has resolved this session, ordered by address — excluding confirmed-absent, owner-blanked, and your own locally-set accounts). It's how you discover what a scenario actually touched.

`svm.fork_programs(*ids)` pre-fetches **programs** by id without running a transaction — each program account (and, for upgradeable programs, its programdata) hydrates exactly as lazy touch would. Use it to *pin* the program set a test needs so an `offline=True` replay has no network dependency. It raises if an id isn't an executable program on chain (so a wrong id fails loudly), and returns the number pinned:

```python
svm.fork(url)
svm.fork_programs(MY_PROGRAM)                 # -> 1; also pulls programdata for upgradeables

# discover-then-freeze: run online once, then pin every program it touched
svm.fork_programs(*(a.pubkey for a in svm.forked_accounts() if a.executable))
```

## Turning it off: `unfork`

`svm.unfork()` drops the fork configuration so nothing further hydrates. It **stops** forking; it does not **undo** it — already-hydrated accounts stay, and the seeded clock/blockhash aren't reverted (those are `reset()`'s job). The on-disk cache is untouched.

- `unfork()` alone → **freezes** the current forked state (hydrate what you need online, then run RPC-free).
- `unfork()` then `reset()` → a clean, unforked SVM.
- `reset()` alone → keeps forking **on** (wipes accounts back to genesis, still hydrates).

The full model — RPC/snapshot resolution, the negative cache, owner-blanking, phasing — is specified under [../design/forking-spec/](../design/forking-spec/index.md).
