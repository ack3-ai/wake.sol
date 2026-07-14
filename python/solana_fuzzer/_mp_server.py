"""Server-side pytest plugin for the multiprocess test runner.

``solana-fuzzer test -P N`` runs **one server pytest session + N worker
sessions**, all in one process group. The CLI starts the server with
``pytest.main(server_args, plugins=[PytestPluginMultiprocessServer(...)])`` and
``-p no:solana_fuzzer`` (so the entry-point plugin does not register ``--seed``
or print a meaningless "Base seed" summary in the server). The server *collects*
tests but never runs them — :meth:`pytest_runtestloop` returns ``True`` after
draining the workers' event queue.

Topology (all explicitly on the ``fork`` start method — see design doc §3
blocker 5; Python 3.14 defaults Linux to ``forkserver``, which would break the
live-object handoff of the ``Connection``/``Queue``/plugin instance):

* one shared ``Queue`` (workers → server: all event traffic);
* one ``Pipe`` per worker (server ↔ worker: collection handshake + index-list
  assignment; interactive-attach negotiation is Phase 2).

Lifecycle:

1. :meth:`pytest_sessionstart` wipes ``.solana-fuzzer/logs/testing``, then forks
   N workers, each a full ``pytest.main`` with the auto-loaded entry-point
   plugin plus one :class:`~solana_fuzzer._mp_worker.PytestPluginMultiprocessWorker`.
   Afterwards the server sets ``SIGINT`` to ``SIG_IGN`` — Ctrl+C reaches the
   workers directly (they catch it and exit cleanly).
2. :meth:`pytest_runtestloop` receives each worker's collected node-id list over
   its pipe and asserts they are **identical** (a guard against nondeterministic
   collection), assigns work as index lists (``duplicated`` = every worker runs
   every test — the N-seeds fuzzing use case, default; ``uniform`` = contiguous
   slices for wall-clock sharding), then runs an event loop over the queue,
   rendering one progress row per worker. ``TestReport``s are **buffered** and
   replayed into this session at the very end so the terminal reporter
   aggregates once and ``Session.testsfailed`` reflects worker failures (that is
   what makes the server's exit status correct).
3. :meth:`pytest_sessionfinish` SIGINTs any still-alive workers, joins them, and
   closes the pipes/queue.
4. :meth:`pytest_terminal_summary` prints one aggregated fuzz-stats table per
   FuzzTest class (registries merged additively from every worker's
   ``fuzz_test_stats`` message), the per-worker seeds (and their reproduce
   lines), and the crash-log paths workers reported. Worker seeds are the
   server's, not the entry-point plugin's — the server runs with
   ``-p no:solana_fuzzer``.

Interactive attach (Phase 2). With ``--attach`` a worker that raises (or hits a
``breakpoint()``) ships a pickled traceback / source snippet and blocks; the
server pauses the progress UI, renders it, and — **only when its own stdin is a
tty** — prompts ``attach? [y/n]`` on the real terminal, sends the bool back over
that worker's pipe, and blocks for the worker's ``*_handled`` ack before
resuming. When stdin is not a tty (CI, a pipe) or under ``--attach-first`` (no
progress UI at all), it auto-declines without prompting, so a parallel run never
wedges waiting for input that will not come. The debugger itself runs *in the
worker*, which holds the live pyo3 objects.

Every queue message is a tuple whose ``[0]`` is the kind and ``[1]`` is the
worker index (unconditionally — see the worker module). ``TestReport``s arrive
as a tagged pair (``pytest_runtest_logreport`` pickle bytes, or a
``pytest_runtest_logreport_json`` fallback — see :mod:`_mp_serial`);
``fuzz_test_stats`` and ``pytest_crashlog_path`` carry the Phase-3 reporting
payloads. Unknown kinds are ignored, so the vocabulary stays open.
"""

from __future__ import annotations

import multiprocessing
import os
import pickle
import shutil
import signal
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

import pytest

from solana_fuzzer._mp_serial import REPORT_JSON, REPORT_PICKLE, load_report


