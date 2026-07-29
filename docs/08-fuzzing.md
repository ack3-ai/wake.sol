[← Index](./index.md)

# 8 · Fuzzing

The harness has a small **stateful fuzzing** engine: you declare *actions* (`@flow`) and *properties* (`@invariant`) on a `FuzzTest` subclass, and the engine drives your program with long randomized sequences of actions, checking the properties after every step. When a property breaks — or a flow raises — it stops and hands you a reproducible failure.

```python
from wake_sol import FuzzTest, flow, invariant
```

The model:

- A **flow** is one state-mutating action (usually: build an instruction, send it). The engine picks flows at random (by weight) to form a sequence.
- An **invariant** is a property that must always hold (usually: on-chain state matches a Python model you maintain). It's checked between flows and should raise on violation.
- A **sequence** is one run of up to `flows_count` flows against a freshly-reset SVM. `run()` executes `sequences_count` of them.

All randomness — flow selection *and* whatever a flow draws — comes from the one process-global `wake_sol.random`, which the pytest plugin reseeds per test, so any failure reproduces from the base `--seed` alone.

## A complete example

Fuzzing the `native-counter` program (increments a program-owned `u64`). The full version lives in `tests/test_fuzz_counter.py`.

```python
from pathlib import Path
from wake_sol import Account, FuzzTest, Instruction, Pubkey, flow, invariant, random, svm, writable

SO = Path(__file__).parent.parent / "programs/native-counter/target/deploy/native_counter.so"
PROGRAM_ID = Pubkey(bytes([0xC0] * 32))


class CounterFuzz(FuzzTest):
    def pre_sequence(self):
        # The SVM is wiped to genesis before each sequence — rebuild the world.
        svm.add_program(PROGRAM_ID, SO.read_bytes())
        self.payer = Account.new()
        svm.airdrop(self.payer, 1_000_000_000)
        self.counter = Account.new()
        svm.set_account(self.counter, lamports=svm.minimum_balance_for_rent_exemption(8),
                        data=bytes(8), owner=PROGRAM_ID)
        self.model = 0                        # our shadow model of the counter

    @flow(weight=300)
    def increment(self):
        self.payer.tx(Instruction(PROGRAM_ID, [writable(self.counter)], b""))
        self.model += 1

    @flow(weight=100)
    def increment_batch(self):
        n = random.randint(2, 4)              # in-flow randomness, from the seeded RNG
        self.payer.tx(*[Instruction(PROGRAM_ID, [writable(self.counter)], b"") for _ in range(n)])
        self.model += n

    @invariant()
    def counter_matches_model(self):
        onchain = int.from_bytes(self.counter.data[:8], "little")
        assert onchain == self.model, f"on-chain {onchain} != model {self.model}"


def test_fuzz_counter():                      # a normal pytest test
    CounterFuzz.run(sequences_count=4, flows_count=15)
```

Run it with `pytest tests/test_fuzz_counter.py`; the flow-stats table below prints in the summary either way (no `-s` needed).

## Flows

`@flow` marks a method the engine may pick each step. It takes only `self` — there is **no** generated-argument magic; a flow draws whatever it needs from `wake_sol.random` in its own body.

```python
@flow(weight=100, max_times=None, precondition=None)
def my_action(self): ...
```

| Option | Meaning |
| --- | --- |
| `weight` | relative selection weight (default `100`); a `weight=300` flow is picked ~3× as often as a `weight=100` one |
| `max_times` | cap on executions **per sequence** (default: unlimited) |
| `precondition` | `lambda self: bool` — the flow is only eligible when it returns `True` |

**Soft-skip:** a flow may `return` a `str` to signal "nothing to do this step" (e.g. no eligible account yet). It's recorded as a skip under that reason and does **not** count toward `max_times`:

```python
@flow()
def withdraw(self):
    if not self.depositors:
        return "no_depositors"     # skip, with a reason that shows up in the stats
    ...
```

If no flow is eligible at a step (all capped or preconditions false), the engine raises with the list of what's blocked.

## Invariants

`@invariant` marks a property checked between flows. It should raise (e.g. `assert`) on violation.

```python
@invariant(period=1)
def my_property(self): ...
```

`period` runs the invariant every *N* flows (default `1` = after every flow). Use a larger period for an expensive check you don't need every step. Counters are per-sequence.

## The sequence lifecycle

Each sequence resets the SVM to genesis and then runs your hooks and flows in this order:

```
svm.reset()                      # wipe accounts to genesis (config + any fork kept)
pre_sequence()                   # ← you rebuild the world here
for each step (up to flows_count):
    pre_flow(flow); flow(self); post_flow(flow)
    pre_invariants()
      pre_invariant(inv); inv(self); post_invariant(inv)   # for each due invariant
    post_invariants()
post_sequence()
```

The engine keeps **one** `FuzzTest` instance for the whole run, and `reset()` wipes the SVM before every sequence — so **`pre_sequence` is where you rebuild per-sequence state**: redeploy your program, fund a payer, create accounts, and reset your Python model (`self.model = 0`, etc.). Anything you set in `__init__` persists across sequences; anything sequence-specific belongs in `pre_sequence`.

All hooks are optional no-ops by default. Inside any of them (and in flows/invariants) you can read `self.sequence_num` and `self.flow_num`.

| Hook | Fires |
| --- | --- |
| `pre_sequence` / `post_sequence` | once per sequence, around all its flows |
| `pre_flow(flow)` / `post_flow(flow)` | around each flow call |
| `pre_invariants` / `post_invariants` | around the invariant batch after each flow |
| `pre_invariant(inv)` / `post_invariant(inv)` | around each invariant that runs |

