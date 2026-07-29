[← Index](./index.md)

# 9 · Signing & precompiles

Solana's three signature-verification **precompiles** — ed25519, secp256k1
(Ethereum-style), and secp256r1 (NIST P-256 / passkeys) — are **always enabled**,
matching mainnet. A transaction that carries a precompile instruction (oracle
updates, off-chain-signed permits, passkey auth, …) verifies here exactly as it
would on-chain.

This page covers detached **signing** and building those precompile
instructions. Transaction signing itself is automatic — see [§3](03-transactions.md).

## The claim: `SignedMessage`

Every precompile checks one or more `(identity, signature, message)` tuples. That
tuple is a `SignedMessage`, and it's the unit every builder consumes. You get one
by signing:

```python
alice = Account.new()
sm = alice.sign(b"hello")     # ed25519 -> SignedMessage
sm.signature                  # 64 raw bytes  (also bytes(sm))
sm.identity                   # the verifier key (here, alice's 32-byte pubkey)
```

`account.sign` is the ed25519 signer (an `Account` *is* an ed25519 keypair). The
other two curves aren't Solana accounts, so they have their own key types:

```python
k = secp256k1.Key.new()       # Ethereum-style; identity is a 20-byte eth address
k.eth_address                 # -> bytes(20)
smk = k.sign(b"withdraw:42")  # keccak256 + recoverable ECDSA; smk.recovery_id is set

r = secp256r1.Key.new()       # P-256; identity is a 33-byte compressed pubkey
r.public_key                  # -> bytes(33)
smr = r.sign(b"authorize")    # SHA-256, low-S normalized
```

`Key.new()` seeds from `wake_sol.random`, so keys are **reproducible from the
`--seed`** just like `Account.new()` ([§8](08-fuzzing.md)). `Key.from_secret(bytes)`
reconstructs a known key.

## Building a verify instruction

Each curve module has `verify(*claims)`, returning an instruction you drop into
`account.tx(...)` / `simulate(...)`. It's variadic — several claims batch into one
instruction:

```python
payer.tx(ed25519.verify(alice.sign(b"m")))                      # single
payer.tx(secp256k1.verify(k.sign(b"a"), k2.sign(b"b")))         # batched
payer.tx(program_ix, secp256r1.verify(r.sign(b"x")))            # alongside other ixs
```

## Forging claims (the fuzzing move)

A `SignedMessage` is a plain frozen dataclass, so you can hand-build or mutate one
to feed the verifier something wrong — the first-class way to exercise a program's
own signature checks (the same spirit as `signer()` / `writable()` passing wrong
account privileges):

```python
import dataclasses
bad = dataclasses.replace(alice.sign(b"real"), signature=bytes(64))
payer.tx(ed25519.verify(bad))          # raises TransactionFailed — the runtime rejects it
```

## Cross-instruction references

By default a claim's components are stored **inline** in the precompile
instruction. Real programs often instead point the precompile at *another*
instruction's data — they introspect the Instructions sysvar and check that the
precompile verified exactly the bytes the program is processing. Use `.at(...)`
with `Ref` to reproduce (and fuzz) that binding, without duplicating the bytes:

```python
prog_ix = my_program.consume(msg=payload, ...)         # a sibling instruction
ix = ed25519.verify(alice.sign(payload).at(message=Ref(prog_ix)))
payer.tx(prog_ix, ix)   # the precompile's message offset points into prog_ix's data
```

`Ref` forms, ergonomic to raw:

| form | meaning |
|---|---|
| `Ref(prog_ix)` | find this component's bytes in `prog_ix`'s data (must occur once) |
| `Ref(prog_ix, offset=o, size=n)` | an explicit slice of `prog_ix`'s data |
| `Ref(index=i, offset=o, size=n)` | a raw instruction index (fuzz hatch) |

A `Ref` whose bytes aren't found — or that names an instruction not in the
transaction — is refused at build time (refuse, don't guess).

The default placement — bytes stored inline in the precompile instruction — is the
`Inline` marker (also importable from `wake_sol`). You rarely write it, since
it's the default for every component, but it's the explicit counterpart to `Ref`
when repointing only *some* components: `sm.at(message=Ref(prog_ix), signature=Inline())`.

> **secp256k1 resolves late.** Unlike ed25519/secp256r1, the secp256k1 precompile
> has no "this instruction" sentinel, so even an inline entry must name its own
> transaction position. `secp256k1.verify(...)` therefore returns a
> `PrecompileInstruction` that binds its index when you pass it to `account.tx` /
> `.simulate` (or `.resolve(index)` explicitly). ed25519/secp256r1 `verify(...)`
> return a plain, position-independent `Instruction` unless a `Ref` is used.

## The raw hatch: `pack`

For adversarial layouts the ergonomic path won't produce — wrong instruction
indices, a header count that disagrees with the entries, zero entries,
out-of-range offsets — each module has `pack(count, entries, data)` that writes
`[count] + entries + data` verbatim with **no validation**. (The only limit is
the wire format itself: each `Offsets` field must fit its slot — `0…65535` for
the `u16` offsets and size, `0…255` for secp256k1's `u8` instruction indices.)

```python
from wake_sol import Offsets
ed25519.pack(0, [])                                  # a valid "verify nothing"
ed25519.pack(3, [Offsets(signature_offset=16, identity_offset=48,
                         message_offset=112, message_size=60_000)],  # count/entry & size lies
              data=b"\x00" * 96)
```

## Addresses

The precompile program IDs are importable constants:

```python
from wake_sol import ED25519_PROGRAM_ID, SECP256K1_PROGRAM_ID, SECP256R1_PROGRAM_ID
```

They render by name in call traces (`Ed25519 Program::verify(signatures=1)`).
