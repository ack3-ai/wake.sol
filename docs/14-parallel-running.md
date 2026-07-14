[← Index](./index.md)

# 14 · Parallel running

`solana-fuzzer test -P N` runs your tests across **N worker processes** at once. There are two reasons to reach for it:

- **More fuzzing, same wall clock.** By default every worker runs *every* test, each with its own random seed — so `-P 8` is eight independent fuzzing runs in parallel, eight different seeds exploring eight different sequences. This is the "leave it running overnight" mode.
- **Faster suites.** With `--dist uniform` the tests are *sharded* across workers instead of duplicated, so a large suite finishes in roughly `1/N` the time.

```bash
solana-fuzzer test -P 8 tests/test_fuzz_counter.py     # 8 seeds, same test
solana-fuzzer test -P 4 --dist uniform tests/          # shard the whole suite
solana-fuzzer test tests/                              # no -P → single process, as before
```

## How it works

One **server** process collects the tests but never runs them; it forks **N worker** processes, each a full, independent pytest session. Workers stream their results back to the server, which aggregates them into a single pass/fail summary and exit status. A worker failure makes the whole run exit nonzero, exactly like a normal `pytest`.

Everything is on the POSIX `fork` start method (the workers inherit the live in-process SVM and the result channels). **macOS and Linux only** — there is no Windows support, mirroring the single-process debugger.

## Seeds

Each worker gets its own base seed, so each explores a different sequence. Seeds are printed in the summary, one per worker, with a ready-to-paste reproduce line:

```
================================ solana-fuzzer =================================
Ran 8 workers, dist=duplicated. Per-worker seeds:
  #0: 3d4ad2957d262442   (reproduce: pytest --seed 3d4ad2957d262442)
  #1: a1f0c39b7712de08   (reproduce: pytest --seed a1f0c39b7712de08)
  ...
```

To reproduce a failure, take the failing worker's seed and re-run it single-process:

```bash
pytest --seed 3d4ad2957d262442 "tests/test_fuzz_counter.py::test_fuzz_counter"
```

