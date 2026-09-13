# 15 · Coverage

Coverage answers the question a green suite does not: **which lines of the program did these tests actually execute?** It is collected from the SVM itself — agave's VM records the program counter of every instruction it runs — and mapped back to `file:line` through the program's own DWARF.

```
wake-sol test --cov
```

```
Coverage:
  programs/native-counter/src/lib.rs    12/14     85.7%
```

## Building the program under test

`cargo build-sbf` strips debug information by default, and coverage cannot invent it. Two changes are needed, and both matter.

**1. Emit DWARF.** In the *program's* `Cargo.toml`:

```toml
[profile.release]
debug = true
```

This does not change how the program is deployed: the build still writes an optimized, stripped `target/deploy/<name>.so`, and puts the debug information in `target/sbpf-solana-solana/release/<name>.so` beside it.

**2. Keep the paths.** Build with `--disable-remap-cwd`:

```bash
cargo build-sbf --disable-remap-cwd
```

Without it the toolchain strips `DW_AT_comp_dir`, and every crate records its sources as a bare relative path. In a minimal program eighteen crates call their source `src/lib.rs` — the program's, `borsh`'s, `sha2`'s — and there is no longer anything in the file that tells them apart. wake.sol falls back to matching by crate name and discards anything that lands past the end of a file, so you get a usable report rather than a wrong one, but attribution is only exact with this flag.

**3. Turn the optimizer down**, or a good part of the file will simply not be
in the report:

```toml
[profile.release]
debug = true
lto = "off"
opt-level = 0
```

This is the change people skip, and it is the usual answer to "why isn't it
highlighting all my lines". At the default optimization the compiler merges and
reorders enough that many statements get no line-table row at all — and a line
with no row is *absent*, which in an editor looks the same as untested. On the
14-statement `native-counter` fixture, the default build maps **9**; five
statements, including `let counter = next_account_info(account_iter)?` and the
`if counter.owner != program_id` check, vanish entirely. Unoptimized, all 14 map.

The trade is real: the same program burns roughly **5× the compute units**
unoptimized (measured on SPL Token). So this is a build for measuring coverage,
not for judging CU budgets or for shipping — run your normal suite against the
production profile and coverage against this one. If your program is heavy
enough that `opt-level = 0` breaks the per-instruction CU limit, `opt-level = 1`
is the fallback: it costs ~1.35× CU and recovers inlined helpers, though it
buys back little of the line mapping.

## Deploying so coverage can find the symbols

The debug build **cannot be deployed**. With tracing on, agave's loader walks the ELF's symbol table and rejects any program whose function symbols fall outside `.text` — an unstripped build trips this and fails to load as `InvalidAccountData`, but only under `--coverage`, which makes it a puzzling failure to meet by accident.

So the two artifacts have separate jobs: deploy the stripped one, resolve against the unstripped one. Deploy from a path and this is automatic:

```python
svm.add_program_from_file(PROGRAM_ID, "target/deploy/mine.so")
```

Deploying from bytes leaves nothing to search, so register the debug build yourself:

```python
from wake_sol import coverage

svm.add_program(PROGRAM_ID, so_bytes)
coverage.add_debug_elf(PROGRAM_ID, "target/sbpf-solana-solana/release/mine.so")
```

A sidecar is used only if its `.text` matches the deployed program. A stale one is reported as a mismatch and skipped rather than resolved into confident, wrong line numbers.

## Running

```bash
wake-sol test --cov
```

That is the whole thing: it collects, prints the per-file table, and keeps
`.wake-sol/lcov.info` refreshed as the run proceeds.

| | |
|---|---|
| `--cov`, `--coverage` | collect, print the table, write a live report. Takes an optional worker count — see below. `0` turns it off |
| `--coverage-report PATH` | write the report somewhere else than `.wake-sol/lcov.info` |
| `--coverage-source DIR` | limit to sources under `DIR`, repeatable (default: rootdir) |
| `--coverage-sync SECONDS` | how often to refresh (default: 5; `0` writes only at the end) |