class PytestPluginMultiprocessServer:
    def __init__(
        self,
        proc_count: int,
        seeds: List[bytes],
        dist: str,
        worker_args: List[str],
        logs_dir: Path,
        attach: bool = False,
        attach_first: bool = False,
    ) -> None:
        self._proc_count = proc_count
        self._seeds = seeds
        self._dist = dist
        self._worker_args = worker_args
        self._logs_dir = logs_dir
        self._attach = attach
        self._attach_first = attach_first
        # index -> (Process, parent end of its Pipe)
        self._processes: Dict[int, tuple] = {}
        # Fuzz-stats registries merged across workers (see solana_fuzzer.fuzzing)
        # and (index, nodeid, relpath) for every crash log a worker reported.
        self._fuzz_stats: Dict[str, dict] = {}
        self._crash_logs: List[Tuple[int, str, str]] = []
        from rich.console import Console

        self._console = Console()

    # -- fork / teardown ------------------------------------------------------ #

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        shutil.rmtree(self._logs_dir, ignore_errors=True)
        self._logs_dir.mkdir(parents=True, exist_ok=True)

        # Explicit fork context: the worker plugin instance, its Connection, and
        # the Queue are live objects passed to the child — they are inherited by
        # fork, never pickled. (design doc §3 blocker 5)
        from solana_fuzzer._mp_worker import PytestPluginMultiprocessWorker

        # Per-worker crash-log dirs, siblings of the testing logs. Not wiped — a
        # crash log is a durable artifact, and the server only lists the ones a
        # worker reports this run (it never scans the dir), so nothing goes stale.
        crashes_root = self._logs_dir.parent / "crashes"

        ctx = multiprocessing.get_context("fork")
        self._queue = ctx.Queue(1000)

        for i in range(self._proc_count):
            parent_conn, child_conn = ctx.Pipe()
            crash_dir = crashes_root / f"process-{i}"
            crash_dir.mkdir(parents=True, exist_ok=True)
            # Each worker keeps the auto-loaded entry-point plugin (per-test
            # reset + seeding) and gets its own --seed; only the injected worker
            # plugin differs between workers. The debugger flags are passed as
            # constructor booleans (wake-style), never as pytest args — workers
            # do not receive the entry-point --attach option.
            worker_args = self._worker_args + ["--seed", self._seeds[i].hex()]
            p = ctx.Process(
                target=pytest.main,
                args=(worker_args,),
                kwargs={
                    "plugins": [
                        PytestPluginMultiprocessWorker(
                            i,
                            child_conn,
                            self._queue,
                            self._logs_dir,
                            crash_dir,
                            self._seeds[i],
                            self._attach,
                            # --attach-first tees only worker 0 to the console.
                            self._attach_first and i == 0,
                        )
                    ]
                },
            )
            self._processes[i] = (p, parent_conn)
            p.start()

        # Ctrl+C goes to the whole foreground process group; let the workers
        # handle it and exit cleanly, and ignore it here.
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        self._queue.cancel_join_thread()
        for p, conn in self._processes.values():
            if p.pid is not None:
                try:
                    os.kill(p.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass  # already exited
            p.join()
            conn.close()
        self._queue.close()

    # -- suppress our own teststatus ----------------------------------------- #

    def pytest_report_teststatus(self, report, config):
        # Abstain (return None): this firstresult hook then falls through to the
        # default terminal reporter, which computes the real category/letter for
        # both the live progress rows and the end-of-run replay. The server
        # never adds a status of its own.
        return None

    # -- event loop ----------------------------------------------------------- #

    def pytest_runtestloop(self, session: pytest.Session) -> bool:
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

        # Collection handshake: every worker must have collected the identical
        # node-id list, else distribution-by-index is meaningless.
        collected: List[List[str]] = []
        for i in range(self._proc_count):
            cmd, data = self._processes[i][1].recv()
            assert cmd == "pytest_collection_finish"
            collected.append(data)

        for i in range(1, self._proc_count):
            if collected[0] != collected[i]:
                # pytest swallows the Failed message, so print the details first
                # (workers block on recv() until pytest_sessionfinish SIGINTs
                # them, so this does not hang).
                msg = (
                    "workers collected different tests — collection is "
                    "nondeterministic:\n"
                    f"  #0: {collected[0]}\n  #{i}: {collected[i]}"
                )
                self._console.print(f"[red]{msg}")
                raise session.Failed(msg)

        # Assign work as index lists.
        n = len(collected[0])
        for i in range(self._proc_count):
            if self._dist == "uniform":
                step = n // self._proc_count
                if i == self._proc_count - 1:
                    self._processes[i][1].send(list(range(i * step, n)))
                else:
                    self._processes[i][1].send(list(range(i * step, (i + 1) * step)))
            elif self._dist == "duplicated":
                self._processes[i][1].send(list(range(n)))
            else:  # pragma: no cover - guarded by the CLI's click.Choice
                raise session.Failed(f"unknown distribution: {self._dist}")

        current_tests: Dict[int, Optional[str]] = {
            i: None for i in range(self._proc_count)
        }
        test_reports: Dict[int, Dict[str, str]] = {
            i: {} for i in range(self._proc_count)
        }
        reports: List[pytest.TestReport] = []
        keyboard_interrupt = [False for _ in range(self._proc_count)]

        # Live progress needs a real terminal; degrade to plain per-worker lines
        # otherwise (CI, pipes) so output stays sane and non-interactive. Under
        # --attach-first there is no progress UI at all — worker 0's tee'd output
        # owns the console (wake uses nullcontext), and the server auto-declines
        # every attach because `progress is None`.
        if self._console.is_terminal and not self._attach_first:
            import rich.progress

            ctx = rich.progress.Progress(
                rich.progress.SpinnerColumn(finished_text="[green]done"),
                "[progress.description]{task.description}",
                console=self._console,
            )
        else:
            ctx = nullcontext()

        try:
            with ctx as progress:
                if progress is not None:
                    tasks = [
                        progress.add_task(f"#{i} starting", total=None)
                        for i in range(self._proc_count)
                    ]
                else:
                    tasks = []

                while self._processes:
                    msg = self._queue.get()
                    kind = msg[0]
                    index = msg[1]

                    if kind == "pytest_runtest_protocol":
                        current_tests[index] = msg[2]
                        if progress is not None:
                            self._update_progress(
                                progress, index, tasks[index],
                                current_tests[index], test_reports[index],
                            )
                    elif kind in (REPORT_PICKLE, REPORT_JSON):
                        # Reconstruct from the worker's explicit serialization
                        # (pickle, or JSON fallback for an unpicklable longrepr).
                        report = load_report(kind, msg[2])
                        reports.append(report)
                        self._process_teststatus(index, session, report, test_reports)
                        if progress is not None:
                            self._update_progress(
                                progress, index, tasks[index],
                                current_tests[index], test_reports[index],
                            )
                    elif kind == "exception":
                        e_type, e, tb = pickle.loads(msg[2])

                        def render_exception(e_type=e_type, e=e, tb=tb, index=index):
                            import rich.traceback

                            self._console.print(
                                rich.traceback.Traceback.from_exception(e_type, e, tb)
                            )
                            self._console.print(
                                f"Worker #{index} raised the exception above."
                            )

                        self._negotiate(index, progress, render_exception)
                    elif kind == "breakpoint":
                        filename, lineno, function_name, snippet = pickle.loads(msg[2])

                        def render_breakpoint(
                            snippet=snippet,
                            function_name=function_name,
                            filename=filename,
                            lineno=lineno,
                            index=index,
                        ):
                            self._console.print(snippet)
                            self._console.print(
                                f"Worker #{index} hit a breakpoint in "
                                f"{function_name} at {filename}:{lineno}."
                            )

                        self._negotiate(index, progress, render_breakpoint)
                    elif kind == "pytest_warning_recorded":
                        session.config.hook.pytest_warning_recorded.call_historic(
                            kwargs={
                                "warning_message": msg[2],
                                "when": msg[3],
                                "nodeid": msg[4],
                                "location": msg[5],
                            },
                        )
                    elif kind == "pytest_internalerror":
                        exc_info = pytest.ExceptionInfo.from_exc_info(
                            pickle.loads(msg[2])
                        )
                        self._console.print(
                            f"Worker #{index} failed with an internal error:"
                        )
                        session.config.hook.pytest_internalerror(
                            excrepr=exc_info.getrepr(style="short"), excinfo=exc_info
                        )
                    elif kind == "fuzz_test_stats":
                        # Merge this worker's per-flow registry into the aggregate
                        # (additive at the leaves); rendered in the summary.
                        from solana_fuzzer import fuzzing

                        fuzzing.merge_session_stats(self._fuzz_stats, msg[2])
                    elif kind == "pytest_crashlog_path":
                        self._crash_logs.append((index, msg[2], msg[3]))
                    elif kind == "keyboard_interrupt":
                        keyboard_interrupt[index] = True
                    elif kind == "pytest_sessionfinish":
                        if progress is not None:
                            if keyboard_interrupt[index]:
                                desc = f"#{index} interrupted [yellow]!"
                            elif msg[2] == 0:
                                desc = f"#{index} finished [green]OK"
                            else:
                                desc = f"#{index} failed [red]X"
                            progress.update(tasks[index], description=desc, completed=1)
                        else:
                            self._plain_status(index, keyboard_interrupt[index], msg[2])
                        self._processes.pop(index)
                    # Unknown kinds are ignored: the protocol is left open for
                    # later message types (attach, fuzz-stats) without a reshape.

            if True in keyboard_interrupt:
                raise KeyboardInterrupt
        finally:
            # Replay every buffered report into this session so the terminal
            # reporter aggregates once and Session.testsfailed is correct. This
            # runs even when KeyboardInterrupt propagates out of the try.
            print("")
            for report in reports:
                session.config.hook.pytest_runtest_logreport(report=report)

        return True

    def pytest_terminal_summary(self, terminalreporter, exitstatus, config) -> None:
        self._print_fuzz_stats()
        terminalreporter.section("solana-fuzzer")
        terminalreporter.write_line(
            f"Ran {self._proc_count} workers, dist={self._dist}. Per-worker seeds:"
        )
        for i in range(self._proc_count):
            hex_seed = self._seeds[i].hex()
            terminalreporter.write_line(
                f"  #{i}: {hex_seed}   (reproduce: pytest --seed {hex_seed})"
            )
        if self._crash_logs:
            terminalreporter.write_line("Crash logs:")
            for index, nodeid, relpath in self._crash_logs:
                terminalreporter.write_line(f"  #{index} {nodeid}: {relpath}")

    def _print_fuzz_stats(self) -> None:
        """Render one aggregated flow-stats table per FuzzTest class, merged
        across all workers. Prints nothing when no worker reported fuzz stats.

        Uses ``solana_fuzzer.print`` — the same markup-disabled console as the
        single-process per-run table — so the aggregate renders identically and a
        skip reason containing brackets can't be mis-parsed as rich markup.
        """
        if not self._fuzz_stats:
            return
        import solana_fuzzer as sf
        from solana_fuzzer import fuzzing

        for name in sorted(self._fuzz_stats):
            entry = self._fuzz_stats[name]
            title = (
                f"{name} — aggregated flow stats across {self._proc_count} workers "
                f"({entry['sequences']} sequences, {entry['steps']} flow-steps)"
            )
            table, warnings = fuzzing.build_stats_table(title, entry["flows"])
            sf.print(table)
            for w in warnings:
                sf.print(f"[fuzz] ⚠ {w}")

    # -- interactive attach negotiation --------------------------------------- #

    def _negotiate(self, index: int, progress, render: Callable[[], None]) -> None:
        """Decide attach for worker ``index``, send it, wait for the ack.

        Prompt **only** when there is a live progress UI (so not under
        ``--attach-first`` / non-tty stdout) *and* our own stdin is a tty — the
        stricter of wake's gates, so CI never blocks on input. Otherwise
        auto-decline silently. Either way the bool is sent and the worker's
        ``*_handled`` ack is awaited, so the blocked worker is always released.
        """
        prompt = progress is not None and sys.stdin.isatty()
        if prompt:
            progress.stop()
            render()
            attach = self._prompt_attach()
        else:
            attach = False

        conn = self._processes[index][1]
        conn.send(attach)
        try:
            conn.recv()  # the worker's ("<kind>_handled",) ack — released now
        except EOFError:
            pass  # worker vanished mid-negotiation — don't wedge the loop

        if prompt:
            progress.start()

    def _prompt_attach(self) -> bool:
        attach: Optional[bool] = None
        while attach is None:
            try:
                response = input("Attach the debugger? [y/n] ").strip().lower()
            except EOFError:
                return False
            if response == "y":
                attach = True
            elif response == "n":
                attach = False
        return attach

    # -- helpers -------------------------------------------------------------- #

    def _plain_status(self, index: int, interrupted: bool, exitstatus: int) -> None:
        if interrupted:
            self._console.print(f"#{index} interrupted")
        elif exitstatus == 0:
            self._console.print(f"#{index} finished")
        else:
            self._console.print(f"#{index} failed")

    def _update_progress(self, progress, index, task_id, current_test, letters) -> None:
        running = (
            f"running {current_test}" if current_test is not None else "starting"
        )
        progress.update(
            task_id, description=f"#{index} {running} {''.join(letters.values())}"
        )

    def _process_teststatus(
        self,
        index: int,
        session: pytest.Session,
        report: pytest.TestReport,
        test_reports: Dict[int, Dict[str, str]],
    ) -> None:
        category, letter, word = session.config.hook.pytest_report_teststatus(
            report=report, config=session.config
        )
        if not isinstance(word, tuple):
            markup = None
        else:
            word, markup = word

        if not letter and not word:
            return

        if markup is None:
            was_xfail = hasattr(report, "wasxfail")
            if report.passed and not was_xfail:
                markup = {"green": True}
            elif report.passed and was_xfail:
                markup = {"yellow": True}
            elif report.failed:
                markup = {"red": True}
            elif report.skipped:
                markup = {"yellow": True}
            else:
                markup = {}

        msg_start = "".join(f"[{m}]" for m in markup)
        msg_end = "".join(f"[/{m}]" for m in reversed(list(markup)))
        token = letter if session.config.option.verbose <= 0 else word
        test_reports[index][report.nodeid] = f"{msg_start}{token}{msg_end}"
