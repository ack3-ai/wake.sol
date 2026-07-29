"""The plugin must give *every* consumer project two defaults, with no
project-local pytest config:

1. the project root on ``sys.path``, so a generated root-level ``pytypes/``
   imports under a bare ``pytest``;
2. ``breakpoint()`` landing in ipdb rather than stock pdb.

Both used to live in this repo's own ``pyproject.toml``, which did nothing for
downstream projects.

Two testing traps these avoid, both of which silently make the assertions pass
for the wrong reason:

* ``pytester.runpytest_subprocess()`` shells out to ``python -m pytest``, and
  ``-m`` puts the cwd on ``sys.path`` by itself.
* ``pytester.popen()`` (so also ``pytester.run()``) injects ``os.getcwd()`` into
  the child's ``PYTHONPATH`` — see ``_pytest/pytester.py:1365``.

So these drive the bare ``pytest`` console script through ``subprocess`` with
``PYTHONPATH`` stripped.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

BARE_PYTEST = str(Path(sys.executable).with_name("pytest"))


def _run_bare(cwd: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    """Bare `pytest` in `cwd`, with PYTHONPATH removed so only the plugin can put
    the project root on sys.path."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        [BARE_PYTEST, *args],
        cwd=str(cwd),
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _downstream_project(pytester: pytest.Pytester) -> Path:
    """A consumer project: root-level `pytypes/`, tests under `tests/`."""
    pytester.makepyprojecttoml("[project]\nname='downstream'\nversion='0.1.0'\n")
    (pytester.path / "pytypes").mkdir()
    (pytester.path / "pytypes" / "__init__.py").write_text("MARKER = 'imported'\n")
    (pytester.path / "tests").mkdir()
    (pytester.path / "tests" / "test_root_import.py").write_text(
        "def test_import():\n"
        "    import pytypes\n"
        "    assert pytypes.MARKER == 'imported'\n"
    )
    return pytester.path


def test_project_root_is_on_sys_path(pytester: pytest.Pytester) -> None:
    root = _downstream_project(pytester)
    res = _run_bare(root)
    assert res.returncode == 0, res.stdout + res.stderr


def test_root_import_fails_without_the_plugin(pytester: pytest.Pytester) -> None:
    """The A/B: proves the pass above is the plugin's doing, not pytest's."""
    root = _downstream_project(pytester)
    res = _run_bare(root, "-p", "no:wake_sol")
    assert res.returncode != 0
    assert "No module named 'pytypes'" in res.stdout


def _bp_project(pytester: pytest.Pytester) -> Path:
    pytester.makepyfile("def test_bp():\n    x = 7\n    breakpoint()\n    assert x == 7\n")
    return pytester.path


def test_breakpoint_uses_ipdb(pytester: pytest.Pytester) -> None:
    res = _run_bare(_bp_project(pytester), stdin="c\n")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "ipdb>" in res.stdout


def test_breakpoint_is_stock_pdb_without_the_plugin(pytester: pytest.Pytester) -> None:
    res = _run_bare(_bp_project(pytester), "-p", "no:wake_sol", stdin="c\n")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "(Pdb)" in res.stdout and "ipdb>" not in res.stdout


def test_explicit_pdbcls_still_wins(pytester: pytest.Pytester) -> None:
    """The default is a default: `--pdbcls=pdb:Pdb` restores stock pdb."""
    res = _run_bare(_bp_project(pytester), "--pdbcls=pdb:Pdb", stdin="c\n")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "(Pdb)" in res.stdout and "ipdb>" not in res.stdout


def test_ipdb_routing_does_not_import_ipython(pytester: pytest.Pytester) -> None:
    """Routing `breakpoint()` must not *import* IPython at configure time.

    `IPython.terminal.interactiveshell` computes an "are the streams a tty"
    answer once, at import. Imported from `pytest_configure` — capture active,
    `sys.stdin` a `DontReadFromInput` — it stays False for the whole process, and
    ipdb's `shell.debugger_cls` then hands the `--attach` post-mortem a plain
    `Pdb`: no tab completion, no `~/.pdbhistory`.
    """
    pytester.makepyfile(
        "import sys\n"
        "def test_ipython_untouched():\n"
        "    assert 'IPython' not in sys.modules\n"
    )
    res = _run_bare(pytester.path)
    assert res.returncode == 0, res.stdout + res.stderr


def test_debugger_is_terminal_pdb_on_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given a terminal, the debugger built for the post-mortem is the
    prompt_toolkit one — the completion and history the plain `Pdb` lacks. The
    tty itself is stubbed; what matters is which class the choice lands on."""
    from wake_sol import _debug

    monkeypatch.setattr(_debug, "_stdio_is_tty", lambda: True)
    p = _debug._init_pdb(commands=["up 2"])

    try:
        assert type(p).__name__ == "TerminalPdb"
        assert p.pt_app.completer is not None      # tab completion
        assert p.pt_app.history is not None        # persistent history
        assert p.rcLines == ["up 2"]               # the "land on the user's frame" cmd
    finally:
        # A real prompt session was built: close what it opened, so the test
        # leaves no event loop or idle worker thread behind.
        p.pt_loop.close()
        p.thread_executor.shutdown(wait=False)


def test_debugger_falls_back_without_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no terminal there is nothing for prompt_toolkit to drive, so hand the
    choice back to ipdb rather than force a session that cannot read input.

    Asserts the delegation, not the class it returns: ipdb's answer depends on
    process-global state (whether IPython was imported on a tty), so pinning the
    class would make this pass or fail for reasons unrelated to the fallback."""
    import ipdb.__main__

    from wake_sol import _debug

    calls = []
    monkeypatch.setattr(_debug, "_stdio_is_tty", lambda: False)
    monkeypatch.setattr(ipdb.__main__, "_init_pdb",
                        lambda context=None, commands=[]: calls.append((context, commands)))

    _debug._init_pdb(commands=["up 2"])
    assert len(calls) == 1
    context, commands = calls[0]
    assert context is not None       # ipdb's own context resolution still applied
    assert commands == ["up 2"]


def test_sys_path_insert_is_cleaned_up(pytester: pytest.Pytester) -> None:
    """Run in-process so the hook touches *this* sys.path, and check it tidies up."""
    pytester.makepyfile("def test_noop():\n    pass\n")
    before = list(sys.path)
    pytester.runpytest_inprocess().assert_outcomes(passed=1)
    assert str(pytester.path) not in sys.path
    assert list(sys.path) == before