`--cov` is the short spelling of `--coverage`, registered only when **pytest-cov**
is absent: that plugin owns the name, and two plugins claiming one option aborts
the whole run. With pytest-cov installed use `--coverage` — the two measure
different things (your Python tests vs. the SBF program) and a project may
reasonably want both.

### Choosing how many workers to trace

`--cov` takes an optional number: how many `-P` workers collect coverage.

```bash
wake-sol test -P 8 --cov      # all eight workers trace
wake-sol test -P 8 --cov 2    # two trace; the other six run at full speed
```

This matters because tracing is not free — between 1.3× and 40× depending on how
much compute a transaction does (see *Cost* below). For a long fuzzing campaign,
tracing one or two workers gives you the coverage picture while the rest explore
at full speed. Asking for more collectors than there are workers is an error.

Because the option takes an optional value, keep a bare `--cov` away from a
following path — `pytest --cov tests/foo.py` tries to read `tests/foo.py` as the
count. Put `--cov` last, or give it an explicit number.

## Watching a run live

Counts accumulate continuously — the collector updates them after every transaction — so a report is meaningful at any moment. `--cov` refreshes it **every 5 seconds**, which an editor can follow:

```bash
wake-sol test --cov                        # live, every 5s, to .wake-sol/lcov.info
wake-sol test --cov --coverage-sync 1      # more often
wake-sol test --cov --coverage-sync 0      # end of run only
wake-sol test --cov --coverage-report build/lcov.info   # somewhere else
```

A report is written immediately when the run starts, then on the interval, then once more at the end — so a run shorter than the interval still produces two.

Under `-P` each collecting worker sends snapshots on that interval and the server rewrites the merged report as they arrive, so the live view covers the whole run rather than one worker.

**Cost.** An export is ~4 ms for a program the size of SPL Token:

| interval | overhead |
|---|---|
| 5 s (default) | 0.08% |
| 1 s | 0.41% |
| 0.25 s | 1.7% |

