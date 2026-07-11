[← Index](./index.md)

# 3 · Transactions

Build instructions with a program builder, then send or simulate them through a fee-payer account.

```python
ix = svm.system.transfer(1_000_000, from_=alice, to=bob)
res = alice.tx(ix)            # build → sign → send (commits state)
sim = alice.simulate(ix)      # build → sign → run, but commit nothing
```

Both take any number of instructions (executed in order) and return a [`TransactionResult`](#transactionresult):

```python
def tx(self, *ixs: Instruction, signers: Sequence[Account] = ()) -> TransactionResult: ...
def simulate(self, *ixs: Instruction, signers: Sequence[Account] = ()) -> TransactionResult: ...
```

`self` (the account you call it on) is the **fee payer** and must hold a keypair. The recent blockhash is taken from the bound SVM.

## `tx` vs `simulate`

Identical build + sign path; only the final step differs:

| | commits state? | `signature` | `logs` / `return_data` / `compute_units` / `call_trace` |
|---|---|---|---|
| `tx` | yes | set | yes |
| `simulate` | **no** | `None` | yes |

Use `simulate` to inspect logs / compute units / return data, or to check whether something *would* succeed, without spending the transaction or mutating balances.

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
from solana_fuzzer import AccountMeta, signer, writable, readonly, writable_signer
svm.system.transfer(1, from_=writable_signer(alice), to=bob)   # explicit privileges
```

`remaining_accounts=(...)` (trailing, keyword-only) appends extra accounts after the declared slots.

## TransactionResult

```python
res.success                  # bool
res.signature                # bytes | None  (None for simulate / failures)
res.error                    # str | None    — structured TransactionError, one line
res.logs                     # list[str]     — raw program logs (tx-wide)
res.return_data              # bytes | None
res.compute_units_consumed   # int
res.call_trace               # CallTrace     — decoded instruction tree (§4)
```

On failure, `res.error` is the structured error such as `InstructionError(0, ProgramFailedToComplete)` — the leading number is the **top-level instruction index**, not an error code (`Custom(1)` is the one case where the number *is* the program's error code). The human *reason* (a panic message, `Error: …`, `insufficient lamports …`) is attributed per-node in the call trace's logs — see [§4](04-call-traces.md).
