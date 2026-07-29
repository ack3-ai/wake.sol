"""Command-line interface for wake.sol."""

from __future__ import annotations

import sys
from pathlib import Path

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
    server_args = base_args + ["-p", "no:wake_sol", "-s"]

    logs_dir = Path.cwd() / ".wake-sol" / "logs" / "testing"

    sys.exit(
        pytest.main(
            server_args,
            plugins=[
                PytestPluginMultiprocessServer(
                    proc_count, worker_seeds, dist, worker_args, logs_dir,
                    attach=attach, attach_first=attach_first,
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
    click.option("--no-facts", is_flag=True,
                 help="Ignore *.facts.json sidecars. Reserved — sidecars are "
                      "not consumed yet, so this is currently a no-op."),
    click.option("--strict", is_flag=True,
                 help="Treat any per-program refusal as a hard failure."),
    click.option("--verify", is_flag=True,
                 help="Run the offline verification gate (deferred in v1)."),
    click.option("--allow-unverified", is_flag=True,
                 help="Emit even without sample bytes (stamps verified=false)."),
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


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