The first export is dearer (~14 ms — it parses the program's DWARF), which is why it happens up front rather than mid-run; every later one reuses the parsed line table. The default is well under the noise floor, so choose the interval for how fresh you want the editor to be, not for the cost.

## Merging

Counters live in the process that ran the transactions, so anything parallel produces several sets of counts.

Under `wake-sol test -P N` this is handled for you: each collecting worker ships its counts to the server, which merges them and writes **one** report and one table.

```
Coverage (merged from 3 of 3 workers):
  programs/native-counter/src/lib.rs    12/14     85.7%
  wrote .wake-sol/lcov.info (1 file(s))
```

The "3 of 3" is how many workers reported, against how many ran — `--cov 1 -P 3` reads "1 of 3".

For anything else — CI shards, or the same suite run several times with different seeds:

```bash
wake-sol coverage merge shard-*.lcov -o coverage.lcov
```

Counts **sum**. That is the correct merge and not an arbitrary choice: a line's count is the maximum over the separate blocks it owns, which is right within one execution context because a single pass enters all of them — but two processes running different transactions are disjoint events that add. Merging the underlying instruction counters first and folding afterwards would take a maximum *across* processes and undercount.

### Merging across builds

Line numbers from two different builds of a program are not comparable, and a report that mixes them looks perfectly healthy. Every report therefore gets a `<report>.meta.json` recording the SHA-256 of each program's deployed `.text`, and `merge` refuses when two inputs disagree:

```
Error: b.lcov and a.lcov contain different builds of program Token… (69c7826f97af… vs
8883e6404c18…). Their line numbers describe different code and must not be summed.
```

`--force` downgrades that to a warning. Reports from other tools have no sidecar; they merge with a warning that the check could not be made.

One thing the check cannot catch: `SF:` paths must agree. Shards that check out to different directories produce two half-covered entries per file rather than one merged entry.

## Viewing it

The output is plain LCOV, so nothing here is wake.sol-specific and none of it needs an extension written for this project.

**In VS Code — [Coverage Gutters](https://marketplace.visualstudio.com/items?itemName=ryanluker.vscode-coverage-gutters).** The recommended one, and it needs no configuration on either side: its default `coverageFileNames` includes `lcov.info`, and its search covers `.wake-sol/`, so it finds the report where `--cov` already writes it. Run *Coverage Gutters: Watch* and the marks follow the run as it goes.

```bash
wake-sol test --cov     # then: Coverage Gutters: Watch
```

It draws its own gutter decorations rather than using VS Code's native coverage view.

**The native coverage view** — counts on the line numbers, the Test Coverage panel — is reachable through [Import lcov](https://marketplace.visualstudio.com/items?itemName=gregoire.import-lcov), which imports an LCOV file into it. Be aware it will not follow a live run: it watches for the file being *created* or *deleted* and deliberately ignores modifications (`ignoreChangeEvents` is `true` in its watcher), so an in-place refresh goes unnoticed. Good for looking at a finished run, not for watching one.

**As HTML**, for a report you can hand to someone else:

```bash
genhtml --output-directory htmlcov .wake-sol/lcov.info
```

**In CI**, any LCOV consumer — codecov, coveralls, sonar — takes it as-is.

## By hand

Register tracing is compiled into a program when it loads, so coverage has to be armed before the SVM that loads it exists. `--cov` handles this; driving it yourself takes one extra step:

```python
from wake_sol import coverage, svm

coverage.enable()
svm.reset()                       # rebuild the SVM with tracing on
...                               # deploy, run transactions
print(coverage.summary())
coverage.write_lcov("coverage.lcov")
coverage.disable()                # tracing is not free; turn it back off
```

`coverage.report()` returns the underlying data — `{"files": {path: {line: hits}}, "programs": [...]}` — where each program entry carries `instructions_covered` / `instructions_total` (useful even with no debug info at all), `has_debug_info` and `text_mismatch`.

## What the numbers mean

A line's count is **how many times that line ran**, matching what LCOV and `genhtml` expect. Getting there is less obvious than it sounds, because a source line compiles to many instructions: charging each instruction reports a one-line `msg!` as having run eleven times for a single transaction, which is the documented behaviour of every other DWARF-based tool in this space.

wake.sol counts statement boundaries instead — the `is_stmt` rows of the line table — and takes the maximum across the separate blocks a line owns, since a single pass through a line enters all of them. Three transactions through a `msg!` report 3.

The residue, worth knowing before you read too much into a number:

- **A line reached through genuinely distinct blocks reports the busiest one, not the total.** A function inlined at several call sites is the usual case. This errs toward claiming less coverage than you have, which is the safe direction — and it does not arise at `opt-level = 0`.
- **Inlined code is credited to the library it came from**, then dropped for being outside your sources. Every tool in this space behaves this way; it matters little at `opt-level = 0`, where nothing is inlined.
- **Zero hits on a line that clearly ran** usually means the optimizer merged it into a neighbour. Lower `opt-level` before concluding there is a gap in the tests.

## Cost

Tracing appends the register file on every executed instruction, in the JIT as well as the interpreter. The cost is therefore **per instruction executed, not per transaction** — measured at about **5.8 ns per instruction**, against a baseline SBF execution rate of roughly 0.1 ns per instruction. Tracing makes execution itself ~60× slower; what you actually observe depends on how much of a transaction is execution:

| compute units / tx | untraced | traced | slowdown |
|---|---|---|---|
| 342 | 9.8 µs | 13.1 µs | 1.3× |
| 1,143 | 9.9 µs | 16.6 µs | 1.7× |
| 8,343 | 7.6 µs | 65 µs | 8.6× |
| 80,343 | 14.7 µs | 477 µs | 33× |
| 160,343 | 22.5 µs | 943 µs | 42× |

(SVM engine time per transaction, `sigverify` off. With signature verification on — the default — its ~35 µs dwarfs everything below about 10k CU, so the same runs read 1.1× to 20×.)

Memory follows the same shape: ~96 bytes per executed instruction, held for the lifetime of a transaction, so a 1.4M-CU transaction can peak north of 100 MB.

The practical reading: coverage is close to free on ordinary unit tests, and expensive on compute-heavy ones. For a long fuzzing campaign leave it off and collect coverage on a shorter, separate run.

## Where to go next

- Drive more of the program before measuring → [§8 Fuzzing](08-fuzzing.md)
- Collect across N seeds at once → [§14 Parallel running](14-parallel-running.md)