Reproduction works because per-test seeding is derived — `sha256(base_seed + nodeid)` — so a single seed reproduces a single test regardless of how it was scheduled (see [§8 → Randomness & reproducibility](08-fuzzing.md#randomness--reproducibility)).

Pin specific worker seeds with `-S <hex>` (repeatable); any remaining workers get a random seed:

```bash
solana-fuzzer test -P 4 -S 3d4ad2957d262442 -S a1f0c39b7712de08 tests/
```

## Options

| Option | Meaning |
| --- | --- |
| `-P, --proc N` | Run across N workers. Bare `-P` (no number) uses one worker per CPU. Omit entirely for single-process. |
| `--dist duplicated` | *(default)* Every worker runs every test — N seeds, N runs. |
| `--dist uniform` | Shard the collected tests into contiguous slices across workers — wall-clock speed. |
| `-S, --seed <hex>` | Pin a worker's base seed (repeatable). Remaining workers are seeded randomly. |
| `-d, --attach` | Drop into an interactive ipdb post-mortem on failure (and on `breakpoint()`). See [Interactive debugging](#interactive-debugging-under--p). Mutually exclusive with `--attach-first`. |
| `--attach-first` | Stream worker 0's output live to the console (the rest go to their logs) and never prompt. Mutually exclusive with `--attach`. |

Any other arguments (paths, `-k`, `-v`, …) are forwarded to every worker's pytest session unchanged.

## Interactive debugging under `-P`

`--attach`/`-d` gives you the same ipdb post-mortem as single-process mode, across processes. When a worker's test raises — or calls `breakpoint()` — it pauses, ships the traceback (or a source snippet) back to the server, and the server prints it and asks:

```
Worker #1 raised the exception above.
Attach the debugger? [y/n]
```

Answer `y` and you land in ipdb **inside that worker**, at your frame, with the live objects reachable (a failed transaction is bound to `__exception__`, so `__exception__.tx`, `.call_trace`, `.code`, … all work — exactly as single-process). The rest of the run is paused until you `continue`/`quit`; then the progress display resumes. Answer `n` to let the run carry on and report the failure normally.

A couple of practical notes:

- **The prompt only appears on a real terminal.** If the run's stdin is not a tty (CI, a pipe, output redirected), or you passed `--attach-first`, every attach is **auto-declined** — the run never blocks waiting for input it can't get. The failure is still reported, with its reproduce line.
- **`breakpoint()` works even without `--attach`.** It always negotiates: on a terminal you're asked whether to attach; off one it's auto-continued. So a stray `breakpoint()` can't wedge a parallel run.
- A `TransactionFailed` carries Rust-side objects that can't cross the process boundary, so the server renders a text view of it; the *interactive* session runs in the worker, which still holds the real objects.

To reproduce a single failure in isolation instead, take the failing worker's seed and re-run it single-process with `--attach` (see [Seeds](#seeds)).

## `--attach-first`: watch one worker live

`--attach-first` is the "just let me watch it run" mode. Worker 0's stdout/stderr are *teed* to the console (and still saved to `process-0.ansi`); every other worker redirects to its log as usual. The progress table is turned off (worker 0 owns the console), and no worker ever prompts to attach. It's mutually exclusive with `--attach`.

```bash
solana-fuzzer test -P 4 --attach-first tests/test_fuzz_counter.py
```

## Output & logs

The console shows a live progress row per worker in a terminal, and degrades to plain per-worker status lines when it isn't one (CI, a pipe). The full pass/fail detail and summary are aggregated once, at the end, from all workers.

Each worker's own stdout/stderr is redirected to a per-worker log file so the console stays readable:

```
.solana-fuzzer/logs/testing/process-0.ansi
.solana-fuzzer/logs/testing/process-1.ansi
...
```

The directory is wiped at the start of every parallel run. Open a worker's `.ansi` file (e.g. `less -R`) to see its full session output, including its own per-run flow-stats tables.

## Aggregated fuzz stats

When you fuzz under `-P`, each worker runs the fuzz test with its own seed and keeps a per-flow stats registry (picked / ran / skipped, plus soft-skip reasons). At the end of the run the server **merges all the workers' registries** and prints one aggregated table per `FuzzTest` class — the counts are the sum across every worker:

```
BadFuzz — aggregated flow stats across 2 workers (4 sequences, 40 flow-steps)
┏━━━━━━┳━━━━━━━━┳━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ flow ┃ picked ┃ ran ┃ skipped ┃ skip reasons ┃
┡━━━━━━╇━━━━━━━━╇━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ inc  │      6 │   6 │       0 │              │
└──────┴────────┴─────┴─────────┴──────────────┘
```

The label shows the summed budget — `N sequences` and `N × flows_count` flow-steps across the workers. It's the same table you'd see single-process (see [§8 → Reading the output](08-fuzzing.md#reading-the-output)), just totalled across the parallel runs, so dead-flow warnings still fire on the aggregate. Each worker's own per-run table is still in its `.ansi` log if you want the per-seed breakdown.

## Crash logs

When a `FuzzTest` fails, the harness writes a JSON crash log next to the seed-based reproduce line — a convenience artifact you can read, diff, or archive (reproduction is still just the seed). Under `-P` each worker writes into its own directory so filenames never collide:

```
.solana-fuzzer/logs/crashes/process-0/20260713_200848_872928_test_smoke_fuzz.py__test_bad.json
```

and the server lists them in its summary:

```
Crash logs:
  #0 test_smoke_fuzz.py::test_bad: .solana-fuzzer/logs/crashes/process-0/2026...json
  #1 test_smoke_fuzz.py::test_bad: .solana-fuzzer/logs/crashes/process-1/2026...json
```

Each file records the failing node id, the worker's seed, the `pytest --seed …` reproduce line verbatim, the `FuzzTest` class, the failing sequence/flow index, the failing step (`"flow increment"` / `"invariant counter_matches_model"`), the flow trace, and the exception's type and value. Non-fuzz test failures write nothing. Crash logs are best-effort — a failure to write one never masks the underlying test failure. See [§8 → Crash logs](08-fuzzing.md#crash-logs) for the single-process form.

## Notes & limits

- **Collection must be deterministic.** Every worker collects the tests independently; the server asserts they all collected the *identical* set and aborts otherwise. Don't parametrize on `os.getpid()`, wall-clock, or unseeded entropy.
- **Interactive `--attach` works under `-P`** (see [Interactive debugging](#interactive-debugging-under--p)), but only prompts on a real terminal — off a tty it auto-declines. POSIX only: the cross-process handoff relies on the worker inheriting the terminal across `fork`.
- **Ctrl+C** stops all workers cleanly and still prints the summary of whatever finished.

## Where to go next

- Write the fuzz tests you'll run in parallel → [§8 Fuzzing](08-fuzzing.md)
- Reproduce and debug a single failing seed → [§8 → Randomness & reproducibility](08-fuzzing.md#randomness--reproducibility)
