[← Index](./index.md)

# 3 · Transactions

Build instructions with a program builder, then send or simulate them through a fee-payer account.

```python
ix = svm.system.transfer(1_000_000, from_=alice, to=bob)
res = alice.tx(ix)            # build → sign → send (commits state)
sim = alice.simulate(ix)      # build → sign → run, but commit nothing
```

Both take any number of instructions (executed in order) and, **on success**, return a [`TransactionResult`](#transactionresult). A **failed** transaction raises `TransactionFailed` instead of returning — see [§11 Errors](11-errors.md):

```python
def tx(self, *ixs: Instruction,
       signers: Sequence[Account] = (),
       lookup_tables: Sequence[AddressLike] = ()) -> TransactionResult: ...
def simulate(self, *ixs: Instruction,
             signers: Sequence[Account] = (),
             lookup_tables: Sequence[AddressLike] = ()) -> TransactionResult: ...
```

`self` (the account you call it on) is the **fee payer** and must hold a keypair. The recent blockhash is taken from the bound SVM. Passing `lookup_tables=` builds a **v0** transaction that sources accounts from those Address Lookup Tables; omitting it builds a legacy transaction — see [§12](12-lookup-tables.md).

## `tx` vs `simulate`

Identical build + sign path; only the final step differs:

| | commits state? | `signature` | `logs` / `return_value` / `events` / `compute_units` / `call_trace` |
|---|---|---|---|
| `tx` | yes | set | yes |
| `simulate` | **no** | `None` | yes |

Use `simulate` to inspect logs / compute units / return value / events, or to check whether something *would* succeed, without spending the transaction or mutating balances.

## The signing model (`signers=`)

Two independent things:

1. **Who must sign** is fixed by the instructions (every account flagged as a signer). `signers=` does **not** change this set — it neither adds signers nor replaces the inferred ones. Passing a non-required account does nothing.
2. **The private key** for each required signer is resolved automatically, in order:
   1. the fee payer (`self`),
   2. the accounts you pass in `signers=`,
   3. the process-global keystore of every account ever made with `Account.new()` / `Account.from_secret()`.

   If none yields a key, `tx`/`simulate` raises `no keypair known for required signer <addr>; pass it via signers=`.

So `signers=` is a **fallback supply of keys** for a required signer the harness can't otherwise resolve. Because created signing accounts auto-register, you usually don't need it:

```python
payer, new_acc = Account.new(), Account.new()
svm.airdrop(payer, 5_000_000_000)
# new_acc must co-sign create_account; its key is found in the keystore automatically
res = payer.tx(svm.system.create_account(rent, 0, owner, from_=payer, to=new_acc))
```

## Passing accounts (`MetaLike`)

Account params accept a `Pubkey`, an `Account`, an explicit `AccountMeta`, or an address-like `str`/`bytes`/`int`. A bare address gets the instruction's IDL-declared signer/writable flags; pass an explicit `AccountMeta` to override them (a flag mismatch emits a suppressible `AccountFlagOverride` warning — a deliberate fuzzing hook):

```python
from wake_sol import AccountMeta, signer, writable, readonly, writable_signer
svm.system.transfer(1, from_=writable_signer(alice), to=bob)   # explicit privileges
```

`remaining_accounts=(...)` (trailing, keyword-only) appends extra accounts after the declared slots.

## TransactionResult

`tx` / `simulate` return a `TransactionResult` **only on success** (`res.success` is then always `True`). A failure raises `TransactionFailed`; you reach the failed receipt through the exception's `.tx` — see [§11 Errors](11-errors.md).

```python
res.success                  # bool (True for a returned result)
res.signature                # bytes | None  (None for simulate; all-zero if signing was skipped)
res.logs                     # list[str]     — raw program logs (tx-wide)
res.compute_units_consumed   # int
res.call_trace               # CallTrace     — decoded instruction tree (§4)

res.return_value             # decoded return value (per IDL `returns`), or None    (§10)
res.raw_return_value         # raw return-data bytes, or None                       (§10)
res.return_program_id        # Pubkey that set the return data, or None             (§10)
res.decode_return(u64)       # decode the return data against an explicit type      (§10)
res.events                   # list — decoded events (emit! / emit_cpi!)            (§10)

res.error                    # TransactionFailed | None — the exception, or None on success
```

Return values and events have their own page — see [§10](10-return-values-and-events.md). Error handling (the typed hierarchy, `must_revert` / `may_revert`) is [§11](11-errors.md).

> **A note on `signature`.** When the sending SVM has **both** `sigverify` and `transaction_history` off, signatures are cosmetic (never verified, never used as a dedup key), so `tx` skips ed25519 signing entirely and leaves the all-zero placeholder — `res.signature` is then the 64-byte all-zero placeholder rather than a real signature. See [§5](05-svm-and-sysvars.md).
