"""Stateful fuzzing engine: ``FuzzTest`` + the ``@flow`` / ``@invariant`` model.

A fuzz test is a class subclassing :class:`FuzzTest`. You mark state-mutating
actions with :func:`flow` and property checks with :func:`invariant`, then call
``MyFuzz.run(sequences_count, flows_count)`` from inside an ordinary pytest test.

Each *sequence* is an independent run of up to ``flows_count`` randomly-chosen
flows against a freshly-reset SVM; invariants are checked between flows. All
randomness — flow choice here, plus whatever a flow draws — comes from the one
process-global ``solana_fuzzer.random`` (keypairs included, see
``Account.new``), which the pytest plugin reseeds per test. So a failure
reproduces from the base ``--seed`` alone.

Deliberately *not* ported from wake (feat/version-5.0.0 `wake/testing/fuzzing`):
shrinking and type-driven parameter generation. Flows take no generated
arguments — a flow draws whatever it needs from ``solana_fuzzer.random`` in its
own body.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Callable, DefaultDict, Dict, List, Optional


def flow(
    *,
    weight: int = 100,
    max_times: Optional[int] = None,
    precondition: Optional[Callable[["FuzzTest"], bool]] = None,
):
    """Mark a method as a fuzz *flow* — a state-mutating action the engine may
    pick each step. ``weight`` biases selection; ``max_times`` caps how often it
    runs per sequence; ``precondition(self) -> bool`` gates it on current state.

    A flow may return a ``str`` to signal "did nothing this step" (e.g. no
    eligible account): it is recorded as a skip and does *not* count toward
    ``max_times``.
    """

    def decorator(fn):
        fn.flow = True
        fn.weight = weight
        if max_times is not None:
            fn.max_times = max_times
        if precondition is not None:
            fn.precondition = precondition
        return fn

    return decorator


def invariant(*, period: int = 1):
    """Mark a method as an *invariant* — a property checked between flows, every
    ``period`` flows (``period=1`` = after every flow). It should raise on
    violation (e.g. via ``assert``)."""

    def decorator(fn):
        fn.invariant = True
        fn.period = period
        return fn

    return decorator


class FuzzTest:
    """Base class for a stateful fuzz test. Subclass it, add ``@flow`` /
    ``@invariant`` methods and optionally override the lifecycle hooks, then run
    it with ``MyFuzz.run(sequences_count, flows_count)``.

    The engine keeps a single instance across all sequences, so ``pre_sequence``
    is where you rebuild per-sequence state — redeploy your program, fund
    accounts, and reset any Python model state — since ``solana_fuzzer.svm`` is
    wiped to genesis before each sequence.
    """

    _sequence_num: int
    _flow_num: int

    @property
    def sequence_num(self) -> int:
        """0-based index of the sequence currently running."""
        return self._sequence_num

    @property
    def flow_num(self) -> int:
        """0-based index of the flow currently running within the sequence."""
        return self._flow_num

    @classmethod
    def run(
        cls,
        sequences_count: int,
        flows_count: int,
        *,
        transaction_history: bool = False,
    ) -> None:
        """Run ``sequences_count`` sequences of up to ``flows_count`` flows each.

        Subtlety worth reading — ``transaction_history`` defaults to ``False``
        **here**, the *opposite* of a plain ``LiteSVM`` (default ``True``). Off
        lets the SVM accept repeated byte-identical transactions, so a fuzz flow
        can re-issue the same action without tripping litesvm's
        ``AlreadyProcessed`` dedup (litesvm has no moving blockhash, so identical
        txs would otherwise collide on signature). Pass ``transaction_history=True``
        to keep the dedup — e.g. when auditing replay/idempotency; with it off a
        byte-identical tx can apply twice, which a real cluster forbids. The
        pytest plugin restores the SVM's ``True`` default before the next test,
        so this never leaks. See ``docs/05-svm-and-sysvars.md``.
        """
        _run(cls, sequences_count, flows_count, transaction_history)

    # --- lifecycle hooks (override as needed; all no-ops by default) --------- #

    def pre_sequence(self) -> None: ...
    def post_sequence(self) -> None: ...

    def pre_flow(self, flow: Callable) -> None: ...
    def post_flow(self, flow: Callable) -> None: ...

    def pre_invariants(self) -> None: ...
    def post_invariants(self) -> None: ...

    def pre_invariant(self, invariant: Callable) -> None: ...
    def post_invariant(self, invariant: Callable) -> None: ...


def _methods_with(cls: type, attr: str) -> List[Callable]:
    """The class's methods (as functions) tagged with truthy ``attr``, ordered
    by name for deterministic selection given a fixed seed."""
    out = []
    for name in sorted(dir(cls)):
        m = getattr(cls, name, None)
        if callable(m) and getattr(m, attr, False):
            out.append(m)
    return out


def _run(cls: type, sequences_count: int, flows_count: int, transaction_history: bool) -> None:
    # Imported here (not at module load) to avoid a circular import: the
    # `solana_fuzzer` package imports this module to re-export FuzzTest.
    import solana_fuzzer as sf

    # Clear any prior failure context so a passing run — or a non-fuzz test that
    # fails later in the same process — never inherits a stale one. The pytest
    # plugin also clears it per test (belt and suspenders for non-pytest callers).
    clear_last_fuzz_failure()

    if not issubclass(cls, FuzzTest):
        raise TypeError(f"{cls.__name__} must subclass FuzzTest")

    # Off by default: let a fuzzer re-issue identical actions without hitting
    # AlreadyProcessed. reset() preserves this, so set it once for the run.
    sf.svm.transaction_history = transaction_history

    instance = cls()
    flows = _methods_with(cls, "flow")
    invariants = _methods_with(cls, "invariant")
    if not flows:
        raise ValueError(f"{cls.__name__} defines no @flow methods")

    # Per-flow diagnostics, rendered as a table when the run ends (pass or fail).
    stats: dict = {
        f.__name__: {"picked": 0, "ran": 0, "skipped": 0, "reasons": defaultdict(int)}
        for f in flows
    }

    for seq in range(sequences_count):
        # Wipe to genesis (keeps fork config; forked accounts re-hydrate from
        # the disk cache on next touch), then let the test rebuild its world.
        sf.svm.reset()
        instance._sequence_num = seq
        instance._flow_num = 0
        instance.pre_sequence()

        flows_counter: DefaultDict[Callable, int] = defaultdict(int)
        invariant_periods: DefaultDict[Callable, int] = defaultdict(int)
        trace: List[str] = []
        current = ""

        try:
            for step in range(flows_count):
                instance._flow_num = step

                valid = [
                    f
                    for f in flows
                    if (not hasattr(f, "max_times") or flows_counter[f] < f.max_times)
                    and (not hasattr(f, "precondition") or f.precondition(instance))
                ]
                if not valid:
                    capped = [f.__name__ for f in flows if hasattr(f, "max_times") and flows_counter[f] >= f.max_times]
                    gated = [f.__name__ for f in flows if hasattr(f, "precondition") and not f.precondition(instance)]
                    raise RuntimeError(
                        "no runnable flow this step.\n"
                        f"  reached max_times: {capped}\n"
                        f"  precondition false: {gated}"
                    )

                chosen = sf.random.choices(valid, weights=[f.weight for f in valid])[0]
                current = f"flow {chosen.__name__}"
                trace.append(chosen.__name__)
                st = stats[chosen.__name__]
                st["picked"] += 1

                instance.pre_flow(chosen)
                ret = chosen(instance)
                instance.post_flow(chosen)

                if isinstance(ret, str):
                    st["skipped"] += 1
                    st["reasons"][ret] += 1
                else:
                    flows_counter[chosen] += 1
                    st["ran"] += 1

                instance.pre_invariants()
                for inv in invariants:
                    if invariant_periods[inv] == 0:
                        current = f"invariant {inv.__name__}"
                        instance.pre_invariant(inv)
                        inv(instance)
                        instance.post_invariant(inv)
                    invariant_periods[inv] += 1
                    if invariant_periods[inv] >= inv.period:
                        invariant_periods[inv] = 0
                instance.post_invariants()

            instance.post_sequence()
        except Exception:
            # Record the failure context (read by the pytest plugin to write a
            # crash-log JSON) and fold this run's partial stats into the session
            # registry, then re-raise so the plugin prints the `--seed` reproduce
            # line and (with --attach) drops into ipdb.
            _set_last_fuzz_failure(
                fuzz_class=cls.__name__,
                sequence_num=instance._sequence_num,
                flow_num=instance._flow_num,
                failing=current,
                trace=list(trace),
            )
            sf.print(
                f"\n[fuzz] {cls.__name__} failed in {current} "
                f"at sequence {instance._sequence_num}, flow {instance._flow_num}"
            )
            sf.print(f"[fuzz] sequence flow trace: {' -> '.join(trace)}")
            _print_stats(sf, cls.__name__, sequences_count, flows_count, stats)
            record_run_stats(cls.__name__, sequences_count, flows_count, stats)
            raise

    _print_stats(sf, cls.__name__, sequences_count, flows_count, stats)
    record_run_stats(cls.__name__, sequences_count, flows_count, stats)


def build_stats_table(title: str, flows_stats: dict):
    """Build the flow-stats rich ``Table`` + dead-flow warnings from a
    ``{flow: {picked, ran, skipped, reasons}}`` mapping. Shared by the
    single-process per-run table (:func:`_print_stats`) and the multiprocess
    server's aggregated table, so both render identically.

    Columns: ``picked`` (chosen by the weighted draw), ``ran`` (executed and
    mutated state — counts toward ``max_times``), ``skipped`` (returned a ``str``
    soft-skip), and the breakdown of those skip reasons. A flow never picked, or
    picked but never run, is called out — the usual sign of a mis-set
    ``precondition``/``weight`` or an always-bailing flow, i.e. a path the fuzzer
    thinks it covers but doesn't.
    """
    from rich.table import Table

    table = Table(title=title, title_justify="left", header_style="bold")
    table.add_column("flow")
    table.add_column("picked", justify="right")
    table.add_column("ran", justify="right")
    table.add_column("skipped", justify="right")
    table.add_column("skip reasons")

    warnings: List[str] = []
    for fname in sorted(flows_stats):
        s = flows_stats[fname]
        reasons = ", ".join(f"{r}:{c}" for r, c in sorted(s["reasons"].items()))
        table.add_row(fname, str(s["picked"]), str(s["ran"]), str(s["skipped"]), reasons)
        if s["picked"] == 0:
            warnings.append(
                f"flow '{fname}' was never picked (weight/precondition never let it run)"
            )
        elif s["ran"] == 0:
            warnings.append(
                f"flow '{fname}' was picked {s['picked']}x but never ran (always soft-skipped)"
            )
    return table, warnings


def _print_stats(sf, name: str, sequences_count: int, flows_count: int, stats: dict) -> None:
    """Render the per-flow diagnostics table + dead-flow warnings, on every run
    (pass or fail). pytest captures stdout, so it surfaces on failure or under
    ``-s`` / ``-v``. Single-process UX; unchanged by the session registry below.
    """
    title = f"{name} — flow stats ({sequences_count} sequences x {flows_count} flows)"
    table, warnings = build_stats_table(title, stats)
    sf.print(table)
    for w in warnings:
        sf.print(f"[fuzz] ⚠ {w}")


# --------------------------------------------------------------------------- #
# Session-level bookkeeping for the multiprocess runner (``solana-fuzzer test
# -P N``). Purely additive: it accumulates per-FuzzTest-class stats across every
# ``_run`` in the session so a worker can ship its registry to the server, which
# merges N registries and renders one aggregated table. Nothing here prints in
# single-process — the per-run table above is the only single-process UX.
# --------------------------------------------------------------------------- #

#: ``{class_name: {"sequences": int, "steps": int,
#:                 "flows": {flow_name: {"picked", "ran", "skipped",
#:                                       "reasons": {reason: count}}}}}``.
#: ``sequences``/``steps`` are the summed budget (sequences_count and
#: sequences_count × flows_count) — enough to label the aggregate.
_session_stats: Dict[str, dict] = {}

#: The most recent FuzzTest failure's context, or ``None``. Set by ``_run``'s
#: except block, cleared at the start of each ``_run`` and each test.
_last_fuzz_failure: Optional[dict] = None


def _merge_flow_leaves(dst: dict, src: dict) -> None:
    """Sum ``src``'s per-flow leaves (picked/ran/skipped and each skip reason)
    into ``dst`` in place. ``src`` may be a live ``_run`` stats dict (``reasons``
    a defaultdict) or a registry entry's ``flows`` (``reasons`` a plain dict)."""
    for fname, s in src.items():
        leaf = dst.setdefault(
            fname, {"picked": 0, "ran": 0, "skipped": 0, "reasons": {}}
        )
        leaf["picked"] += s.get("picked", 0)
        leaf["ran"] += s.get("ran", 0)
        leaf["skipped"] += s.get("skipped", 0)
        for reason, count in s.get("reasons", {}).items():
            leaf["reasons"][reason] = leaf["reasons"].get(reason, 0) + count


