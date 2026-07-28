[← Index](./index.md)

# 10 · Return values & events

Beyond logs and the call trace ([§4](04-call-traces.md)), a transaction carries two kinds of *typed* program output: a **return value** (the program's `set_return_data`) and **events** (`emit!` / `emit_cpi!`). Both are decoded for you against the registered program interfaces ([§7](07-programs-and-addresses.md)) and hang off the `TransactionResult`.

## Return values

A Solana program returns data by writing tx-wide return bytes (`set_return_data`); the value is last-writer-wins across the whole transaction. The harness decodes those bytes against the setting instruction's IDL `returns` type:

```python
res = payer.simulate(adder.Adder.add(5, 37))   # add(a, b) -> u64  (IDL `returns: u64`)

res.return_value       # 42            — decoded per the IDL `returns` type
res.raw_return_value   # b'\x2a\x00\x00\x00\x00\x00\x00\x00'  — the raw bytes
res.return_program_id  # Pubkey(...)   — the program that set the data
```

`return_value` is **best-effort**: the setting program is matched via the call trace and the bytes are strictly decoded. If the transaction set no return data, all three are `None` (never a raise):

```python
res = svm.airdrop(Pubkey(7), 1_000_000_000)
assert res.return_value is None and res.raw_return_value is None
```

### Decoding against an explicit type: `decode_return`

When there's no attribution to lean on (a low-level / multi-instruction path), or you just want to name the type yourself, use `decode_return(ty)`. It's strict — no heuristic — and takes any annotation the codec accepts (a width alias, a generated struct/enum, `Optional[...]`, `list[...]`, …):

```python
from wake_sol import u64
res.decode_return(u64)          # -> 42
```

`raw_return_value` is always available regardless of decoding, so it's the fallback when a value can't be attributed or typed.

### When decoding raises: `ReturnDataError`

`return_value` and `decode_return` raise `ReturnDataError` (importable from `wake_sol`) rather than guess, when:

- the setting program has no generated interface,
- the return data can't be tied to an instruction,
- the instruction declares no `returns` type,
- the bytes don't validate as the target type (wrong length, trailing bytes, …),
- or there's no return data at all (for `decode_return`).

```python
from wake_sol import ReturnDataError

try:
    value = res.return_value
except ReturnDataError:
    value = res.raw_return_value        # fall back to the bytes
```

### Static typing

Running **exactly one** instruction that declares a `returns` type through the single-instruction `tx` / `simulate` overload gives you a `TransactionResult[T]`, so `res.return_value` is statically typed `T`. Every other path — `send_transaction(bytes)`, multi-instruction transactions, `airdrop` — is `TransactionResult[object]`.

## Events

`res.events` is the flat, pre-order roll-up of **every** decoded event the transaction emitted (both `emit!` log events and `emit_cpi!` self-CPI events), across the whole call tree — the natural assertion surface:

```python
res = payer.simulate(emitter.Emitter.ping(7, event_authority=ev_auth, self_program=PROGRAM_ID))

# ping(7) emits {value: 7, doubled: 14} both ways (emit! + emit_cpi!)
by_kind = {type(e).__name__: (e.value, e.doubled) for e in res.events}
assert by_kind == {"Logged": (7, 14), "Cpied": (7, 14)}
```

Each event is an instance of the generated event dataclass, so you assert against it directly:

```python
assert any(isinstance(e, Logged) and e.value == 7 for e in res.events)
```

An event whose program/discriminator isn't in a generated interface decodes to `UnknownEvent` (raw) rather than being guessed at — so an unregistered emitter never silently vanishes and never produces a wrong type.

### Per-node events

The tx-wide `res.events` is a flattening of what each frame emitted. On a `TracedInstruction` node ([§4](04-call-traces.md)) the same data is available per-frame:

```python
root = res.call_trace[0]
{type(e).__name__ for e in root.events}   # this frame's decoded events
root.events_raw                            # list[bytes] — raw payloads (disc ‖ Borsh)
```

(An `emit_cpi!` self-CPI is folded into the emitting frame's `events`, not shown as a child call.)

## In the rendered trace

Both surface in the colored call trace: an event renders as a `⚡ EventName(field=…, …)` line under its frame, and a program's return value as a `➞ <value>` line. So `print(res.call_trace)` already shows them without any of the accessors above — those are for assertions.
