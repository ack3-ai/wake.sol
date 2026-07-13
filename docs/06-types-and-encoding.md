[← Index](./index.md)

# 6 · Types & encoding

The Borsh codec and its type vocabulary live in `solana_fuzzer._codec` and are re-exported from the top-level `solana_fuzzer`. Generated `pytypes` modules use exactly these symbols; you use the same ones to define ad-hoc types or encode/decode values in tests.

## Width-carrying number aliases

Numbers are typed with width aliases — never bare `int`/`float`, so the codec knows the byte width:

```python
from solana_fuzzer import u8, u16, u32, u64, u128, u256, i8, i16, i32, i64, i128, i256, f32, f64, char, pubkey
```

(`u256` / `i256` are 256-bit; `char` is a 4-byte Unicode codepoint. These three are **engine extensions**, not emitted by Anchor IDLs — handy when hand-defining a non-Anchor layout.)

Each is a real `int`/`float` subclass exposing bounds and metadata (à la wake's `uintN`):

```python
u64.max     # 18446744073709551615
i32.min, i32.max
u8.bits, u8.signed     # 8, False
u8(300)                # ValueError: out of range [0, 255]
```

**Type-checker friendliness:** to a static type-checker every width is just `int` (`f32`/`f64` are `float`), so a `u64` (e.g. `account.lamports`), a plain literal, or another width flow into a `u32` parameter without a red squiggle. The width that matters is enforced at **runtime** — encoding range-checks every value and raises on overflow / a negative into an unsigned. (Trade-off: because they look like `int` statically, `u256.max` resolves at runtime but a strict checker won't statically know the `.max` attribute.)

`pubkey` is the native `Pubkey` class (32 raw bytes), re-exported lowercase; `pubkey is Pubkey`.

## Containers, options, enums

| Concept | How you write it | Wire |
|---|---|---|
| vector | `list[u16]` | u32 len + items |
| fixed array | `Annotated[list[i64], 4]` | N items, no prefix |
| fixed bytes `[u8; N]` | `Annotated[bytes, 32]` | N raw bytes |
| bytes / `Vec<u8>` | `bytes` | u32 len + raw |
| option | `Optional[u64]` / `u64 \| None` | u8 tag + payload |
| nested option | `Opt[Opt[u64]]` | keeps `Some(None)` vs `None` distinct |
| SPL COption | `COption[T]` | 4-byte tag (SPL-Pack only) |

Enums:

```python
from enum import IntEnum
from solana_fuzzer import BorshEnum, variant
from dataclasses import dataclass

class Side(IntEnum):          # all-unit → IntEnum (value = wire tag)
    Bid = 0
    Ask = 1

class Action(BorshEnum):      # any data-carrying variant → BorshEnum
    @variant(0)
    @dataclass
    class Noop: ...
    @variant(1)
    @dataclass
    class Move:
        x: i64
        y: i64
```

## Structs & `BorshStruct`

Define a struct as a `@dataclass` subclassing `BorshStruct`, and you get ergonomic, IDE-discoverable encode/decode that hide the (cached) layout compilation:

```python
from dataclasses import dataclass
from solana_fuzzer import BorshStruct, u64, pubkey

@dataclass
class Wallet(BorshStruct):
    authority: pubkey
    amount: u64

data = Wallet(authority=pk, amount=42).encode()   # bytes
back = Wallet.decode(data)                          # Wallet  (typed return)
```

For an **account** type (carrying a `__borsh_meta__` with a discriminator), `.encode()` prepends the 8-byte discriminator by default — the full on-chain layout, ready for `svm.set_account(data=…)` — and `.decode()` strips/verifies it (tolerating trailing realloc slack). Pass `with_discriminator=False` for body-only. Plain structs are body-only.

## Low-level codec

When you have an annotation but no `BorshStruct` (e.g. a single field):

```python
from solana_fuzzer import encode, decode, Mode, compile_field   # compile_* are engine internals
node = compile_field(u64)
encode(7, node)                       # b'\x07\x00\x00\x00\x00\x00\x00\x00'
decode(b'\x07...', node, Mode.IX_DATA)
```

`Mode.IX_DATA` requires exact length (no trailing bytes); `Mode.ACCOUNT_DATA` tolerates trailing realloc slack. Decode failures raise `BorshError`; generation-time problems (a bare `int`, an untagged union) raise `GenError`.

## `MetaLike` — account inputs

`MetaLike = AccountMeta | Pubkey | Account | str | bytes | int` is the type every builder account parameter accepts. A bare address is coerced to an `AccountMeta` with the IDL-declared flags; an explicit `AccountMeta` (or the `signer`/`writable`/`readonly`/`writable_signer` helpers) overrides them.
