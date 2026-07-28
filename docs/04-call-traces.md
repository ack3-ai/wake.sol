[← Index](./index.md)

# 4 · Call traces

`res.call_trace` is the decoded tree of what the transaction actually executed: each top-level instruction, its CPIs (nested), the resolved accounts, the decoded arguments, and the program logs attributed to each node.

```python
res = alice.tx(svm.token.create_ata(alice, owner, mint))
print(res.call_trace)        # colored tree via Rich
```

```
✓ Transaction  16,517 CU
└── Associated Token Account Program::create()
      funder: alice [SW]
      ata: GWBn…Nn3x [W]
      mint: 8fdm…96At
      token_program: Token Program
      ▸ Create
    ├── Token Program::get_account_data_size()
    ├── System Program::create_account(lamports=2,039,280, space=165, …)
    └── Token Program::initialize_account3(owner=AmPH…5bQd)
```

## Rendering

`CallTrace` is Rich-renderable: in a Rich context (`rich.print`, a notebook, the terminal) it shows the colored tree above; `str(call_trace)` / `print(...)` renders the same tree as plain text. The root line shows ✓/✗, total compute units, and — on failure — the structured error. Per node, the tree also renders decoded **events** as `⚡ EventName(...)` lines and a program's **return value** as a `➞ <value>` line (see [§10](10-return-values-and-events.md)).

## Walking it programmatically

```python
ct = res.call_trace
ct.success                 # bool
ct.error                   # str | None
ct.compute_units_consumed  # int
ct.instructions            # list[TracedInstruction] (top-level)
len(ct); ct[0]             # it's also a sequence

def walk(node, depth=0):
    print("  " * depth, node.program_id, node.stack_height)
    for log in node.logs:          # program logs emitted *by this node*
        print("  " * depth, "  ⌐", log)
    for child in node.inner:       # CPIs
        walk(child, depth + 1)

for top in ct.instructions:
    walk(top)
```

### `TracedInstruction`

```python
node.program_id       # Pubkey
node.accounts         # list[AccountMeta] — resolved accounts, in instruction order
node.data             # bytes — instruction data
node.stack_height     # int — 1 = top-level, ≥2 = CPI depth
node.logs             # list[str] — program logs this invocation emitted directly
node.inner            # list[TracedInstruction] — child CPIs, in order

node.status           # "success" | "failed" | "unknown" (frame never closed)
node.compute_units    # int | None — cumulative CU for this frame (incl. its CPIs)
node.error            # str | None — the raw `failed: <msg>` text if this frame failed
node.raw_return_value # bytes | None — this frame's return data (§10)
node.events           # list — this frame's decoded events (§10)
node.events_raw       # list[bytes] — raw event payloads (disc ‖ Borsh) for this frame
```

`status` / `compute_units` / `error` are recovered best-effort from the log stream (the runtime's `invoke` / `consumed` / `success` / `failed` brackets); a value is `None` / `"unknown"` when the marker wasn't present (e.g. truncated logs).

## Per-node logs

Each node's `logs` are the lines that invocation emitted **itself** — not its children's, and not the runtime's `invoke`/`consumed`/`success`/`failed` scaffolding. The harness reconstructs this by aligning the flat log stream's `invoke [depth]` / `success` / `failed:` brackets to the call tree positionally. This is text only — events / return-data are **not** decoded into typed values here.

This is also where failure reasons live. The structured `res.error` (e.g. `InstructionError(0, ProgramFailedToComplete)`) says *which* instruction and *what kind*; the program's own message — a Rust `panicked at '…'`, an Anchor `Error Message: …`, an SPL `Error: …`, a System `Transfer: insufficient lamports …` — is attributed to the node that emitted it:

```
✗ Transaction  26,918 CU  InstructionError(0, ProgramFailedToComplete)
└── level2::withdraw(amount=1,000,000,001,113,600)
      authority: hacker [SW]
      ▸ withdraw 1000000001113600
      ▸ panicked at 'assertion failed: `(left == right)`
      ▸   left: `AtAb…Vrup`,  right: `6MeJ…UfS7`', level2/src/processor.rs:93:5
```

The full, raw, tx-wide log list is always on `res.logs`.

## Decoding without a transaction

The decode that powers the trace is also available directly:

```python
from wake_sol import decode_instruction
dec = decode_instruction(program_id, instruction_data, n_accounts)
dec.name           # instruction name, or None if undecodable
dec.args           # {arg_name: value}
dec.account_names  # role name per slot
```

Decoding is driven by the registered program interfaces — the built-ins plus any generated `pytypes` programs (see [§7](07-programs-and-addresses.md)). An instruction whose program isn't registered renders as name-only; a matched-but-undecodable body surfaces a visible `<undecodable: …>` marker rather than silently empty args.
