"""Command-line interface for solana-fuzzer."""

from __future__ import annotations

import sys
from pathlib import Path

import click


@click.group()
def cli() -> None:
    """solana-fuzzer — a Solana program testing and fuzzing harness."""


@cli.command(context_settings=dict(ignore_unknown_options=True))
@click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
def test(pytest_args: tuple[str, ...]) -> None:
    """Run tests with pytest.

    The solana_fuzzer pytest plugin (auto-loaded) resets the global SVM and
    reseeds `random` before each test. Pass `--seed <hex>` for a fixed base
    seed; all arguments are forwarded to pytest.
    """
    import pytest

    sys.exit(pytest.main(list(pytest_args)))


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
    click.option("--no-facts", is_flag=True, help="Ignore *.facts.json sidecars."),
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
        from solana_fuzzer._gen import run_gen

        ctx.exit(run_gen(**opts))


@gen.command("run")
@click.pass_context
def gen_run(ctx: click.Context) -> None:
    """Generate the package (the default action)."""
    from solana_fuzzer._gen import run_gen

    ctx.exit(run_gen(**ctx.obj))


@gen.command("check")
@click.pass_context
def gen_check(ctx: click.Context) -> None:
    """Alias for `gen --check`: regenerate and diff against --out."""
    from solana_fuzzer._gen import run_gen

    opts = dict(ctx.obj)
    opts["check"] = True
    ctx.exit(run_gen(**opts))


@gen.command("list")
@click.pass_context
def gen_list(ctx: click.Context) -> None:
    """Print the discovered {address: idl_path, source_root} table; no codegen."""
    from solana_fuzzer._gen.run import _discover

    opts = ctx.obj
    discovered = _discover(opts["target_idls"], opts["dep_idls"], opts["verbose"])
    for addr in sorted(discovered):
        _idl, path, src = discovered[addr]
        click.echo(f"{addr}\t{path}\t({src})")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
