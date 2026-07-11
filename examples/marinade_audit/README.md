# Marinade audit-workflow example (live / manual)

A runnable, end-to-end demonstration of the forking + pytypes + decode
composition (handover 02) against a real mainnet program: **Marinade Finance**
(`MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD`).

> **Live, not CI.** This script talks to mainnet and is intentionally *not* in the
> hermetic test suite. It needs a mainnet RPC endpoint.

```sh
SOLANA_RPC_URL=<https-mainnet-rpc>  .venv/bin/python examples/marinade_audit/audit_marinade.py
```

## What it shows

| Stage | Leg exercised |
|-------|---------------|
| 0 | `svm.fork(url)` — enable mainnet forking |
| 1 | `svm.fork_programs(MARINADE)` pins the program (+ its ~2 MB programdata); `svm.forked_accounts()` reports what hydrated |
| 2 | `gen` the pytypes package from the captured IDL and import it (self-registration) |
| 3 | decode a **real on-chain** marinade instruction from raw tx bytes via the generated pytypes — the reality check |
| 4 | build a `deposit` with the generated builder, decode our own bytes back (encode leg == decode leg) |
| 5 | **drive a live `deposit` under the fork** (marinade state/pools hydrate on touch; we supply a funded payer + mSOL token account) and decode the resulting call trace |

Last verified run executed a 1 SOL deposit successfully (`success=True`), with
marinade minting mSOL via CPI to the Token program.

## Files

- `audit_marinade.py` — the example.
- `idls/MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD.json` — Marinade's Anchor IDL,
  captured from its **on-chain IDL account** (so the example is self-contained).
  Caveat: an on-chain IDL can lag the deployed bytecode.
- `.fork-cache/` — snapshot cache written on live runs (gitignored).

## Auditing your *own* build

This example forks the real deployed marinade to decode against it. To audit a
patched/instrumented build instead, swap Stage 0 for:

```python
svm.fork(url, exclude=[MARINADE])      # blanks the audited program's code + state
svm.add_program(MARINADE, my_elf)      # deploy your build at the same address
```

Its real mainnet dependencies (state, pools, mints) still fork in around it.
