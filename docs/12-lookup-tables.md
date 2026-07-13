[← Index](./index.md)

# 12 · Address Lookup Tables & v0 transactions

An Address Lookup Table (ALT) stores a list of addresses on-chain so a **v0** (versioned) transaction can reference them by index instead of spelling each out — the mechanism that lets a transaction touch more accounts than a legacy message fits. In the harness you get both a one-call cheatcode to *have* a usable table and the real ALT-program builders to *exercise the table lifecycle itself*.

## The quick way (cheatcode): `create_lookup_table`

`svm.create_lookup_table(addresses, *, address=None, authority=None)` injects a ready-to-use table directly (via `set_account`) and returns its address. It bypasses the ALT program's create/extend flow and its recent-slot + one-slot-warmup + authority requirements — the table is **immediately active**, which can't happen on a real chain:

```python
recipient = Account.new(svm)
table = svm.create_lookup_table([recipient.pubkey])   # -> Pubkey, active now
```

- `address=` fixes the table's own address (default: a fresh one).
- `authority=` sets the modification authority (default: System — irrelevant for pure lookups).

This is the god-mode path: use it whenever you just need a table to drive a v0 transaction.

## Using a table: v0 transactions

Pass tables to `tx` / `simulate` via `lookup_tables=`; that builds a **v0** message sourcing eligible accounts from them. Omitting `lookup_tables=` builds a legacy transaction (everything else in the guide):

```python
svm.warp_to_slot(100)                     # advance off genesis so lookups resolve
payer = Account.new(svm); svm.airdrop(payer, 10_000_000_000)
recipient = Account.new(svm)

alt = svm.create_lookup_table([recipient.pubkey])          # recipient provided only via the ALT
ix = svm.system.transfer(2_000_000, from_=payer, to=recipient)
res = payer.tx(ix, lookup_tables=[alt])                    # v0 message
assert res.success and recipient.lamports == 2_000_000     # resolved to the right pubkey
```

> **Advance the slot.** A cheatcode table has `last_extended_slot = 0`, so its addresses are active at any slot ≥ 1. On the genesis slot lookups won't resolve — `svm.warp_to_slot(...)` (or any tx that moves the slot) first.

`simulate(..., lookup_tables=[...])` builds the same v0 message but commits nothing. A `lookup_tables=` entry with no table at that address is a clear error rather than a silent misresolve:

```python
with pytest.raises(Exception):
    payer.tx(ix, lookup_tables=[some_address_with_no_table])
```

## The faithful way: `svm.address_lookup_table`

`svm.address_lookup_table` builds the **real** on-chain ALT-program instructions — not cheatcodes. They carry the program's real constraints (a `recent_slot` present in the `SlotHashes` sysvar, authority signatures, and the one-slot warmup before a new table's addresses become usable). Reach for these when you're testing the ALT lifecycle itself; otherwise prefer the cheatcode above.

```python
payer = Account.new(svm); svm.airdrop(payer, 1_000_000_000)

create_ix, table = svm.address_lookup_table.create(payer, payer, recent_slot=0)
extend_ix        = svm.address_lookup_table.extend(table, payer, [A, B], payer=payer)
deactivate_ix    = svm.address_lookup_table.deactivate(table, payer)
close_ix         = svm.address_lookup_table.close(table, payer, recipient)
freeze_ix        = svm.address_lookup_table.freeze(table, payer)

payer.tx(create_ix)
payer.tx(extend_ix)
```

| Builder | On-chain instruction | Notes |
|---|---|---|
| `create(authority, payer, recent_slot=None)` | `CreateLookupTable` | returns `(instruction, table_address)`; `recent_slot` must be in `SlotHashes` (defaults to the current slot); table address derives from `authority` + `recent_slot`; both must sign |
| `extend(table, authority, addresses, payer=None)` | `ExtendLookupTable` | appends `addresses`; `payer` funds any rent increase and must sign |
| `deactivate(table, authority)` | `DeactivateLookupTable` | begins deactivation |
| `close(table, authority, recipient)` | `CloseLookupTable` | closes a deactivated table, draining lamports to `recipient` |
| `freeze(table, authority)` | `FreezeLookupTable` | permanently freezes the table (no more extend/close) |

Because these go through the real program, a table you `create` + `extend` only becomes usable in a v0 transaction after its warmup slot — exactly as on mainnet.
