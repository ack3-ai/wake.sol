"""Command-line interface for wake.sol."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click


@click.group()
def cli() -> None:
    """wake.sol — a Solana program testing and fuzzing harness."""


def _strip_seed(args: list[str]) -> list[str]:
    """Drop any ``--seed <hex>`` / ``--seed=<hex>`` from passthrough args.

    In multi-process mode the CLI owns seeds (``-S``); the server session runs
    with ``-p no:wake_sol`` so ``--seed`` is not even a registered option
    there, and each worker gets its own injected ``--seed``.
    """
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "--seed":
            skip = True
            continue
        if a.startswith("--seed="):
            continue
        out.append(a)
    return out


#: Options the wake_sol pytest plugin registers. The server session runs with
#: ``-p no:wake_sol``, so none of them exist there and pytest would reject the
#: whole invocation; the workers, which do load the plugin, still get them.
#:
#: ``--coverage``/``--cov`` take an *optional* count, so stripping them also has
#: to consume a following bare integer — and only an integer, or
#: ``--cov tests/foo.py`` would silently lose the test path.
_PLUGIN_OPTIONAL_VALUE = ("--coverage", "--cov")
_PLUGIN_OPTIONS = ("--coverage-report", "--coverage-source", "--coverage-sync")


def _is_int(text: str) -> bool:
    try:
        int(text)
    except ValueError:
        return False
    return True


def _strip_plugin_opts(args: list[str]) -> list[str]:
    """Drop wake_sol plugin options from a server-side pytest invocation."""
    out: list[str] = []
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a in _PLUGIN_OPTIONAL_VALUE:
            nxt = args[i + 1] if i + 1 < len(args) else None
            skip = nxt is not None and _is_int(nxt)
            continue
        if a in _PLUGIN_OPTIONS:
            skip = True
            continue
        if any(a.startswith(f"{opt}=") for opt in (*_PLUGIN_OPTIONS, *_PLUGIN_OPTIONAL_VALUE)):
            continue
        out.append(a)
    return out


def _coverage_count(args: list[str]) -> int:
    """The `--cov` / `--coverage` value: 0 off, -1 all workers, else a count.

    Parsed here as well as by the plugin because the server, which writes the
    merged report, runs with the plugin disabled and never sees the option.
    """
    from wake_sol._pytest_plugin import _COVERAGE_ALL

    count = 0
    for i, a in enumerate(args):
        if a in _PLUGIN_OPTIONAL_VALUE:
            nxt = args[i + 1] if i + 1 < len(args) else None
            count = int(nxt) if nxt is not None and _is_int(nxt) else _COVERAGE_ALL
        elif any(a.startswith(f"{opt}=") for opt in _PLUGIN_OPTIONAL_VALUE):
            value = a.split("=", 1)[1]
            count = int(value) if _is_int(value) else _COVERAGE_ALL
    return count


def _option_value(args: list[str], name: str) -> Optional[str]:
    """The value of `--name <value>` or `--name=<value>`, last one winning.

    Read here because the server runs with the plugin disabled, so it never
    parses these itself — but it is the process that merges the workers' counts
    and therefore the one that writes the report.
    """
    value = None
    expect = False
    for a in args:
        if expect:
            value, expect = a, False
            continue
        if a == name:
            expect = True
        elif a.startswith(f"{name}="):
            value = a[len(name) + 1:]
    return value


@cli.command(context_settings=dict(ignore_unknown_options=True))
@click.option(
    "-P", "--proc", "proc_count",
    type=int, is_flag=False, flag_value=-1, default=None,
    help="Run tests across N worker processes (bare -P = one per CPU). "
         "Each worker is a full pytest run with its own seed.",
)
@click.option(
    "-S", "--seed", "seeds", multiple=True, type=str,
    help="Per-worker base seed (hex), repeatable. Remaining workers get a "
         "random seed. Only used with -P.",
)
@click.option(
    "--dist", type=click.Choice(["uniform", "duplicated"]), default="duplicated",
    show_default=True,
    help="duplicated: every worker runs every test (N seeds — the fuzzing use "
         "case). uniform: shard tests across workers (wall-clock speed).",
)
@click.option(
    "-d", "--attach", "attach", is_flag=True, default=False,
    help="On a test failure, drop into an ipdb post-mortem at your frame. With "
         "-P, the worker that failed hands the debugger to your terminal.",
)
@click.option(
    "--attach-first", "attach_first", is_flag=True, default=False,
    help="With -P, stream worker 0's output live to the console (the rest go to "
         "their logs) and never prompt to attach. Mutually exclusive with --attach.",
)
@click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
def test(
    proc_count: int | None,
    seeds: tuple[str, ...],
    dist: str,
    attach: bool,
    attach_first: bool,
    pytest_args: tuple[str, ...],
) -> None:
    """Run tests with pytest.

    The wake_sol pytest plugin (auto-loaded) resets the global SVM and
    reseeds `random` before each test. Pass `--seed <hex>` for a fixed base
    seed; all arguments are forwarded to pytest.

    With `-P N`, tests run across N worker processes (one server + N workers,
    all forked). `--dist duplicated` (default) runs every test in every worker
    with a distinct seed — N fuzzing runs in parallel; `--dist uniform` shards
    the tests for wall-clock speed. Use `-S <hex>` (repeatable) to pin
    per-worker seeds; the rest are random.

    `--attach`/`-d` drops into an interactive ipdb post-mortem on failure (and
    on `breakpoint()`); under `-P` the failing worker negotiates with your
    terminal. `--attach-first` instead streams worker 0 live and never prompts;
    the two are mutually exclusive.
    """
    import pytest

    if attach and attach_first:
        raise click.BadParameter("--attach and --attach-first are mutually exclusive")

    if proc_count is None:
        # Single-process: exactly the historical thin pytest.main wrapper.
        # --attach maps onto the entry-point plugin's --attach pytest option
        # (--attach-first is a no-op without workers).
        args = list(pytest_args)
        if attach:
            args.append("--attach")
        sys.exit(pytest.main(args))

    import os

    from wake_sol._mp_server import PytestPluginMultiprocessServer

    if proc_count == -1:
        proc_count = os.cpu_count() or 1
    if proc_count < 1:
        raise click.BadParameter("-P must be >= 1")

    try:
        worker_seeds = [bytes.fromhex(s) for s in seeds]
    except ValueError:
        raise click.BadParameter("seeds (-S) must be hex strings")
    while len(worker_seeds) < proc_count:
        worker_seeds.append(os.urandom(8))

    # Coverage options are parsed here as well as in the plugin: the server runs
    # with the plugin disabled, but it is the process that merges the workers'
    # counts and writes the report, so it needs to know all of this itself.
    from wake_sol._pytest_plugin import (
        _COVERAGE_ALL,
        _DEFAULT_COVERAGE_REPORT,
        _DEFAULT_COVERAGE_SYNC,
    )

    cov_count = _coverage_count(list(pytest_args))
    if cov_count != 0:
        wanted = proc_count if cov_count == _COVERAGE_ALL else cov_count
        if wanted > proc_count:
            raise click.BadParameter(
                f"--cov {cov_count} asks for more workers than -P {proc_count}"
            )

    raw = _option_value(list(pytest_args), "--coverage-report")
    if raw:
        coverage_report = Path(raw).resolve()
    elif cov_count != 0:
        coverage_report = (Path.cwd() / _DEFAULT_COVERAGE_REPORT).resolve()
    else:
        coverage_report = None

    raw_sync = _option_value(list(pytest_args), "--coverage-sync")
    try:
        coverage_sync = (
            _DEFAULT_COVERAGE_SYNC if raw_sync is None else float(raw_sync)
        )
    except ValueError:
        raise click.BadParameter("--coverage-sync must be a number of seconds")

    base_args = _strip_seed(list(pytest_args))
    # Workers: run their output uncaptured (-s) so the per-worker log file
    # captures everything; the server injects each worker's --seed. The debugger
    # flags are NOT forwarded to pytest — workers receive them as plugin
    # constructor booleans, and the entry-point --attach option is never passed.
    worker_args = base_args + ["-s"]
    # Server: collects but never runs; disable the entry-point plugin so it does
    # not register --seed or print a "Base seed" summary. It also runs with -s:
    # pytest's stdin capture otherwise swaps in a non-tty object, so the attach
    # prompt (input() + sys.stdin.isatty()) would never fire on a real terminal.
    server_args = _strip_plugin_opts(base_args) + ["-p", "no:wake_sol", "-s"]

    logs_dir = Path.cwd() / ".wake-sol" / "logs" / "testing"

    sys.exit(
        pytest.main(
            server_args,
            plugins=[
                PytestPluginMultiprocessServer(
                    proc_count, worker_seeds, dist, worker_args, logs_dir,
                    attach=attach, attach_first=attach_first,
                    coverage_report=coverage_report,
                    coverage_sync=coverage_sync,
                )
            ],
        )
    )


_GEN_OPTIONS = [
    click.option("--target-idl", "target_idls", multiple=True,
                 type=click.Path(file_okay=False, path_type=Path),
                 default=("target/idl",), show_default=True,
                 help="Local-build IDL root (repeatable)."),
    click.option("--idls", "dep_idls", multiple=True,
                 type=click.Path(file_okay=False, path_type=Path),
                 default=("idls",), show_default=True,
                 help="Dependency-IDL root (repeatable)."),
    click.option("--out", type=click.Path(file_okay=False, path_type=Path),
                 default="pytypes", show_default=True,
                 help="Output package directory."),
    click.option("--only", "only", multiple=True,
                 help="Generate only these base58 program addresses (repeatable)."),
    click.option("--check", is_flag=True,
                 help="Diff a fresh regen against --out; exit 2 on drift. Writes nothing."),
    click.option("--strict", is_flag=True,
                 help="Treat any per-program refusal as a hard failure."),
    click.option("-v", "--verbose", count=True, help="Per-program gen log."),
]


def _gen_options(fn):
    for opt in reversed(_GEN_OPTIONS):
        fn = opt(fn)
    return fn


@cli.group(invoke_without_command=True)
@_gen_options
@click.pass_context
def gen(ctx: click.Context, **opts) -> None:
    """Generate the pytypes/ package from Anchor IDLs."""
    ctx.obj = opts
    if ctx.invoked_subcommand is None:
        from wake_sol._gen import run_gen

        ctx.exit(run_gen(**opts))


@gen.command("run")
@click.pass_context
def gen_run(ctx: click.Context) -> None:
    """Generate the package (the default action)."""
    from wake_sol._gen import run_gen

    ctx.exit(run_gen(**ctx.obj))


@gen.command("check")
@click.pass_context
def gen_check(ctx: click.Context) -> None:
    """Alias for `gen --check`: regenerate and diff against --out."""
    from wake_sol._gen import run_gen

    opts = dict(ctx.obj)
    opts["check"] = True
    ctx.exit(run_gen(**opts))


@gen.command("list")
@click.pass_context
def gen_list(ctx: click.Context) -> None:
    """Print the discovered {address: idl_path, source_root} table; no codegen."""
    from wake_sol._gen.run import _discover

    opts = ctx.obj
    discovered = _discover(opts["target_idls"], opts["dep_idls"], opts["verbose"])
    for addr in sorted(discovered):
        _idl, path, src = discovered[addr]
        click.echo(f"{addr}\t{path}\t({src})")


@cli.group()
def coverage() -> None:
    """Work with coverage reports."""


@coverage.command("merge")
@click.argument("inputs", nargs=-1, required=True,
                type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--out", required=True,
              type=click.Path(dir_okay=False, path_type=Path),
              help="Where to write the merged LCOV.")
@click.option("--force", is_flag=True,
              help="Merge even if the inputs describe different builds.")
def coverage_merge(inputs: tuple[Path, ...], out: Path, force: bool) -> None:
    """Merge LCOV reports, summing per-line counts.

    For CI shards and repeated runs — `wake-sol test -P N` already merges its own
    workers and writes one report.

    Refuses when two inputs cover different builds of the same program: their
    line numbers describe different code, and a merged report would look healthy
    while being meaningless. The check reads the `.meta.json` sidecars written
    next to each report.
    """
    from wake_sol import coverage as cov

    try:
        n = cov.merge_lcov(list(inputs), out, force=force)
    except ValueError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"merged {len(inputs)} report(s) into {out} ({n} file(s))")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
