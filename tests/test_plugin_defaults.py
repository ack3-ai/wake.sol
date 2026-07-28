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
    res = _run_bare(root, "-p", "no:solana_fuzzer")
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
    res = _run_bare(_bp_project(pytester), "-p", "no:solana_fuzzer", stdin="c\n")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "(Pdb)" in res.stdout and "ipdb>" not in res.stdout


def test_explicit_pdbcls_still_wins(pytester: pytest.Pytester) -> None:
    """The default is a default: `--pdbcls=pdb:Pdb` restores stock pdb."""
    res = _run_bare(_bp_project(pytester), "--pdbcls=pdb:Pdb", stdin="c\n")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "(Pdb)" in res.stdout and "ipdb>" not in res.stdout


def test_sys_path_insert_is_cleaned_up(pytester: pytest.Pytester) -> None:
    """Run in-process so the hook touches *this* sys.path, and check it tidies up."""
    pytester.makepyfile("def test_noop():\n    pass\n")
    before = list(sys.path)
    pytester.runpytest_inprocess().assert_outcomes(passed=1)
    assert str(pytester.path) not in sys.path
    assert list(sys.path) == before
