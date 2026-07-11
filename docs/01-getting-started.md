[← Index](./index.md)

# 1 · Getting started

## Install / build

The package is a Rust/pyo3 extension built with [maturin](https://www.maturin.rs/). In the project venv:

```bash
.venv/bin/maturin develop      # build the native extension + install editable
.venv/bin/python -m pytest -q  # run the suite
```

Use the project interpreter (`.venv/bin/python`) for everything. The CLI entry point is `solana-fuzzer` (e.g. `solana-fuzzer test`, `solana-fuzzer gen`).

## The global `svm`

Importing the package gives you a process-global SVM instance, `svm` — the implicit target for `Account(...)` when you don't pass `svm=`:

```python
from solana_fuzzer import svm, Account, LiteSVM

svm                 # the global LiteSVM, created once
other = LiteSVM()   # a separate, independent chain
```

Under `pytest` (plain `pytest` or `solana-fuzzer test`) the global `svm` is **reset before every test** and `random` is reseeded deterministically, so tests are isolated and reproducible (see the seed printed in the test summary).

## Your first transaction

```python
from solana_fuzzer import svm, Account

# fresh keypair-backed accounts (they can sign)
alice = Account.new()
bob = Account.new()

# fund alice (lamports); accounts must exist / be rent-funded to act
svm.airdrop(alice, 1_000_000_000)

# build an instruction with a built-in program builder (data-first, accounts kw-only)
ix = svm.system.transfer(1_000_000, from_=alice, to=bob)

# alice pays + signs, send it
res = alice.tx(ix)

assert res.success, res.error
print("bob balance:", bob.lamports)
print(res.call_trace)            # a decoded, colored call tree
```

## Reading the result

`account.tx(...)` returns a [`TransactionResult`](03-transactions.md):

```python
res.success                  # bool
res.error                    # structured error string, or None
res.logs                     # list[str] — raw program logs
res.return_data              # bytes | None
res.compute_units_consumed   # int
res.call_trace               # CallTrace — the decoded instruction tree (§4)
```

> **Tip:** `from solana_fuzzer import print` re-exports a Rich console's `print`, so `print(res.call_trace)` (and any `__rich__` object) renders colored. It intentionally shadows the builtin `print` in that test module. **Rich markup is off by default** — a stray `[..]` in a label or log won't be parsed (and can't raise); pass `markup=True` to a call if you want markup.
>
> Under `pytest -v` (or `solana-fuzzer test -v`), call traces print **full addresses** instead of the `3Ftw…HBaY` ellipsis, and the plugin installs Rich's traceback handler.

To try a transaction **without committing** state (inspect logs / CUs / return data), use [`simulate`](03-transactions.md) instead of `tx`:

```python
sim = alice.simulate(ix)     # same shape, nothing is written
assert sim.success
```

## Where to go next

- Model accounts and read on-chain state → [§2 Accounts](02-accounts.md)
- Sign, send, simulate, and the `signers=` rule → [§3 Transactions](03-transactions.md)
- Inspect CPIs, logs, and errors → [§4 Call traces](04-call-traces.md)
- Control time/rent/blockhash → [§5 The SVM & sysvars](05-svm-and-sysvars.md)
