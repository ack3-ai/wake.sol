"""Pytest plugin: per-test SVM reset and deterministic per-test seeding.

Registered as a ``pytest11`` entry point, so it is active for both plain
``pytest`` and the ``solana-fuzzer test`` CLI. Before every test it resets the
global SVM and reseeds the global ``random`` with a seed derived from the base
seed and the test's node id — so any single test reproduces on its own,
regardless of run order or selection.
"""

from __future__ import annotations

import hashlib
import os

import pytest

import solana_fuzzer
from solana_fuzzer import _debug

_SEED_KEY: pytest.StashKey[bytes] = pytest.StashKey()


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


def pytest_configure(config: pytest.Config) -> None:
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
    solana_fuzzer._labels.set_full_addresses(config.option.verbose > 0)

    # Zero-config auto-import of a top-level generated `pytypes/` package (§9.9):
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
    base_seed = item.config.stash[_SEED_KEY]
    svm = solana_fuzzer.svm
    # Restore default config, then wipe all accounts — a pristine SVM per test.
    if not svm.sigverify:
        svm.sigverify = True
    if not svm.blockhash_check:
        svm.blockhash_check = True
    if not svm.transaction_history:
        svm.transaction_history = True  # reset() keeps it; restore the default
    svm.unfork()   # forking is config that reset() keeps, so drop it explicitly
    svm.reset()    # wipes accounts + sysvars, reverts feature deltas to construction
    solana_fuzzer._labels.clear_labels()  # drop per-test account labels
    per_test = hashlib.sha256(base_seed + item.nodeid.encode()).digest()
    solana_fuzzer.random.seed(per_test)


def pytest_exception_interact(node, call, report) -> None:
    """Drop into the ipdb post-mortem on a test exception when ``--attach`` is set.

    Single-process path: call :func:`_debug.attach_debugger` directly. When the
    multiprocess runner lands, it will instead route through
    :func:`_debug.get_exception_handler` so the worker that raised hands the
    breakpoint to the process owning the terminal.
    """
    config = node.config
    if not config.getoption("--attach") or call.excinfo is None:
        return

    base_seed = config.stash[_SEED_KEY]

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
    terminalreporter.section("solana-fuzzer")
    terminalreporter.write_line(f"Base seed: {base_seed.hex()}")
    terminalreporter.write_line(f"Reproduce: pytest --seed {base_seed.hex()}")
