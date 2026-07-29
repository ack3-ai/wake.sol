"""Interactive ipdb post-mortem for failing tests.

Ported ~1:1 from wake's testing-framework debugger — the pieces that carry over
are:

* :func:`default_handler` / :func:`breakpoint_handler` — verbatim from
  ``wake/utils/pdb_handler.py``: a REPL that ``eval``/``exec``s input in the
  current frame with ``rich`` pretty-printing.
* :func:`attach_debugger` — adapted from ``wake/development/globals.py``: on a
  failing test it drops the user into ipdb positioned at *their* frame, with the
  raised exception bound to ``__exception__`` in every frame.

Adaptations for this harness (vanilla pytest, single process, ``pytypes/``
codegen, Rust-raised :class:`~wake_sol._errors.TransactionFailed`):

* The frame the debugger lands on skips this package's own frames and the
  generated ``pytypes/`` bindings, not just ``pytypes/`` — because the failure
  is raised from Rust, the innermost Python frame is the user's ``send_*`` call
  site (or a generated wrapper), never a framework frame the user cares about.
* Reproduction is surfaced by the pytest plugin as ``pytest --seed <base>
  "<nodeid>"`` (this harness derives the per-test seed; there is no mutable
  global seed to inject as a pdb command the way wake does).
* The debugger class is named explicitly instead of taken from ipdb's
  ``shell.debugger_cls`` — see the note above :func:`_init_pdb`. Under pytest,
  that dispatch answers with a completion-less, history-less ``Pdb``.

Multiprocess note: the multiprocess runner (``wake-sol test -P N``, see
:mod:`wake_sol._mp_worker`) wires the debugger across processes through the
seam here. The :func:`set_exception_handler` / :func:`get_exception_handler` /
:func:`reset_exception_handled` seam mirrors wake's globals so a worker can
install a routing handler that hands the breakpoint to whichever process owns
the terminal, without touching call sites. :func:`breakpoint_handler` stays
unwired in-process (it fights pytest's capture); the runner instead drives
``breakpoint()`` via ``sys.breakpointhook`` + :func:`make_custom_pdb`. In-process
we deliberately do *not* claim ``PYTHONBREAKPOINT``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import TracebackType
from typing import Callable, Optional, Type

from rich import print as rich_print

# The directory of this installed package. Frames inside it are framework
# internals (the pyo3 shim, `_errors`, `must_fail`/`may_fail`, this plugin)
# and are skipped when positioning the debugger.
_PKG_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Exception-handler seam (mirrors wake/development/globals.py).
#
# Single-process pytest calls attach_debugger directly. The seam exists so the
# planned multiprocess runner can register a handler that marshals the
# breakpoint to whichever worker owns the terminal, without changing call sites.
# --------------------------------------------------------------------------- #

_ExceptionHandler = Callable[
    [
        Optional[Type[BaseException]],
        Optional[BaseException],
        Optional[TracebackType],
    ],
    None,
]

_exception_handler: Optional[_ExceptionHandler] = None
_exception_handled = False


def set_exception_handler(handler: _ExceptionHandler) -> None:
    global _exception_handler
    _exception_handler = handler


def get_exception_handler() -> Optional[_ExceptionHandler]:
    return _exception_handler


def reset_exception_handled() -> None:
    global _exception_handled
    _exception_handled = False


# --------------------------------------------------------------------------- #
# Debugger construction.
#
# ipdb's own `_init_pdb` asks the IPython shell which class to use, and the
# answer is `Pdb if shell.simple_prompt else TerminalPdb`. `simple_prompt`
# defaults from a module constant IPython computes at *import* time from
# `sys.std{in,out,err}.isatty()`, so one import under pytest's capture — where
# stdin is `DontReadFromInput` — pins it False for the rest of the process and
# the post-mortem silently degrades to a plain `Pdb`: its `completekey` defaults
# to None, so `cmd` installs no readline completion, and without prompt_toolkit
# there is no `~/.pdbhistory` either. Suspending capture before attaching cannot
# undo it; the constant is already baked.
#
# Naming `TerminalPdb` outright sidesteps that: the terminal debugger keeps its
# own `_use_simple_prompt`, which consults only `IPY_TEST_SIMPLE_PROMPT`. The tty
# question is then asked here, at construction time, when the answer is current.
# --------------------------------------------------------------------------- #


def _stdio_is_tty():
    """Whether stdin *and* stdout are a terminal right now. Conservative in the
    same way as IPython's own probe: a missing, closed, or exotic stream is not
    a terminal."""
    for stream in (sys.stdin, sys.stdout):
        try:
            if not stream or not stream.isatty():
                return False
        except (AttributeError, ValueError):  # no isatty(), or a closed stream
            return False
    return True


def _init_pdb(context=None, commands=()):
    """Build the debugger — replaces ``ipdb.__main__._init_pdb`` (see above).

    Defers to ipdb's choice when there is no terminal to drive: piped stdio
    gives prompt_toolkit nothing to talk to, and a plain ``Pdb`` is then the
    right answer rather than a degraded one.
    """
    import os

    from ipdb.__main__ import get_context_from_config

    if context is None:
        context = os.getenv("IPDB_CONTEXT_SIZE", get_context_from_config())

    if not _stdio_is_tty():
        from ipdb.__main__ import _init_pdb as _ipdb_init_pdb

        return _ipdb_init_pdb(context, list(commands))

    from IPython.terminal.debugger import TerminalPdb

    try:
        p = TerminalPdb(context=context)
    except TypeError:  # parity with ipdb: tolerate a debugger without `context`
        p = TerminalPdb()
    p.rcLines.extend(commands)
    return p


# --------------------------------------------------------------------------- #
# REPL handler (verbatim from wake/utils/pdb_handler.py).
# --------------------------------------------------------------------------- #


def breakpoint_handler(frame=None, context=None, cond=True):
    """``sys.breakpointhook`` implementation giving ``breakpoint()`` the same
    rich REPL as the post-mortem. Not wired in-process (see module note); kept
    for the multiprocess runner and users who point ``PYTHONBREAKPOINT`` here."""
    import inspect  # noqa: F401  (parity with wake's import)

    from ipdb.__main__ import wrap_sys_excepthook

    if not cond:
        return
    wrap_sys_excepthook()
    if frame is None:
        frame = sys._getframe().f_back
    p = _init_pdb(context)
    p.default = lambda line: default_handler(p, line)
    x = p.set_trace(frame)
    if x and hasattr(p, "shell"):
        x.shell.restore_sys_module_state()


def default_handler(self, line):
    # If line starts with '!', strip it and remove leading/trailing whitespace
    if line[:1] == "!":
        line = line[1:].strip()

    locals = self.curframe_locals
    globals = self.curframe.f_globals

    try:
        # Compile the input line as a single interactive statement
        code = compile(line, "<stdin>", "single")

        # Save current I/O and displayhook
        save_stdout = sys.stdout
        save_stdin = sys.stdin
        save_displayhook = sys.displayhook

        try:
            # Redirect I/O to use the debugger's streams
            sys.stdin = self.stdin
            sys.stdout = self.stdout
            sys.displayhook = self.displayhook

            try:
                result = eval(line, globals, locals)
                if result is not None:
                    rich_print(result)
                    locals["_"] = result
            except SyntaxError:
                # Execute the compiled code in the current frame's context
                exec(code, globals, locals)

        finally:
            # Restore original I/O and displayhook
            sys.stdout = save_stdout
            sys.stdin = save_stdin
            sys.displayhook = save_displayhook

    except:  # noqa: E722  (mirrors wake: report anything, never leak out of the REPL)
        # Handle and report any exceptions
        self._error_exc()


# --------------------------------------------------------------------------- #
# breakpoint() debugger for the multiprocess runner (ports wake/testing/
# custom_pdb.py). Built lazily so importing this module — which the always-on
# pytest entry-point plugin pulls in — never eagerly imports IPython.
# --------------------------------------------------------------------------- #


def make_custom_pdb(cleanup: Callable[[], None]):
    """Return a ``TerminalPdb`` instance whose exit commands run ``cleanup``.

    The multiprocess worker installs this via ``sys.breakpointhook``: on
    ``breakpoint()`` it negotiates an attach with the terminal-owning process
    and, on accept, drops into this pdb positioned at the caller's frame.
    ``cleanup`` is the worker's idempotent "re-redirect stdio and ack the
    server" callback.

    Unlike wake's ``custom_pdb.py`` (which overrides only ``continue``/``quit``,
    so a ``Ctrl-D``/EOF exit leaves the server deadlocked waiting for the ack),
    this overrides *every* session-ending command — continue, quit, EOF. The
    worker additionally arms an ``atexit`` fallback, so the ack is guaranteed
    even on an abnormal debugger exit.
    """
    from IPython.terminal.debugger import TerminalPdb

    class CustomPdb(TerminalPdb):
        def do_continue(self, arg):
            cleanup()
            return super().do_continue(arg)

        do_c = do_cont = do_continue

        def do_quit(self, arg):
            cleanup()
            return super().do_quit(arg)

        do_q = do_exit = do_quit

        def do_EOF(self, arg):
            cleanup()
            return super().do_EOF(arg)

    return CustomPdb()


# --------------------------------------------------------------------------- #
# Post-mortem entry point (adapted from wake/development/globals.py).
# --------------------------------------------------------------------------- #


def attach_debugger(
    e_type: Optional[Type[BaseException]],
    e: Optional[BaseException],
    tb: Optional[TracebackType],
    seed: Optional[bytes] = None,
) -> None:
    """Drop into an ipdb post-mortem at the user's frame.

    Fires at most once between :func:`reset_exception_handled` calls (the pytest
    plugin resets it per test). ``seed``, when given, is printed for context;
    the plugin prints the full ``pytest --seed`` reproduce line.
    """
    global _exception_handled

    if _exception_handled:
        return
    _exception_handled = True

    import traceback

    from rich.console import Console
    from rich.traceback import Traceback

    console = Console()

    assert e_type is not None
    assert e is not None
    assert tb is not None

    # Persist the traceback so a failed run leaves an artifact (CI-friendly),
    # mirroring wake's `.wake/crash.txt`.
    tb_lines = traceback.format_exception(e_type, e, tb)
    try:
        crash_dir = Path.cwd() / ".wake-sol"
        crash_dir.mkdir(parents=True, exist_ok=True)
        (crash_dir / "crash.txt").write_text("".join(tb_lines))
    except OSError:
        pass  # best effort — never let artifact writing mask the real failure

    console.print(Traceback.from_exception(e_type, e, tb))

    frames = [(frame, lineno) for frame, lineno in traceback.walk_tb(tb)]

    # Position the debugger at the deepest user frame: walk inward→outward and
    # stop at the first frame that lives in the project (under cwd) but is
    # neither generated `pytypes/` nor this package's own internals.
    frames_up = 0
    pytypes_dir = Path.cwd() / "pytypes"
    for frame, _ in reversed(frames):
        fp = Path(frame.f_code.co_filename)
        if (
            _is_relative_to(fp, Path.cwd())
            and not _is_relative_to(fp, pytypes_dir)
            and not _is_relative_to(fp, _PKG_DIR)
        ):
            break
        frames_up += 1

    # Make the raised exception reachable from every frame. For this harness
    # that is a `TransactionFailed`, so the user gets `__exception__.tx`,
    # `.call_trace`, `.code`, ... from wherever they land.
    for f, _ in frames:
        f.f_globals["__exception__"] = e

    commands = []
    if frames_up > 0:
        commands.append("up %d" % frames_up)

    # wake injects `wake_random_seed = ...` as a pdb command here so the user can
    # replay; this harness has no mutable global seed (reproduction is
    # `pytest --seed <base>`), so just surface it for context.
    if seed is not None:
        console.print(f"[dim]base seed: {seed.hex()}[/]")

    try:
        p = _init_pdb(commands=commands)
        p.default = lambda line: default_handler(p, line)
        p.reset()
        p.interaction(None, tb)
    finally:
        for f, _ in frames:
            f.f_globals.pop("__exception__", None)


def _is_relative_to(path: Path, other: Path) -> bool:
    # Path.is_relative_to exists on 3.9+ (our floor) but tolerate odd co_filenames
    # like "<string>" / "<frozen ...>" that aren't real paths.
    try:
        return path.is_relative_to(other)
    except ValueError:
        return False
