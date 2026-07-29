# wake.sol

A Python testing and fuzzing harness for Solana programs, backed by
[litesvm](https://github.com/LiteSVM/litesvm) through a Rust/pyo3 extension.
You drive a fast in-process SVM from Python: fund accounts, build and send (or
simulate) transactions, and read a decoded call trace — with typed instruction
builders generated from Anchor IDLs.

It is the Solana counterpart to Wake, the Solidity development and testing
framework, and follows the same shape: tests are plain `pytest`, the fuzzer is
stateful and seed-reproducible, and failures come back as typed exceptions
rather than strings.

> **Status: soft release.** The API is usable and covered by tests, but it is
> not yet stable — expect breaking changes.

```python
from wake_sol import svm, Account

alice, bob = Account.new(), Account.new()
svm.airdrop(alice, 1_000_000_000)

res = alice.tx(svm.system.transfer(1_000_000, from_=alice, to=bob))
print(res.call_trace)          # decoded, colored call tree
assert res.success
```

## Build from source

There is no published release yet, so install from a checkout. The package is a
Rust/pyo3 extension built with [maturin](https://www.maturin.rs/), so a Rust
toolchain (1.85+, for edition 2024) is needed alongside Python 3.9+.

```bash
uv sync                    # create .venv and install the Python dependencies
uv pip install maturin
uv run maturin develop     # build the native extension + install editable
uv run pytest -q           # run the suite
```

The same thing in a plain venv, without uv:

```bash
python -m venv .venv && . .venv/bin/activate
pip install maturin
maturin develop
pytest -q
```

A few end-to-end tests drive the small on-chain programs under `programs/`, and
**skip** unless that program's `.so` is built (each skip message names the
command). With a Solana toolchain installed:

```bash
cd programs/native-counter && cargo build-sbf   # likewise native-adder, native-emitter
```

`maturin develop --release` builds an optimized extension instead — worth it for
long fuzzing campaigns. To get a wheel rather than an editable install,
`maturin build --release` writes one to `target/wheels/`, installable with
`pip install` anywhere the interpreter matches.

## What you get

- **A real SVM, in-process.** Full transaction execution against
  mainnet-parity feature gates, fast enough to run thousands of transactions
  per test.
- **Typed builders from Anchor IDLs.** `wake-sol gen` emits a `pytypes/`
  package — instruction builders, account/event structs, and one exception
  class per IDL error — that self-registers on import.
- **Decoded call traces.** The instruction tree with per-node programs, logs,
  compute units, return values, and events.
- **Errors as types.** A failed transaction *raises*, resolved to a specific
  class (`TokenError.InsufficientFunds`, `MyProgram.TooSmall`), with
  `must_revert` / `may_revert` for assertions.
- **Stateful fuzzing.** `FuzzTest` with `@flow` / `@invariant`, weighted
  randomized sequences, per-flow coverage stats, and full reproduction from a
  printed seed.
- **Mainnet forking.** Hydrate real accounts from an RPC endpoint on first
  touch, cache them to disk, then replay hermetically offline — including
  `exclude=` to swap your own build in for the program under audit.
- **Parallel runs.** `wake-sol test -P N` runs N worker processes, either N
  seeds of the same suite or a sharded suite, with per-worker logs and
  aggregated results.
- **Cheatcodes.** Write account state and balances directly, warp the clock,
  set sysvars and rent, create lookup tables, and sign for
  ed25519/secp256k1/secp256r1 precompiles.

## Documentation

The [harness guide](docs/index.md) is the reference — start with
[§1 Getting started](docs/01-getting-started.md), then
[§8 Fuzzing](docs/08-fuzzing.md) for the fuzzer.

## License

[ISC](LICENSE)
