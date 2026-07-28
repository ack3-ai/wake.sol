[← Index](./index.md)

# 1 · Getting started

## Install / build

The package is a Rust/pyo3 extension built with [maturin](https://www.maturin.rs/). In the project venv:

```bash
.venv/bin/maturin develop      # build the native extension + install editable
.venv/bin/python -m pytest -q  # run the suite
```

Use the project interpreter (`.venv/bin/python`) for everything. The CLI entry point is `wake-sol` (e.g. `wake-sol test`, `wake-sol gen`).

## The global `svm`

Importing the package gives you a process-global SVM instance, `svm` — the implicit target for `Account(...)` when you don't pass `svm=`:

```python
from wake_sol import svm, Account, LiteSVM

svm                 # the global LiteSVM, created once
other = LiteSVM()   # a separate, independent chain
```

Under `pytest` (plain `pytest` or `wake-sol test`) the global `svm` is **reset before every test** and `random` is reseeded deterministically, so tests are isolated and reproducible (see the seed printed in the test summary).

## Your first transaction

```python
from wake_sol import svm, Account

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

A **successful** `account.tx(...)` returns a [`TransactionResult`](03-transactions.md); a **failed** one raises `TransactionFailed` instead (see [§11](11-errors.md)), so `res` below is always a success:

```python
res.success                  # bool (True here)
res.logs                     # list[str] — raw program logs
res.compute_units_consumed   # int
res.return_value             # decoded return value, or None (§10)
res.events                   # list — decoded events emitted (§10)
res.call_trace               # CallTrace — the decoded instruction tree (§4)
```

> **Tip:** `from wake_sol import print` re-exports a Rich console's `print`, so `print(res.call_trace)` (and any `__rich__` object) renders colored. It intentionally shadows the builtin `print` in that test module. **Rich markup is off by default** — a stray `[..]` in a label or log won't be parsed (and can't raise); pass `markup=True` to a call if you want markup.
>
> Under `pytest -v` (or `wake-sol test -v`), call traces print **full addresses** instead of the `3Ftw…HBaY` ellipsis, and the plugin installs Rich's traceback handler.

To try a transaction **without committing** state (inspect logs / CUs / return data), use [`simulate`](03-transactions.md) instead of `tx`:

```python
sim = alice.simulate(ix)     # same shape, nothing is written
assert sim.success
```

## Where to go next

- Model accounts and read on-chain state → [§2 Accounts](02-accounts.md)
- Sign, send, simulate, and the `signers=` rule → [§3 Transactions](03-transactions.md)
- Inspect CPIs and per-node logs → [§4 Call traces](04-call-traces.md)
- Control time/rent/blockhash, feature gates → [§5 The SVM & sysvars](05-svm-and-sysvars.md)
- Read return values and events → [§10 Return values & events](10-return-values-and-events.md)
- Handle typed failures (`must_revert`, …) → [§11 Errors](11-errors.md)
- Audit against real mainnet state → [§13 Mainnet forking](13-forking.md)
