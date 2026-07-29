"""Pytest plugin: per-test SVM reset and deterministic per-test seeding.

Registered as a ``pytest11`` entry point, so it is active for both plain
``pytest`` and the ``wake-sol test`` CLI. Before every test it resets the
global SVM and reseeds the global ``random`` with a seed derived from the base
seed and the test's node id — so any single test reproduces on its own,
regardless of run order or selection.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import pytest

import wake_sol
from wake_sol import _debug, fuzzing

_SEED_KEY: pytest.StashKey[bytes] = pytest.StashKey()

# Crash-log seams (multiprocess runner). The always-on entry-point plugin writes
# the crash-log JSONs; the worker plugin points them at its per-worker dir and
# forwards written paths to the server. Single-process leaves both unset.
_crash_dir_override: Optional[Path] = None
_crash_sink: Optional[Callable[[str, str], None]] = None
#: ``(nodeid, relpath)`` for every crash log written this session; listed in the
#: (single-process) terminal summary.
_crash_logs: List[Tuple[str, str]] = []

#: One entry per FuzzTest failure this session: the class, the failing step and
#: the sequence/flow index. Captured in `_write_fuzz_crash_log` (which consumes
#: the fuzzing module's single-slot failure context) so the terminal summary can
#: report every failure, not just the last one.
_fuzz_failures: List[dict] = []


def set_crash_dir(path: Optional[Path]) -> None:
    """Point crash-log JSON writing at ``path`` instead of the default
    ``.wake-sol/logs/crashes``. The multiprocess worker sets this to its
    per-worker ``crashes/process-<N>`` dir so filenames can't collide across
    workers; single-process leaves it unset."""
    global _crash_dir_override
    _crash_dir_override = Path(path) if path is not None else None


def set_crash_log_sink(sink: Optional[Callable[[str, str], None]]) -> None:
    """Install a callback invoked with ``(nodeid, relpath)`` each time a crash
    log is written. The multiprocess worker uses it to forward the path to the
    server for the aggregated summary; single-process leaves it unset."""
    global _crash_sink
    _crash_sink = sink


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--seed",
        action="store",
        default=None,
        help="Base random seed (hex). A random one is used if omitted.",
    )
    # NB: pytest reserves `--debug`, so the attach flag is spelled `--attach`.
    parser.addoption(
        "--attach",
        action="store_true",
        default=False,
        help="On test failure, drop into an ipdb post-mortem at your frame.",
    )


_ROOTPATH_ADDED = "_wake_sol_rootpath_added"


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    """Put the project root on ``sys.path`` so a generated ``pytypes/`` package
    imports under a bare ``pytest``.

    ``python -m pytest`` adds the cwd for you; a bare ``pytest`` does not, and
    pytest's own ``prepend`` import mode only inserts the *test file's* directory
    (``tests/``), not the root — so a root-level ``pytypes/`` is unimportable and
    the auto-import in :func:`pytest_configure` finds nothing. Rather than making
    every consumer write ``pythonpath = ["."]`` in their own config, do it here.

    Runs ``tryfirst`` on this hook so the path is in place *before* initial
    conftests are imported — a root ``conftest.py`` that imports ``pytypes`` has
    to work too. Equivalent to pytest's ``pythonpath`` ini (which resolves
    relative paths against the rootdir), and skipped when something already put
    the root on the path.
    """
    root = str(early_config.rootpath)
    if root in sys.path:
        return
    sys.path.insert(0, root)
    setattr(early_config, _ROOTPATH_ADDED, True)

    def _remove() -> None:
        if root in sys.path:
            sys.path.remove(root)

    early_config.add_cleanup(_remove)


def _default_breakpoints_to_ipdb(config: pytest.Config) -> None:
    """Make a manual ``breakpoint()`` land in ipdb rather than stock ``pdb``.

    In-process we deliberately do not claim ``PYTHONBREAKPOINT`` (it fights
    pytest's capture — see :mod:`wake_sol._debug`), so ``breakpoint()``
    would otherwise drop you at a bare ``(Pdb)`` prompt while everything else in
    this harness — ``--attach``, the multiprocess runner — hands you ipdb.

    Routing it through pytest's own ``--pdbcls`` keeps pytest in charge of
    suspending capture, so this works without ``-s``; pointing
    ``PYTHONBREAKPOINT`` at ipdb instead raises "reading from stdin while output
    is captured" unless the user remembers ``-s``.

    An explicit ``--pdbcls`` always wins, so ``--pdbcls=pdb:Pdb`` restores stock
    pdb. IPython arrives with the ``ipdb`` runtime dependency, but degrade
    silently if it is somehow absent.
    """
    if config.getoption("usepdb_cls", None):
        return  # the user asked for a specific debugger
    try:
        import IPython.terminal.debugger  # noqa: F401
    except Exception:
        return
    config.option.usepdb_cls = ("IPython.terminal.debugger", "TerminalPdb")


def pytest_configure(config: pytest.Config) -> None:
    _default_breakpoints_to_ipdb(config)

    raw = config.getoption("--seed")
    if raw is None:
        base_seed = os.urandom(8)
    else:
        try:
            base_seed = bytes.fromhex(str(raw))
        except ValueError:
            raise pytest.UsageError("--seed must be a hex string")
    config.stash[_SEED_KEY] = base_seed

    # Prettier tracebacks for errors that escape pytest's own capture. Best
    # effort — skip silently if rich's traceback handler isn't available.
    try:
        from rich.traceback import install as _install_rich_traceback

        _install_rich_traceback(show_locals=config.option.verbose > 1)
    except Exception:
        pass

    # Verbose (`-v`) renders unlabeled addresses in full, no `3Ftw…HBaY` ellipsis.
    wake_sol._labels.set_full_addresses(config.option.verbose > 0)

    # Zero-config auto-import of a top-level generated `pytypes/` package:
    # importing it registers every generated program (import side effect). A
    # missing `pytypes` is the legal "no generated package" case; a broken
    # generated submodule is a loud error and never swallowed.
    try:
        import pytypes  # noqa: F401
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] != "pytypes":
            raise


def pytest_runtest_setup(item: pytest.Item) -> None:
    # Re-arm the one-shot post-mortem guard for this test (see _debug).
    _debug.reset_exception_handled()
    # Drop any prior fuzz failure context so a non-fuzz test that fails later
    # never inherits a previous fuzz test's context and writes a spurious log.
    fuzzing.clear_last_fuzz_failure()
    base_seed = item.config.stash[_SEED_KEY]
    svm = wake_sol.svm
    # Restore default config, then wipe all accounts — a pristine SVM per test.
    if not svm.sigverify:
        svm.sigverify = True
    if not svm.blockhash_check:
        svm.blockhash_check = True
    if not svm.transaction_history:
        svm.transaction_history = True  # reset() keeps it; restore the default
    svm.unfork()   # forking is config that reset() keeps, so drop it explicitly
    svm.reset()    # wipes accounts + sysvars, reverts feature deltas to construction
    wake_sol._labels.clear_labels()  # drop per-test account labels
    per_test = hashlib.sha256(base_seed + item.nodeid.encode()).digest()
    wake_sol.random.seed(per_test)


def pytest_exception_interact(node, call, report) -> None:
    """Route a test exception to the ipdb post-mortem.

    Multiprocess path: if a worker has installed an exception handler through
    the :mod:`_debug` seam (:func:`_debug.set_exception_handler`), hand the
    exception to it — it negotiates an attach with the process that owns the
    terminal and, on accept, drives ipdb *in the worker* where the live objects
    are. This is consulted **first and unconditionally**: workers are not given
    the ``--attach`` pytest option (the CLI passes the flag to the worker plugin
    as a constructor boolean instead), so gating on it would never fire.

    Single-process path (no handler installed): call
    :func:`_debug.attach_debugger` directly when ``--attach`` is set.

    A FuzzTest failure also leaves a JSON crash log — written first and
    unconditionally, before any attach routing, so it happens regardless of
    ``--attach`` and of whether a worker installed a debugger handler.
    """
    if call.excinfo is None:
        return

    config = node.config
    base_seed = config.stash[_SEED_KEY]

    _write_fuzz_crash_log(node, call, base_seed)

    handler = _debug.get_exception_handler()
    if handler is not None:
        handler(call.excinfo.type, call.excinfo.value, call.excinfo.tb)
        return

    if not config.getoption("--attach"):
        return

    # ipdb needs to own stdin/stdout; suspend pytest's capture around it.
    capman = config.pluginmanager.getplugin("capturemanager")
    if capman is not None:
        capman.suspend_global_capture(in_=True)
    try:
        from rich.console import Console

        Console().print(f'Reproduce: pytest --seed {base_seed.hex()} "{node.nodeid}"')
        _debug.attach_debugger(
            call.excinfo.type,
            call.excinfo.value,
            call.excinfo.tb,
            seed=base_seed,
        )
    finally:
        if capman is not None:
            capman.resume_global_capture()


def pytest_terminal_summary(terminalreporter, exitstatus, config: pytest.Config) -> None:
    base_seed = config.stash[_SEED_KEY]
    terminalreporter.section("wake.sol")

    # The flow-stats table(s) and the failing step used to be printed from inside
    # `_run`, which put them in pytest's "captured stdout" block — visible only on
    # failure or under `-s`, and mixed in with the program's own output. They are
    # this plugin's diagnostics, so they belong in this section.
    _write_flow_stats(terminalreporter)

    for f in _fuzz_failures:
        terminalreporter.write_line(
            f"Failed: {f['fuzz_class']} in {f['failing']} "
            f"at sequence {f['sequence']}, flow {f['flow']}"
        )

    terminalreporter.write_line(f"Base seed: {base_seed.hex()}")
    # Single-process crash logs land here; under -P the worker's summary goes to
    # its redirected log and the server lists them on the console instead.
    if _crash_logs:
        terminalreporter.write_line("Crash logs:")
        for nodeid, relpath in _crash_logs:
            terminalreporter.write_line(f"  {nodeid}: {relpath}")


def _write_flow_stats(terminalreporter) -> None:
    """Render one flow-stats table per FuzzTest class from the session registry.

    Uses rich only to build the table (the same renderable the multiprocess
    server aggregates), then hands the rendered lines to the terminal reporter so
    pytest owns the output stream. A run with no FuzzTest in it prints nothing.
    """
    session_stats = fuzzing.get_session_stats()
    if not session_stats:
        return
    for entry in session_stats.values():
        table, warnings = fuzzing.build_stats_table(None, entry["flows"])
        for line in _render_lines(table):
            terminalreporter.write_line(line)
        for w in warnings:
            terminalreporter.write_line(f"⚠ {w}")


def _render_lines(renderable) -> List[str]:
    """Rich renderable -> list of plain lines, best effort."""
    try:
        from rich.console import Console

        console = Console(file=io.StringIO(), width=100, no_color=False)
        console.print(renderable)
        return console.file.getvalue().rstrip("\n").split("\n")
    except Exception:
        return []


def _sanitize(text: str, maxlen: int = 80) -> str:
    """A filesystem-safe slug of a node id for a crash-log filename (avoids a
    ``pathvalidate`` dep). Timestamps carry uniqueness; this is for readability."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in text)
    return safe[:maxlen] or "crash"


def _write_fuzz_crash_log(node, call, base_seed: bytes) -> None:
    """Write a JSON crash log for a FuzzTest failure and register it for the
    terminal summary (and, under ``-P``, forward it to the server via the sink).

    A no-op when there is no fuzz failure context — so an ordinary (non-fuzz)
    test failure writes nothing. Best-effort: it swallows only ``OSError`` and
    never masks the real test failure. The reproduce line is embedded verbatim
    for copy-paste; the seed is the session base seed that line uses.
    """
    ctx = fuzzing.get_last_fuzz_failure()
    if ctx is None or call.excinfo is None:
        return
    # Consume it: exactly one crash log per failure, even if the hook fires again.
    fuzzing.clear_last_fuzz_failure()
    # Keep a copy for the terminal summary — the single-slot context above is
    # cleared here, and a session can have more than one fuzz failure.
    _fuzz_failures.append(
        {
            "fuzz_class": ctx["fuzz_class"],
            "failing": ctx["failing"],
            "sequence": ctx["sequence"],
            "flow": ctx["flow"],
        }
    )

    import json
    from datetime import datetime

    crash_dir = _crash_dir_override or (
        Path.cwd() / ".wake-sol" / "logs" / "crashes"
    )
    exc_type = getattr(call.excinfo.type, "__name__", None) or str(call.excinfo.type)
    data = {
        "nodeid": node.nodeid,
        "seed": base_seed.hex(),
        "reproduce": f'pytest --seed {base_seed.hex()} "{node.nodeid}"',
        "fuzz_class": ctx["fuzz_class"],
        "sequence": ctx["sequence"],
        "flow": ctx["flow"],
        "failing": ctx["failing"],
        "trace": ctx["trace"],
        "exception": {"type": exc_type, "value": str(call.excinfo.value)},
    }
    # Microsecond timestamp: two failures in the same worker can't collide, and
    # per-worker dirs keep them apart across workers.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    try:
        crash_dir.mkdir(parents=True, exist_ok=True)
        crash_file = crash_dir / f"{timestamp}_{_sanitize(node.nodeid)}.json"
        crash_file.write_text(json.dumps(data, indent=2))
    except OSError:
        return  # a crash log is a convenience; never let it mask the failure

    relpath = os.path.relpath(crash_file, Path.cwd())
    _crash_logs.append((node.nodeid, relpath))
    if _crash_sink is not None:
        _crash_sink(node.nodeid, relpath)
