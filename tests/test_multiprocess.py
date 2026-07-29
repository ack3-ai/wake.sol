"""Integration tests for the multiprocess test runner (`wake-sol test -P N`).

Each case invokes the CLI in a *subprocess* (`python -m wake_sol.cli test`)
against a throwaway test file written into ``tmp_path``, so the runner's own
fork/queue/pipe machinery is exercised end to end and the ``.wake-sol/``
log tree lands under ``tmp_path`` (never the repo). The plain-pipe cases (`_run`)
are not a tty, so the server's rich progress degrades to plain per-worker lines
and every attach prompt auto-declines — CI-safe. The Phase-2 interactive cases
(`_run_pty`) spawn the CLI under a pseudo-terminal so the server *does* prompt;
they answer the prompt over the pty, always kill the child's process group on
the way out, and are skipped where no pty is available.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time

import pytest

_PTY_UNAVAILABLE = not hasattr(os, "openpty")

_CLI = [sys.executable, "-m", "wake_sol.cli", "test"]

# Server summary prints:  "  #0: 0011aabb...   (reproduce: pytest --seed ...)"
_SEED_RE = re.compile(r"#(\d+): ([0-9a-f]{16})")


def _write(tmp_path, name: str, body: str):
    p = tmp_path / name
    p.write_text(body)
    return p


def _run(tmp_path, *args: str, env_extra: dict = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [*_CLI, *args],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )


def _seeds(stdout: str) -> dict:
    return {int(i): s for i, s in _SEED_RE.findall(stdout)}


def test_parallel_happy_path(tmp_path):
    _write(
        tmp_path, "test_ok.py",
        "def test_a():\n    assert True\n\n\ndef test_b():\n    assert 1 + 1 == 2\n",
    )
    r = _run(tmp_path, "-P", "2", "test_ok.py")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out

    # Both workers actually ran: one log file each.
    logs = tmp_path / ".wake-sol" / "logs" / "testing"
    assert (logs / "process-0.ansi").exists(), out
    assert (logs / "process-1.ansi").exists(), out

    # Both workers' seeds appear in the aggregated summary.
    seeds = _seeds(r.stdout)
    assert set(seeds) == {0, 1}, out

    # duplicated (default): 2 tests x 2 workers = 4 reported results.
    assert "4 passed" in r.stdout, out


def test_failure_propagates(tmp_path):
    _write(
        tmp_path, "test_bad.py",
        "def test_ok():\n    assert True\n\n\n"
        "def test_bad():\n    assert 2 + 2 == 5, 'boom-marker'\n",
    )
    r = _run(tmp_path, "-P", "2", "test_bad.py")
    out = r.stdout + r.stderr
    # A worker failure must flip the server's exit status (reports replayed into
    # the server session make Session.testsfailed nonzero).
    assert r.returncode != 0, out
    # The failure shows up in the aggregated output.
    assert "boom-marker" in r.stdout, out
    assert "failed" in r.stdout, out


def test_seeds_differ_and_are_reproduced(tmp_path):
    _write(tmp_path, "test_ok.py", "def test_a():\n    assert True\n")
    r = _run(tmp_path, "-P", "3", "test_ok.py")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out

    seeds = _seeds(r.stdout)
    assert len(seeds) == 3, out
    assert len(set(seeds.values())) == 3, ("seeds not distinct", out)
    # Every worker's seed comes with a runnable reproduce line.
    for s in seeds.values():
        assert f"pytest --seed {s}" in r.stdout, out


def test_pinned_seed_is_used(tmp_path):
    _write(tmp_path, "test_ok.py", "def test_a():\n    assert True\n")
    pinned = "aabbccddeeff0011"
    r = _run(tmp_path, "-P", "2", "-S", pinned, "test_ok.py")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    seeds = _seeds(r.stdout)
    # -S pins worker 0; the rest are random and must differ from it.
    assert seeds[0] == pinned, out
    assert seeds[1] != pinned, out


def test_uniform_shards_do_not_duplicate(tmp_path):
    _write(
        tmp_path, "test_ok.py",
        "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n",
    )
    r = _run(tmp_path, "-P", "2", "--dist", "uniform", "test_ok.py")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "dist=uniform" in r.stdout, out
    # Sharded: each of the 2 tests runs exactly once (not once per worker).
    assert "2 passed" in r.stdout, out


def test_collection_mismatch_errors(tmp_path):
    # Each forked worker parametrizes on its own pid, so the collected node ids
    # differ across workers and the identical-collection guard must fire.
    _write(
        tmp_path, "test_mismatch.py",
        "import os\n"
        "import pytest\n\n\n"
        "@pytest.mark.parametrize('pid', [os.getpid()])\n"
        "def test_pid(pid):\n    assert True\n",
    )
    r = _run(tmp_path, "-P", "2", "test_mismatch.py")
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "collected different tests" in out, out


# --------------------------------------------------------------------------- #
# Interactive cross-process debugging.
# --------------------------------------------------------------------------- #

_FAILING = (
    "def test_ok():\n    assert True\n\n\n"
    "def test_bad():\n    assert 2 + 2 == 5, 'boom-marker'\n"
)

# A test that raises a real TransactionFailed — pyo3 objects that will not
# pickle, forcing the worker's Exception(repr(e)) fallback.
_TXFAIL = (
    "from wake_sol import Account, svm\n\n\n"
    "def test_ok():\n    assert True\n\n\n"
    "def test_txfail():\n"
    "    a = Account.new()\n"
    "    svm.airdrop(a, 2_000_000)\n"
    "    a.tx(svm.system.transfer(10**18, from_=a, to=Account.new()))\n"
)


def _run_pty(tmp_path, args, attach_answers, ipdb_cmd="q", timeout=90):
    """Run the CLI under a pseudo-tty so the server sees an interactive terminal.

    Answers each ``Attach the debugger? [y/n]`` prompt from ``attach_answers``
    (falling back to ``n`` once exhausted, so an unexpected extra prompt can
    never wedge the run) and, when an ipdb session opens, sends ``ipdb_cmd``.
    Terminal echo is disabled so responses don't come back on the read stream.
    The child runs in its own process group, which is **always** killed on the
    way out — a hang can never leak past this helper. Returns
    ``(returncode, decoded_output)``.
    """
    import pty
    import select
    import termios

    master, slave = pty.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attrs)

    proc = subprocess.Popen(
        [*_CLI, *args],
        cwd=str(tmp_path),
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=dict(os.environ, TERM="xterm"),
        start_new_session=True,
    )
    os.close(slave)

    chunks: list = []
    window = b""
    answered = 0
    deadline = time.time() + timeout
    try:
        while True:
            if proc.poll() is not None and not select.select([master], [], [], 0)[0]:
                break
            if time.time() > deadline:
                os.killpg(proc.pid, signal.SIGKILL)
                raise AssertionError(
                    "pty run timed out:\n" + b"".join(chunks).decode(errors="replace")
                )
            r, _, _ = select.select([master], [], [], 0.5)
            if master not in r:
                continue
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
            window += data
            if b"Attach the debugger?" in window:
                ans = attach_answers[answered] if answered < len(attach_answers) else "n"
                answered += 1
                time.sleep(0.2)
                os.write(master, (ans + "\n").encode())
                window = b""
            elif b"ipdb>" in window:
                time.sleep(0.2)
                os.write(master, (ipdb_cmd + "\n").encode())
                window = b""
    finally:
        try:
            os.close(master)
        except OSError:
            pass
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            rc = proc.wait()
    return rc, b"".join(chunks).decode(errors="replace")


def test_attach_nontty_auto_declines(tmp_path):
    # Not a tty -> the server auto-declines: no prompt, and the run finishes (the
    # _run timeout would surface a hang as a test error). The failure is still
    # reported through normal channels.
    _write(tmp_path, "test_bad.py", _FAILING)
    r = _run(tmp_path, "-P", "2", "--attach", "test_bad.py")
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "Attach the debugger" not in out, out
    assert "boom-marker" in r.stdout, out


def test_attach_nontty_unpicklable_exception(tmp_path):
    # A real TransactionFailed can't pickle; the worker falls back to
    # Exception(repr(e)) and still negotiates cleanly (auto-declined here, no
    # tty). Deterministic coverage of the fallback pickling path without a pty.
    _write(tmp_path, "test_tx.py", _TXFAIL)
    r = _run(tmp_path, "-P", "2", "--attach", "test_tx.py")
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "Attach the debugger" not in out, out
    assert "ResultWithNegativeLamports" in out, out  # failure still surfaced


def test_breakpoint_nontty_auto_continues(tmp_path):
    # breakpoint() is always wired under -P; on a non-tty the server auto-declines
    # so the run continues instead of blocking on a prompt that can't be answered.
    _write(
        tmp_path, "test_bp.py",
        "def test_bp():\n    x = 41\n    breakpoint()\n    assert x == 41\n",
    )
    r = _run(tmp_path, "-P", "2", "test_bp.py")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "2 passed" in r.stdout, out


def test_attach_and_attach_first_mutually_exclusive(tmp_path):
    _write(tmp_path, "test_ok.py", "def test_a():\n    assert True\n")
    r = _run(tmp_path, "-P", "2", "--attach", "--attach-first", "test_ok.py")
    out = r.stdout + r.stderr
    assert r.returncode == 2, out  # click BadParameter
    assert "mutually exclusive" in out, out


def test_attach_first_tees_worker_zero(tmp_path):
    _write(
        tmp_path, "test_tee.py",
        "def test_a():\n    print('MARKER_ZERO')\n    assert True\n\n\n"
        "def test_b():\n    print('MARKER_ONE')\n    assert True\n",
    )
    # uniform: worker 0 runs test_a (index 0), worker 1 runs test_b (index 1).
    r = _run(tmp_path, "-P", "2", "--dist", "uniform", "--attach-first", "test_tee.py")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    logs = tmp_path / ".wake-sol" / "logs" / "testing"
    log0 = (logs / "process-0.ansi").read_text()
    log1 = (logs / "process-1.ansi").read_text()
    # Worker 0 tees: its output is live on the console AND in its own log.
    assert "MARKER_ZERO" in out, out
    assert "MARKER_ZERO" in log0
    # Worker 1 only redirects: its output lands in its log, never on the console.
    assert "MARKER_ONE" in log1
    assert "MARKER_ONE" not in out, out


@pytest.mark.skipif(_PTY_UNAVAILABLE, reason="requires a POSIX pty")
def test_pty_attach_decline(tmp_path):
    # duplicated (default): both workers fail -> two prompts, both declined.
    _write(tmp_path, "test_bad.py", _FAILING)
    rc, out = _run_pty(
        tmp_path, ["-P", "2", "--attach", "test_bad.py"], attach_answers=["n", "n"]
    )
    assert "Attach the debugger" in out, out          # the prompt actually fired
    assert "raised the exception above" in out, out    # server rendered the tb
    assert rc == 1, out                                # declining -> normal fail
    assert "boom-marker" in out, out


@pytest.mark.skipif(_PTY_UNAVAILABLE, reason="requires a POSIX pty")
def test_pty_attach_then_quit(tmp_path):
    # uniform + one failing test -> exactly one negotiation. Answer "y", drop into
    # ipdb, then quit; the run must continue to a normal completion (exit 1).
    _write(tmp_path, "test_bad.py", _FAILING)
    rc, out = _run_pty(
        tmp_path,
        ["-P", "2", "--dist", "uniform", "--attach", "test_bad.py"],
        attach_answers=["y"],
        ipdb_cmd="q",
    )
    assert "Attach the debugger" in out, out
    assert rc == 1, out
    assert "1 failed" in out, out


@pytest.mark.skipif(_PTY_UNAVAILABLE, reason="requires a POSIX pty")
def test_pty_repr_fallback_renders(tmp_path):
    # A real (unpicklable) TransactionFailed still negotiates, and the server
    # renders the repr'd traceback before prompting.
    _write(tmp_path, "test_tx.py", _TXFAIL)
    rc, out = _run_pty(
        tmp_path,
        ["-P", "2", "--dist", "uniform", "--attach", "test_tx.py"],
        attach_answers=["n"],
    )
    assert "Attach the debugger" in out, out
    assert "ResultWithNegativeLamports" in out, out
    assert rc == 1, out


@pytest.mark.skipif(_PTY_UNAVAILABLE, reason="requires a POSIX pty")
def test_pty_breakpoint_attach_continue(tmp_path):
    # breakpoint() negotiation: attach, then continue out of ipdb; run completes.
    _write(
        tmp_path, "test_bp.py",
        "def test_a():\n    assert True\n\n\n"
        "def test_bp():\n    x = 41\n    breakpoint()\n    assert x == 41\n",
    )
    rc, out = _run_pty(
        tmp_path,
        ["-P", "2", "--dist", "uniform", "test_bp.py"],
        attach_answers=["y"],
        ipdb_cmd="c",
    )
    assert "hit a breakpoint" in out, out
    assert rc == 0, out
    assert "2 passed" in out, out


# --------------------------------------------------------------------------- #
# Fuzz-stats aggregation, crash-log JSONs, report serialization.
# These fuzz tests are pure-Python (a shadow model, no compiled program) so they
# run in CI without a built .so — see tests/test_fuzz_counter.py for the on-chain
# version whose flow/invariant shape they mirror.
# --------------------------------------------------------------------------- #

_FUZZ_PASS = (
    "from wake_sol import FuzzTest, flow, invariant, random\n\n\n"
    "class ModelFuzz(FuzzTest):\n"
    "    def pre_sequence(self):\n"
    "        self.model = 0\n\n"
    "    @flow(weight=200)\n"
    "    def inc(self):\n"
    "        self.model += 1\n\n"
    "    @flow(weight=100)\n"
    "    def inc_batch(self):\n"
    "        self.model += random.randint(2, 4)\n\n"
    "    @invariant()\n"
    "    def non_negative(self):\n"
    "        assert self.model >= 0\n\n\n"
    "def test_model_fuzz():\n"
    "    ModelFuzz.run(sequences_count=3, flows_count=10)\n"
)

# Fails deterministically at sequence 0, flow 1 (model reaches 2 after the second
# inc, tripping `model < 2`). Trace is ["inc", "inc"], failing = "invariant bounded".
_FUZZ_FAIL = (
    "from wake_sol import FuzzTest, flow, invariant\n\n\n"
    "class BadFuzz(FuzzTest):\n"
    "    def pre_sequence(self):\n"
    "        self.model = 0\n\n"
    "    @flow()\n"
    "    def inc(self):\n"
    "        self.model += 1\n\n"
    "    @invariant()\n"
    "    def bounded(self):\n"
    "        assert self.model < 2, f'model too big: {self.model}'\n\n\n"
    "def test_bad_fuzz():\n"
    "    BadFuzz.run(sequences_count=2, flows_count=10)\n"
)


def test_parallel_fuzz_stats_aggregated(tmp_path):
    _write(tmp_path, "test_fuzz.py", _FUZZ_PASS)
    r = _run(tmp_path, "-P", "2", "test_fuzz.py")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out

    # Exactly one aggregated table (the server's) — the per-worker per-run tables
    # go to each worker's redirected log, never the console.
    assert out.count("aggregated flow stats") == 1, out
    assert "(3 sequences x 10 flows)" not in r.stdout, out  # per-run table not on console

    # rich wraps a table's title to the table width, so match on the flattened
    # (whitespace-collapsed) stdout. Labeled across N workers, with the summed
    # budget: 3+3 sequences, 2*(3*10) flow-steps.
    flat = " ".join(r.stdout.split())
    assert (
        "aggregated flow stats across 2 workers (6 sequences, 60 flow-steps)" in flat
    ), out
    # Both flows present in the merged table.
    assert "inc_batch" in r.stdout, out

    # Two workers actually contributed.
    assert set(_seeds(r.stdout)) == {0, 1}, out


def test_parallel_fuzz_failure_crash_logs(tmp_path):
    import json

    _write(tmp_path, "test_bad.py", _FUZZ_FAIL)
    r = _run(tmp_path, "-P", "2", "test_bad.py")
    out = r.stdout + r.stderr
    assert r.returncode != 0, out

    # duplicated dist ⇒ both workers hit the same deterministic failure and each
    # writes a crash log under its own process-N dir; assert at least one exists.
    crashes = tmp_path / ".wake-sol" / "logs" / "crashes"
    files = sorted(crashes.rglob("*.json"))
    assert files, ("no crash logs under " + str(crashes), out)
    assert any("process-" in str(f.parent) for f in files), out

    # Listed in the server's terminal summary.
    assert "Crash logs:" in r.stdout, out

    # The JSON parses and carries the fuzz context + a copy-paste reproduce line.
    data = json.loads(files[0].read_text())
    assert data["nodeid"] == "test_bad.py::test_bad_fuzz", data
    assert data["fuzz_class"] == "BadFuzz", data
    assert data["sequence"] == 0 and data["flow"] == 1, data
    assert data["failing"] == "invariant bounded", data
    assert data["trace"] == ["inc", "inc"], data
    assert data["exception"]["type"] == "AssertionError", data
    assert data["seed"] in set(_seeds(r.stdout).values()), (data, out)
    assert data["reproduce"] == f'pytest --seed {data["seed"]} "{data["nodeid"]}"', data


def test_single_process_fuzz_failure_crash_log(tmp_path):
    import json

    _write(tmp_path, "test_bad.py", _FUZZ_FAIL)
    r = _run(tmp_path, "test_bad.py")  # no -P → single-process
    out = r.stdout + r.stderr
    assert r.returncode != 0, out

    # Single-process crashes land directly under logs/crashes (no process-N dir).
    crashes = tmp_path / ".wake-sol" / "logs" / "crashes"
    files = sorted(crashes.glob("*.json"))
    assert files, ("no crash log under " + str(crashes), out)
    assert "Crash logs:" in r.stdout, out

    data = json.loads(files[0].read_text())
    assert data["nodeid"] == "test_bad.py::test_bad_fuzz", data
    assert data["failing"] == "invariant bounded", data
    assert data["trace"] == ["inc", "inc"], data


def test_single_process_pass_writes_no_crash_log(tmp_path):
    _write(tmp_path, "test_fuzz.py", _FUZZ_PASS)
    r = _run(tmp_path, "test_fuzz.py")  # no -P → single-process
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    crashes = tmp_path / ".wake-sol" / "logs" / "crashes"
    # A passing run never touches the crashes tree.
    assert not crashes.exists() or not list(crashes.rglob("*.json")), out
    assert "Crash logs:" not in r.stdout, out


def test_nonfuzz_failure_writes_no_crash_log(tmp_path):
    # A plain (non-FuzzTest) failure must not produce a crash-log JSON — the
    # fuzz-failure context is None, so pytest_exception_interact writes nothing.
    _write(tmp_path, "test_plain.py", "def test_boom():\n    assert 1 == 2\n")
    r = _run(tmp_path, "test_plain.py")  # single-process
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    crashes = tmp_path / ".wake-sol" / "logs" / "crashes"
    assert not crashes.exists() or not list(crashes.rglob("*.json")), out
    assert "Crash logs:" not in r.stdout, out


def test_report_serialization_pickle_and_json_fallback():
    # Unit test the send/receive helpers directly (both paths). Constructing a
    # genuinely unpicklable longrepr through the real runner isn't required; here
    # an unpicklable extra attribute forces the JSON fallback (pickle sees the
    # whole __dict__; _to_json only reads the known fields, so it still succeeds).
    from wake_sol._mp_serial import (
        REPORT_JSON,
        REPORT_PICKLE,
        dump_report,
        load_report,
    )

    rep = pytest.TestReport(
        nodeid="t.py::test_x",
        location=("t.py", 1, "test_x"),
        keywords={},
        outcome="failed",
        longrepr="boom",
        when="call",
    )

    kind, payload = dump_report(rep)
    assert kind == REPORT_PICKLE
    back = load_report(kind, payload)
    assert (back.nodeid, back.outcome, back.when) == ("t.py::test_x", "failed", "call")

    rep._unpicklable = lambda: None  # noqa: E731 — deliberately unpicklable
    kind2, payload2 = dump_report(rep)
    assert kind2 == REPORT_JSON
    assert isinstance(payload2, dict)
    back2 = load_report(kind2, payload2)
    assert (back2.nodeid, back2.outcome, back2.when) == ("t.py::test_x", "failed", "call")
