[← Index](./index.md)

# 5 · The SVM & sysvars

`svm` is the process-global [`LiteSVM`](https://github.com/LiteSVM/litesvm) instance. Make independent chains with `LiteSVM()` and bind accounts to them with `Account(addr, svm=other)`.

```python
from solana_fuzzer import svm, LiteSVM
chain_b = LiteSVM()                  # separate state
chain_b = LiteSVM(sigverify=False, blockhash_check=False)
```

## Config toggles

```python
svm.sigverify = False           # skip signature verification
svm.blockhash_check = False     # skip recent-blockhash check
svm.transaction_history = False # allow duplicate (byte-identical) transactions
```

Handy for fuzzing (e.g. submitting txs with deliberately wrong signers). Each is also a constructor keyword (`LiteSVM(sigverify=False, …)`), toggles in place preserving account state, and — under pytest — resets to its default (`True` for all three) before each test.

### Transaction history & duplicate transactions

`transaction_history` controls litesvm's per-**signature** deduplication. A transaction's signature is fixed by its message (instructions + accounts + recent blockhash) and signers, so two *byte-identical* transactions have the *same* signature. With history on, the second is rejected as `AlreadyProcessed`; with it off, it runs again.

**The default differs by context — this is deliberate, and the subtle part:**

| Context | `transaction_history` | Behavior |
| --- | --- | --- |
| `LiteSVM()` / the global `svm` | **`True`** (default) | a repeated identical tx → `AlreadyProcessed`; `svm.get_transaction(sig)` works |
| inside `FuzzTest.run(...)` | **`False`** (default) | repeated identical txs just execute again |

Why the flip: a fuzz flow routinely re-sends the same instruction. On a real cluster you'd resubmit under a fresh recent blockhash (so a new signature); litesvm has no moving blockhash, so identical txs would otherwise collide. `run()` turns history off so a fuzzer can re-issue the same action freely. Override it per run with `MyFuzz.run(..., transaction_history=True)`, or set `svm.transaction_history` directly. The pytest plugin restores `True` before every test, so a fuzz run never leaks the setting into the next test.

Two things to know when it's off:

- A byte-identical transaction can apply **twice** — impossible on a real cluster, where a signature lands exactly once. That matches a fuzzer's intent (each flow = one real action), but **re-enable it (`transaction_history=True`) if you're specifically auditing replay / idempotency.**
- `svm.get_transaction(sig)` returns nothing (there's no history to look up). The `TransactionResult` returned by `tx()` / `simulate()` is unaffected.

If you want per-signature uniqueness *without* the dedup papercut, keep history on and rotate the blockhash yourself with `svm.expire_blockhash()` before each send — the faithful analogue of resubmitting on-chain.

## Accounts, programs, blockhash, slot

```python
svm.airdrop(addr, lamports)                         # credit lamports
svm.set_account(addr, lamports=…, data=…, owner=…)  # overwrite an account
svm.minimum_balance_for_rent_exemption(space)       # rent-exempt min for `space` bytes
svm.latest_blockhash()                              # current blockhash (bytes)
svm.expire_blockhash()                              # force a new blockhash
svm.warp_to_slot(slot)                              # jump the clock to `slot`
svm.add_program_from_file(program_id, "path/to.so") # deploy a BPF program
svm.add_program(program_id, so_bytes)
svm.send_transaction(tx_bytes); svm.simulate_transaction(tx_bytes)
```

Built-in program builders live here too: `svm.system` and `svm.token` (see [§7](07-programs-and-addresses.md)).

## Computing rent

You don't read the rent sysvar to size a deposit — ask the SVM:

```python
svm.minimum_balance_for_rent_exemption(0)     # 890880   (empty account)
svm.minimum_balance_for_rent_exemption(165)   # 2039280  (SPL token account)
```

## Sysvars

Cluster parameters (clock, rent, …) are exposed as typed objects. **Reads** go through litesvm's `get_sysvar`; **writes** through `set_sysvar`, which updates the cached sysvar the runtime actually uses during execution (not just the backing account). Setters are **partial** — only the keywords you pass change; the rest keep their values.

### Clock (time / slot travel)

```python
c = svm.clock        # Clock(slot, epoch, unix_timestamp, epoch_start_timestamp, leader_schedule_epoch)
c.unix_timestamp, c.slot, c.epoch

svm.set_clock(unix_timestamp=1_900_000_000, slot=12_345)   # epoch etc. untouched
svm.warp_to_timestamp(2_000_000_000)                       # set only block time
svm.warp_to_slot(500)                                      # jump slot (recomputes epoch)
```

### Rent

```python
r = svm.rent          # Rent(lamports_per_byte_year, exemption_threshold, burn_percent)
svm.set_rent(burn_percent=0)                # disable rent burn for a test
```

(For "what balance is rent-exempt", prefer `minimum_balance_for_rent_exemption` above.)

### Epoch schedule & last-restart slot

```python
es = svm.epoch_schedule   # EpochSchedule(slots_per_epoch, leader_schedule_slot_offset, warmup, …)
svm.set_epoch_schedule(warmup=False)

svm.last_restart_slot           # int
svm.set_last_restart_slot(7)
```

### Read-only sysvars

```python
svm.epoch_rewards        # EpochRewards(distribution_starting_block_height, num_partitions,
                         #              parent_blockhash: bytes, total_points, …)
svm.slot_hashes          # list[tuple[int, bytes]] — [(slot, hash_bytes), …], newest first
```

> The sysvar value objects (`Clock`, `Rent`, `EpochSchedule`, `EpochRewards`) are importable from `solana_fuzzer` for type annotations. `StakeHistory` isn't currently wrapped.