## Running it

```python
FuzzTest.run(sequences_count, flows_count, *, transaction_history=False)
```

Call it from inside an ordinary pytest test, so the plugin's per-test reset, seeding, and `--attach` all apply. `sequences_count × flows_count` is roughly your total action budget; more sequences = more independent starting points, more flows = deeper single runs.

> `transaction_history` defaults to `False` here — the *opposite* of a plain `LiteSVM` — so a fuzzer can re-issue byte-identical transactions without tripping litesvm's `AlreadyProcessed` dedup. This is subtle; see [§5 → Transaction history](05-svm-and-sysvars.md#transaction-history--duplicate-transactions). Pass `transaction_history=True` to keep the dedup (e.g. when auditing replay).

## Randomness & reproducibility

There is one RNG: `wake_sol.random` (a `random.Random`). Flow selection, your in-flow draws, and even `Account.new()`'s keypair generation all pull from it. The pytest plugin reseeds it before every test as `sha256(base_seed + nodeid)`, so a single test reproduces regardless of run order or selection.

```python
import wake_sol as sf
sf.random.randint(0, 100)      # use this, not the stdlib `random`, in flows
acct = sf.Account.new()        # keypair derived from the RNG → reproducible from the seed
```

The base seed is printed in the test summary. To replay a failing run exactly:

```bash
pytest --seed <hex> "tests/test_fuzz_counter.py::test_fuzz_counter"
```

> **Determinism is your responsibility inside flows.** Anything that pulls entropy from outside the seeded RNG — `time`, `os.urandom`, an unseeded `Keypair`, wall-clock — breaks reproducibility. Draw from `wake_sol.random` and control time with `svm.warp_to_slot` / `svm.warp_to_timestamp` (§5).

## Reading the output

Every run reports a per-flow **stats table** and flags dead flows, in the `wake.sol` section of pytest's summary — so it is always visible, pass or fail, with no `-s` needed:

```
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ flow            ┃ picked ┃ ran ┃ skipped ┃ skip reasons ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ increment       │     44 │  44 │       0 │              │
│ increment_batch │     16 │  16 │       0 │              │
└─────────────────┴────────┴─────┴─────────┴──────────────┘
```

- **picked** — chosen by the weighted draw; **ran** — actually executed (counts toward `max_times`); **skipped** — returned a soft-skip `str`, with the reason breakdown.
- A flow that was **never picked** (weight/precondition never let it run) or **picked but never ran** (always soft-skipped) is called out with `⚠` — the usual sign of a mis-set precondition or an always-bailing flow, i.e. a path you *think* you're covering but aren't.

On failure, the engine adds context before re-raising, and the plugin names the failing step in the summary:

```
Failed: CounterFuzz in invariant counter_matches_model at sequence 2, flow 7
Base seed: 3d4ad2957d262442
```

(The failing step is named as `flow <name>` or `invariant <name>`, whichever raised.)

Re-run with `pytest --seed <hex>` (add the test's node id to run only it) to reproduce exactly. Run with `--attach` to drop into an ipdb post-mortem at the failing frame — it prints the full `pytest --seed <hex> "<nodeid>"` line for you.

## Crash logs

A `FuzzTest` failure also writes a JSON crash log under `.wake-sol/logs/crashes/`, listed in the test summary as `Crash logs:`. It's a convenience artifact for reading, diffing, or archiving — reproduction is still just the seed. Each file records:

```json
{
  "nodeid": "tests/test_fuzz_counter.py::test_fuzz_counter",
  "seed": "3d4ad2957d262442",
  "reproduce": "pytest --seed 3d4ad2957d262442 \"tests/test_fuzz_counter.py::test_fuzz_counter\"",
  "fuzz_class": "CounterFuzz",
  "sequence": 2,
  "flow": 7,
  "failing": "invariant counter_matches_model",
  "trace": ["increment", "increment_batch", "..."],
  "exception": {"type": "AssertionError", "value": "on-chain 8 != model 9"}
}
```

The `reproduce` line is copy-paste ready. Only `FuzzTest` failures produce a crash log; ordinary test failures don't. Writing is best-effort — if the log can't be written it's skipped silently, never masking the real failure. Under `wake-sol test -P N` each worker writes into its own `crashes/process-<N>/` directory and the server lists them all (see [§14 → Crash logs](14-parallel-running.md#crash-logs)).

## Notes & limits

- **Failed transactions raise.** `tx()` raises `TransactionFailed` on error, so an unexpected failure propagates and is reported as a found bug. For a flow that *expects* a failure (negative testing), catch it locally with `may_fail` / `must_fail` (from `wake_sol`) — see [§11](11-errors.md).
- **The fuzzer is random, not coverage-guided.** Selection is weighted-random; there's no feedback loop steering it toward new code. Use `weight`, `precondition`, and the stats table to make sure the interesting flows actually run.
- **Run many seeds at once.** `wake-sol test -P N` launches N independent workers, each a full run with its own seed — the "N seeds overnight" use case. See [§14 Parallel running](14-parallel-running.md).
- **Not (yet) here:** shrinking of a failing sequence. A failure is reproduced from its seed + flow trace, not minimized.

## Where to go next

- Build the instructions your flows send → [§3 Transactions](03-transactions.md)
- Model and read account state for invariants → [§2 Accounts](02-accounts.md)
- Control time / blockhash / the `transaction_history` toggle → [§5 The SVM & sysvars](05-svm-and-sysvars.md)
