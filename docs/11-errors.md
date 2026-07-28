[← Index](./index.md)

# 11 · Errors

## Raise by default

A **failed** `tx` / `simulate` **raises**; a successful one returns a `TransactionResult`. So the happy path needs no error checking, and a failure never slips through as a "successful-looking" result:

```python
res = alice.tx(svm.system.transfer(5_000_000, from_=alice, to=bob))
assert res.success is True and res.error is None      # success path: you get a result

with pytest.raises(TransactionFailed):
    alice.tx(svm.system.transfer(10**18, from_=alice, to=bob))   # overspend -> raises
```

The exception carries the execution **receipt** as `.tx` (a `TransactionResult` with `success is False`), and that receipt points back at the exception — `exc.tx.error is exc`:

```python
try:
    alice.tx(svm.system.transfer(10**18, from_=alice, to=bob))
except TransactionFailed as e:
    e.code               # 1              — the program/instruction error code (or None)
    e.instruction_index  # 0              — which top-level instruction failed
    e.msg                # "..."          — the on-chain message, when known
    e.tx.success         # False          — the failed receipt
    e.tx.call_trace      # CallTrace      — reachable via the receipt
    e.call_trace         # same trace, shortcut (raises if there's no receipt)
```

## The exception hierarchy

The raised type is **resolved** from the failure, so you can catch as narrowly or broadly as you like. Everything is a `TransactionFailed`:

```
TransactionFailed
├── SolanaError          native runtime errors (InvalidAccountData, InsufficientFundsForRent, …)
├── ProgramError         a program's own Custom(code) error
│   ├── SystemProgramError    (code 0 = AccountAlreadyInUse, 1 = ResultWithNegativeLamports, …)
│   ├── TokenError            (shared by Token + Token-2022)
│   └── <YourProgramError>    generated / registered program errors
├── AnchorError          the Anchor framework range (2000–2999, 3000–…)
└── UnknownError         a code matched by nobody — surfaced bare, never invented
```

Catching a base category matches every subclass under it:

```python
with pytest.raises(SystemProgramError.ResultWithNegativeLamports):   # the exact error
    _overspend()
with pytest.raises(ProgramError):        # SystemProgramError is a ProgramError
    _overspend()
with pytest.raises(TransactionFailed):   # ...and everything is a TransactionFailed
    _overspend()
```

### Program-scoped resolution

The same `Custom(code)` means different things in different programs, so resolution is **program-scoped** — the failing program disambiguates:

```python
# code 0, but a different named error depending on which program failed:
#   System  -> SystemProgramError.AccountAlreadyInUse
#   Token   -> TokenError.NotRentExempt
```

A code no catalog claims (and that isn't in the Anchor range) resolves to `UnknownError` with the raw `.code` — the harness refuses to guess.

## `must_revert` / `may_revert`

For the common "this *should* fail" assertion, use the context managers (importable from `solana_fuzzer`) instead of a bare `pytest.raises`. `must_revert` **requires** a matching revert; `may_revert` **allows** success. Both expose the captured exception as `.value`:

```python
from solana_fuzzer import must_revert, may_revert, SystemProgramError

# must fail, with this specific error
with must_revert(SystemProgramError.ResultWithNegativeLamports) as e:
    alice.tx(svm.system.transfer(10**18, from_=alice, to=bob))
assert e.value.code == 1

# may or may not fail; e.value is None if it succeeded
with may_revert(SystemProgramError.ResultWithNegativeLamports) as e:
    alice.tx(maybe_failing_ix)
```

The matcher argument is flexible:

| You pass | Matches |
|---|---|
| an exception **type** (`TokenError.InsufficientFunds`, `ProgramError`, `TransactionFailed`) | that type or any subclass |
| a bare **int** (`must_revert(1)`) | any revert whose `.code` equals it (program-agnostic) |
| **nothing** (`must_revert()`) | any revert at all |

Semantics that make these safe as assertions:

- `must_revert(...)` raises `AssertionError` if the body **succeeds** (it was supposed to fail).
- A revert that **doesn't match** the expected type/code is *not* swallowed — it propagates unchanged, so a wrong-error failure still surfaces as itself.
- A non-type, non-int argument (a string, `True`, a non-exception class) raises `TypeError` — misuse fails loudly.

## Custom program errors: `register_errors`

Generated `pytypes` programs emit an error class per IDL `errors[]` entry, **nested on the program's builder class** and rooted at a per-program `Error` base, and they **register themselves on import** — so a failure in that program resolves to the generated class automatically:

```python
# generated in pytypes/my_program.py, registered when you import it:
#   class MyProgram:
#       class Error(ProgramError): ...            # every error below is one
#       class TooSmall(Error): code = 6100; msg = "value is too small"

with must_revert(MyProgram.TooSmall) as e:
    payer.tx(MyProgram.do_thing(0, ...))
assert e.value.code == 6100
```

Nesting is what makes them read as what they are — errors *that program* defines — and puts them one attribute away from the builder you are already calling. Catch `MyProgram.Error` to match any error from that program.

Each class is also aliased at module level, so a direct import keeps working and the base is available under its `<Program>Error` name:

```python
from pytypes.my_program import TooSmall, MyProgramError   # aliases, same objects
assert TooSmall is MyProgram.TooSmall
assert MyProgramError is MyProgram.Error
```

To register a hand-written error family (no IDL), subclass `ProgramError` and call `register_errors`:

```python
from solana_fuzzer import ProgramError, register_errors

class MyProgError(ProgramError): ...
class DoomViolated(MyProgError):
    code = 61234
    msg = "doom was violated"

register_errors(MyProgError)     # now Custom(61234) resolves to DoomViolated
```