def record_run_stats(name: str, sequences_count: int, flows_count: int, stats: dict) -> None:
    """Fold one ``_run``'s per-flow stats into the session registry (both the
    pass and fail paths). Additive across the whole session, so N duplicated
    workers each contribute and the server merges them."""
    entry = _session_stats.setdefault(name, {"sequences": 0, "steps": 0, "flows": {}})
    entry["sequences"] += sequences_count
    entry["steps"] += sequences_count * flows_count
    _merge_flow_leaves(entry["flows"], stats)


def get_session_stats() -> Dict[str, dict]:
    """This process's accumulated fuzz-stats registry. A worker ships it to the
    multiprocess server at session end (``fuzz_test_stats`` queue message)."""
    return _session_stats


def merge_session_stats(dst: Dict[str, dict], src: Dict[str, dict]) -> None:
    """Merge one registry (e.g. a worker's) into ``dst`` in place, summing at the
    leaves. The multiprocess server uses it to aggregate across workers."""
    for name, entry in src.items():
        d = dst.setdefault(name, {"sequences": 0, "steps": 0, "flows": {}})
        d["sequences"] += entry.get("sequences", 0)
        d["steps"] += entry.get("steps", 0)
        _merge_flow_leaves(d["flows"], entry.get("flows", {}))


def clear_last_fuzz_failure() -> None:
    global _last_fuzz_failure
    _last_fuzz_failure = None


def _set_last_fuzz_failure(
    *, fuzz_class: str, sequence_num: int, flow_num: int, failing: str, trace: List[str]
) -> None:
    global _last_fuzz_failure
    _last_fuzz_failure = {
        "fuzz_class": fuzz_class,
        "sequence": sequence_num,
        "flow": flow_num,
        "failing": failing,
        "trace": trace,
    }


def get_last_fuzz_failure() -> Optional[dict]:
    """Context of the most recent FuzzTest failure — the class, the failing
    sequence/flow index, the failing step (e.g. ``"flow increment"`` /
    ``"invariant counter_matches_model"``), and the flow trace — or ``None``.
    The pytest plugin reads this in ``pytest_exception_interact`` to write a
    crash-log JSON. Minimal analogue of wake's ``get_executing_flow_num`` /
    ``get_sequence_initial_internal_state`` seam."""
    return _last_fuzz_failure
