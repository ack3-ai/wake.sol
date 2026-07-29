"""Worker-side pytest plugin for the multiprocess test runner.

Each worker is a *full, independent* pytest session forked from the server (see
:mod:`wake_sol._mp_server`). It runs with the auto-loaded entry-point
plugin (:mod:`wake_sol._pytest_plugin`) — so per-test SVM reset and the
``sha256(seed + nodeid)`` seeding keep working unchanged — plus one instance of
this plugin, injected by the server with the worker's index, its end of a
``Pipe``, the shared results ``Queue``, its base seed, and the two debugger
flags (``attach`` / ``tee``).

What this plugin does, in lifecycle order:

* ``pytest_configure`` redirects the worker's stdout+stderr into
  ``.wake-sol/logs/testing/process-<N>.ansi`` (the server wipes that dir at
  session start). Workers run with ``-s``, so capture is off and the redirect
  catches everything. Under ``--attach-first`` worker 0 *tees* to console + log
  (``StdoutTee``/``StderrTee``) instead, so its output stays live on the console.
* ``pytest_collection_finish`` sends this worker's collected node-id list back
  over the pipe; the server asserts every worker collected the identical list.
* ``pytest_runtestloop`` installs ``sys.breakpointhook`` (interactive
  ``breakpoint()`` support), tblib ``pickling_support``, the SIGINT handler, the
  crash-log seams (per-worker crash dir + a sink that forwards written paths) and
  — in ``--attach`` mode — the :mod:`_debug` exception-handler seam, then
  receives the assigned index list over the pipe and drives
  ``pytest_runtest_protocol`` for exactly those items. Unlike wake we do **not**
  reseed before each item: the entry-point plugin already derives a per-test seed
  from ``--seed`` + node id, so run order is irrelevant and every failure's
  reproduce line stays valid. After the item loop (in a ``finally``, without a
  ``return``) it enqueues ``fuzz_test_stats`` — this worker's accumulated
  per-flow registry — for the server to merge into one aggregated table.
* The reporting hooks forward ``pytest_runtest_protocol`` / ``_logreport`` (via
  :mod:`_mp_serial`, which falls back to JSON if a report won't pickle) /
  ``pytest_warning_recorded`` / ``pytest_internalerror`` / ``pytest_crashlog_path``
  / ``pytest_sessionfinish`` to the server over the queue. The server buffers the
  ``TestReport``s and replays them into its own session at the end (that is what
  makes the server's exit status and terminal summary correct).
* SIGINT is caught, enqueued as ``keyboard_interrupt``, and the session exits
  cleanly (``returncode=0``); the server re-raises ``KeyboardInterrupt`` once
  every worker is done.

Interactive debugging. On a test exception (``--attach``) or a
``breakpoint()`` call the worker restores real stdio, ships a pickled traceback
(or source snippet) to the server, and **blocks** on the pipe for the server's
attach decision. On accept it grabs the tty (``sys.stdin = os.fdopen(0)`` — the
fork inherited fd 0) and runs ipdb *here*, where the live pyo3 objects are; on
exit it re-redirects stdio and acks the server. A ``TransactionFailed`` carries
pyo3 objects that will **not** pickle, so the ``Exception(repr(e))`` fallback is
the normal path — the server only renders text, and interactive debugging stays
in the worker. The breakpoint ack is guarded by both a ``finally`` and an
``atexit`` fallback so an abnormal debugger exit can never deadlock the server.

Every queue message is a tuple whose ``[0]`` is the kind and ``[1]`` is this
worker's index — in *every* message, unconditionally (wake omits it on one
message and reads a dict as the index by accident; we don't).
"""

from __future__ import annotations

import atexit
import inspect
import logging
import multiprocessing.connection
import multiprocessing.queues
import os
import pickle
import signal
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import TracebackType
from typing import List, Optional, Type

import pytest
from pytest import Session

from wake_sol._mp_serial import dump_report


class PytestPluginMultiprocessWorker:
    """One worker's end of the runner. Instantiated by the server per worker."""

    def __init__(
        self,
        index: int,
        conn: multiprocessing.connection.Connection,
        queue: "multiprocessing.queues.Queue",
        log_dir: Path,
        crash_dir: Path,
        seed: bytes,
        attach: bool,
        tee: bool,
    ) -> None:
        self._index = index
        self._conn = conn
        self._queue = queue
        self._log_dir = log_dir
        self._log_file = log_dir / f"process-{index}.ansi"
        self._crash_dir = crash_dir
        self._seed = seed
        self._attach = attach
        self._tee = tee

        self._f = None  # type: Optional[object]
        self._ctx_managers: List = []
        self._keyboard_interrupt = False
        # One-shot per-test guard: the negotiation must fire at most once even if
        # the same test raises in several phases (reset in pytest_runtest_setup).
        self._exception_handled = False
        # Breakpoint-ack bookkeeping: the ack must reach the server exactly once,
        # even if the debugger exits abnormally (see _ack_breakpoint / atexit).
        self._breakpoint_ack_pending = False
        self._breakpoint_atexit_armed = False

    # -- stdio redirection ---------------------------------------------------- #

    def _setup_stdio(self) -> None:
        # --attach-first tees worker 0 to console + log; everyone else redirects
        # into their per-worker log. Tee opens the file in append mode, and the
        # redirect reuses the still-open self._f, so re-entering after a debugger
        # session never truncates the log.
        if self._tee:
            from wake_sol._tee import StderrTee, StdoutTee

            self._ctx_managers.append(StdoutTee(self._log_file))
            self._ctx_managers.append(StderrTee(self._log_file))
        else:
            self._ctx_managers.append(redirect_stdout(self._f))
            self._ctx_managers.append(redirect_stderr(self._f))
        for cm in self._ctx_managers:
            cm.__enter__()

    def _cleanup_stdio(self) -> None:
        for cm in self._ctx_managers:
            cm.__exit__(None, None, None)
        self._ctx_managers.clear()

    def pytest_configure(self, config: pytest.Config) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._f = open(self._log_file, "w")
        self._setup_stdio()
        # Route stdlib logging to the (now redirected) stdout so library log
        # lines land in this worker's log file too.
        logging.basicConfig(stream=sys.stdout, force=True)

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        self._cleanup_stdio()
        if self._f is not None:
            self._f.close()

    # -- collection handshake ------------------------------------------------- #

    def pytest_collection_finish(self, session: Session) -> None:
        # Pipe (not queue): the server knows which worker by which pipe, so this
        # message needs no index. The server asserts all workers match.
        self._conn.send(("pytest_collection_finish", [i.nodeid for i in session.items]))

    # -- per-test guard reset ------------------------------------------------- #

    def pytest_runtest_setup(self, item: pytest.Item) -> None:
        # Re-arm this plugin's one-shot negotiation guard. The entry-point plugin
        # resets the *global* _debug guard (used by attach_debugger) in its own
        # pytest_runtest_setup; the two guards are independent and both reset.
        self._exception_handled = False

    # -- interactive debugging (attach negotiation) --------------------------- #

    def _exception_handler(
        self,
        e_type: Optional[Type[BaseException]],
        e: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        """Routing handler installed via ``_debug.set_exception_handler``.

        Called from the entry-point plugin's ``pytest_exception_interact``. Ships
        the traceback to the server and blocks for its attach decision; on accept
        runs the ipdb post-mortem here. The ``finally`` always acks the server.
        """
        from wake_sol import _debug

        # After a keyboard interrupt the failure is interrupt noise; don't debug.
        if self._keyboard_interrupt:
            return
        if self._exception_handled:
            return
        self._exception_handled = True
        self._cleanup_stdio()

        assert e_type is not None
        assert e is not None
        assert tb is not None

        # TransactionFailed carries live pyo3 objects that will not pickle, so the
        # repr fallback is the normal path. tblib (installed in runtestloop) makes
        # the traceback picklable either way; the server only renders text.
        try:
            pickled = pickle.dumps((e_type, e, tb))
        except Exception:
            pickled = pickle.dumps((e_type, Exception(repr(e)), tb))
        self._queue.put(("exception", self._index, pickled), block=True)

        attach: bool = self._conn.recv()
        try:
            if attach:
                # The fork inherited the controlling tty on fd 0.
                sys.stdin = os.fdopen(0)
                _debug.attach_debugger(e_type, e, tb, seed=self._seed)
        finally:
            self._setup_stdio()
            self._conn.send(("exception_handled",))

    def _breakpoint_hook(self, *args, **kwargs) -> None:
        """``sys.breakpointhook``: negotiate an interactive ``breakpoint()``.

        Ships a source snippet of the caller to the server and blocks for the
        attach decision; on accept drops into a :func:`_debug.make_custom_pdb`
        session at the caller's frame. The ack is guaranteed exactly once (the
        non-attach branch sends it inline; the attach branch relies on the pdb
        exit commands *and* an atexit fallback), so the server never deadlocks.
        """
        from wake_sol import _debug

        self._cleanup_stdio()
        caller = inspect.currentframe().f_back
        filename = caller.f_code.co_filename
        lineno = caller.f_lineno
        function_name = caller.f_code.co_name
        snippet = _source_snippet(caller, lineno)
        pickled = pickle.dumps((filename, lineno, function_name, snippet))
        self._queue.put(("breakpoint", self._index, pickled), block=True)

        attach: bool = self._conn.recv()
        if not attach:
            self._setup_stdio()
            self._conn.send(("breakpoint_handled",))
            return

        # Arm the ack: cleared once by whoever sends it first (a pdb exit command
        # via _finish_breakpoint, or this atexit fallback on an abnormal exit).
        self._breakpoint_ack_pending = True
        if not self._breakpoint_atexit_armed:
            self._breakpoint_atexit_armed = True
            atexit.register(self._ack_breakpoint)

        sys.stdin = os.fdopen(0)
        frame = sys._getframe(1)
        p = _debug.make_custom_pdb(self._finish_breakpoint)
        p.set_trace(frame)

    def _ack_breakpoint(self) -> None:
        if self._breakpoint_ack_pending:
            self._breakpoint_ack_pending = False
            try:
                self._conn.send(("breakpoint_handled",))
            except (OSError, ValueError):
                pass  # pipe already closed on abnormal shutdown — nothing to ack

    def _finish_breakpoint(self) -> None:
        # Passed to CustomPdb; runs when the user continues/quits/EOFs the pdb.
        self._setup_stdio()
        self._ack_breakpoint()

    # -- run loop ------------------------------------------------------------- #

    def pytest_runtestloop(self, session: Session) -> Optional[bool]:
        if (
            session.testsfailed
            and not session.config.option.continue_on_collection_errors
        ):
            raise session.Interrupted(
                "%d error%s during collection"
                % (session.testsfailed, "s" if session.testsfailed != 1 else "")
            )

        if session.config.option.collectonly:
            return True

        # Route breakpoint() through the server negotiation (always installed;
        # the server auto-declines when it can't prompt, e.g. CI / --attach-first).
        sys.breakpointhook = self._breakpoint_hook

        # tblib lets tracebacks survive pickling — needed for both the attach
        # negotiation and the pytest_internalerror forward below.
        from tblib import pickling_support

        pickling_support.install()

        def sigint_handler(signum, frame):
            self._keyboard_interrupt = True
            self._queue.put(("keyboard_interrupt", self._index))
            # Exit this session cleanly; the server re-raises KeyboardInterrupt
            # after every worker is done.
            pytest.exit("Keyboard interrupt", returncode=0)

        signal.signal(signal.SIGINT, sigint_handler)

        # --attach: install the routing handler the entry-point plugin consults.
        if self._attach:
            from wake_sol import _debug

            _debug.set_exception_handler(self._exception_handler)

        # Point crash-log writing at this worker's dir and forward written paths
        # to the server. The entry-point plugin's pytest_exception_interact does
        # the writing; these seams are unconditional (not gated on --attach).
        from wake_sol import _pytest_plugin

        _pytest_plugin.set_crash_dir(self._crash_dir)
        _pytest_plugin.set_crash_log_sink(self._crashlog_sink)

        indexes = self._conn.recv()
        try:
            for i in range(len(indexes)):
                item = session.items[indexes[i]]
                nextitem = (
                    session.items[indexes[i + 1]] if i + 1 < len(indexes) else None
                )
                # We intentionally do NOT reseed here (see module docstring): the
                # entry-point plugin reseeds per test from --seed + node id.
                item.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
                if session.shouldfail:
                    raise session.Failed(session.shouldfail)
                if session.shouldstop:
                    raise session.Interrupted(session.shouldstop)
        finally:
            # Ship this worker's accumulated fuzz-stats registry for the server to
            # merge and render as one aggregated table. In a finally so an -x /
            # shouldstop early-exit still contributes its partial stats — but with
            # NO `return` here: a return in finally would swallow the
            # session.Failed / session.Interrupted that must propagate so the
            # worker exits nonzero (wake quirk #3).
            from wake_sol import fuzzing

            self._queue.put(("fuzz_test_stats", self._index, fuzzing.get_session_stats()))

        return True

    # -- forwarded reporting hooks ------------------------------------------- #

    def pytest_runtest_protocol(self, item, nextitem):
        # Announce the current test, then return None so the default protocol
        # implementation actually runs it.
        self._queue.put(("pytest_runtest_protocol", self._index, item.nodeid))

    # pytest_runtest_logstart / logfinish are deliberately not forwarded: they
    # write per-item file locations that differ across workers.

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        # After a keyboard interrupt the in-flight report is interrupt noise, not
        # a real result — dropping it keeps the aggregated summary honest.
        if self._keyboard_interrupt:
            return
        # Serialize explicitly so an unpicklable longrepr is caught here and falls
        # back to the JSON form, instead of failing in the queue's feeder thread.
        kind, payload = dump_report(report)
        self._queue.put((kind, self._index, payload))

    def _crashlog_sink(self, nodeid: str, relpath: str) -> None:
        # Installed via _pytest_plugin.set_crash_log_sink; fires when the
        # entry-point plugin writes a crash-log JSON. The server lists these
        # under "Crash logs:" in its terminal summary.
        self._queue.put(("pytest_crashlog_path", self._index, nodeid, relpath))

    def pytest_warning_recorded(self, warning_message, when, nodeid, location) -> None:
        self._queue.put(
            (
                "pytest_warning_recorded",
                self._index,
                warning_message,
                when,
                nodeid,
                location,
            )
        )

    def pytest_internalerror(self, excrepr, excinfo: pytest.ExceptionInfo) -> None:
        # Send the raw (type, value, tb) triple so the server can rebuild it with
        # ExceptionInfo.from_exc_info; tblib (installed above) makes tb picklable.
        try:
            pickled = pickle.dumps((excinfo.type, excinfo.value, excinfo.tb))
        except Exception:
            # Some exceptions carry unpicklable state (e.g. a Rust-raised
            # TransactionFailed with live pyo3 objects). The server only renders
            # text, so a repr-only stand-in is enough.
            pickled = pickle.dumps(
                (excinfo.type, Exception(repr(excinfo.value)), excinfo.tb)
            )
        self._queue.put(("pytest_internalerror", self._index, pickled))

    def pytest_sessionfinish(self, session: Session, exitstatus: int) -> None:
        self._queue.put(("pytest_sessionfinish", self._index, exitstatus))


def _source_snippet(frame, lineno: int) -> str:
    """A ~10-line source window around ``lineno`` with a ``-->`` marker.

    Ported from wake's ``custom_debugger``. Best effort: if the source can't be
    read (``inspect.getsourcelines`` raises, e.g. an interactive/`<string>`
    frame) fall back to a one-line locator so the server still has something to
    show.
    """
    try:
        source_lines, starting_line_no = inspect.getsourcelines(frame)
    except (OSError, TypeError):
        return f"{frame.f_code.co_filename}:{lineno}\n"

    lines_to_show = 10
    relative_lineno = lineno - starting_line_no
    start_line = max(0, relative_lineno - lines_to_show // 2)
    end_line = min(len(source_lines), relative_lineno + lines_to_show // 2)
    subset = source_lines[start_line:end_line]

    width = len(str(starting_line_no + end_line))
    for idx, line in enumerate(subset):
        abs_no = starting_line_no + start_line + idx
        marker = "-->" if start_line + idx == relative_lineno else "   "
        subset[idx] = f"{marker} {abs_no:>{width}} {line}"
    return "".join(subset)
