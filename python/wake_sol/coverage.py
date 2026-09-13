"""Source-line coverage for the SBF programs under test.

Coverage answers the auditor's question that a passing suite does not: *which
lines of the program did these tests actually execute?* It is collected from the
SVM itself — agave's VM records the program counter of every instruction it
runs, and those are mapped back to `file:line` through the program's DWARF. See
:mod:`wake_sol._native` (``src/coverage.rs``) for the mechanism and for why line
hit counts are folded with ``max`` rather than summed.

Usage is normally just the pytest flag::

    wake-sol test --coverage
    wake-sol test --coverage --coverage-report coverage.lcov

Driving it by hand takes two steps, because register tracing is compiled into a
program when it loads and so has to be armed before any SVM exists::

    from wake_sol import coverage, svm

    coverage.enable()
    svm.reset()          # rebuild the SVM with tracing on
    ...                  # deploy, run transactions
    print(coverage.summary())
    coverage.write_lcov("coverage.lcov")

Building the program under test
-------------------------------

The program has to carry debug info, which ``cargo build-sbf`` omits by
default. In the program's ``Cargo.toml``::

    [profile.release]
    debug = true
    lto = "off"
    opt-level = 0   # optional; see "Optimization" below

That build writes two artifacts: the stripped ``target/deploy/<name>.so`` you
deploy, and the full ``target/sbpf-solana-solana/release/<name>.so`` that holds
the DWARF. Deploying with ``svm.add_program_from_file`` finds the second one
automatically. If your layout differs, register it yourself::

    coverage.add_debug_elf(PROGRAM_ID, "path/to/unstripped.so")

You cannot simply deploy the unstripped build: with tracing on, agave's loader
walks its symbol table and rejects the program. ``has_debug_info`` in
:func:`report` tells you whether a program resolved.

Optimization
------------

At ``opt-level = 0`` line attribution is close to exact. At the default
``opt-level = 2`` the compiler reorders and merges code, so instructions from
one line scatter and some lines vanish entirely — coverage is still useful, but
treat individual counts as approximate. The trade is real either way: ``O0`` code
is not the code you ship, and its compute-unit costs differ substantially.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from os import fspath
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

from . import _native

__all__ = [
    "add_debug_elf",
    "disable",
    "enable",
    "enabled",
    "format_lcov",
    "LiveExporter",
    "merge_lcov",
    "merge_reports",
    "parse_lcov",
    "report",
    "reset",
    "summary",
    "write_lcov",
]

#: ``{path: {line: hits}}`` — every line the program compiled code onto, with a
#: count of 0 for those never executed.
FileLines = Dict[str, Dict[int, int]]


def enable() -> None:
    """Arm coverage for SVMs built from here on.

    Register tracing is baked into a program's executable when it loads, so this
    has no effect on an SVM that already exists — call ``svm.reset()`` after it,
    or use ``--coverage``, which arms coverage before the first test resets the
    SVM anyway.
    """
    _native.coverage_enable()


def disable() -> None:
    """Disarm coverage.

    Takes effect on the next ``svm.reset()``, which builds an untraced SVM.
    Counts already collected survive until :func:`reset`.
    """
    _native.coverage_disable()


def enabled() -> bool:
    """Is coverage armed?"""
    return _native.coverage_enabled()


def reset() -> None:
    """Drop all counts. Registered debug ELFs are kept."""
    _native.coverage_reset()


def add_debug_elf(program_id, path: Union[str, Path]) -> None:
    """Register the unstripped build of `program_id` as its debug source.

    Only needed when the ELF is not the sibling of a deployed
    ``target/deploy/*.so``, which ``svm.add_program_from_file`` finds on its own.
    Raises if the file carries no line table — a silent miss here would surface
    much later as an empty report.
    """
    _native.coverage_add_debug_elf(str(program_id), fspath(path))


#: How far below a source root to look for crates. Deep enough for the usual
#: `programs/<name>/Cargo.toml` and `crates/<name>/Cargo.toml` layouts without
#: walking a whole repository.
_CRATE_SEARCH_DEPTH = 3

_PACKAGE_NAME = re.compile(
    r"^\s*\[package\]\s*$.*?^\s*name\s*=\s*[\"']([^\"']+)[\"']",
    re.MULTILINE | re.DOTALL,
)


def _crate_name(manifest: Path) -> Optional[str]:
    """The `[package] name` of a Cargo.toml, as rustc spells it.

    A workspace-only manifest has no `[package]`, so it yields nothing and is
    skipped. Cargo normalizes `-` to `_` for the crate name that ends up in
    DWARF, so do the same.
    """
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _PACKAGE_NAME.search(text)
    return match.group(1).replace("-", "_") if match else None


#: Memoized `_roots` results, keyed by the requested roots.
#:
#: Resolving roots means walking for `Cargo.toml` files and globbing for nested
#: crates — dozens of filesystem calls, each of which drops the GIL and has to
#: win it back. On a live export running beside a transaction loop that cost
#: over a second per export, dwarfing the export itself. The answer depends only
#: on the project layout, which does not change mid-run.
_ROOTS_CACHE: Dict[tuple, List[tuple]] = {}


def _roots(source_roots: Optional[List[Union[str, Path]]]) -> List[tuple]:
    """Pair each source root with the crate compiled from it.

    The crate name is not decoration: the SBF toolchain emits no
    ``DW_AT_comp_dir``, so a program's own sources appear in DWARF as bare
    relative paths like ``src/lib.rs`` — a path that eighteen crates in a
    typical build share. The name is what tells them apart, and it comes from
    the ``Cargo.toml`` governing each root.

    Both the root itself and its enclosing directory are searched, so passing
    either ``mycrate`` or ``mycrate/src`` works; for a workspace, the crates
    below it are picked up too.
    """
    if source_roots is None:
        source_roots = [Path.cwd()]

    cache_key = tuple(str(r) for r in source_roots)
    cached = _ROOTS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    pairs: List[tuple] = []
    seen = set()

    def add(directory: Path, name: Optional[str]) -> None:
        key = (str(directory), name or "")
        if key not in seen:
            seen.add(key)
            pairs.append(key)

    for raw in source_roots:
        root = Path(raw).resolve()
        # The root itself, with no crate attached. This is what confines
        # absolute paths, and it is added unconditionally: if no Cargo.toml is
        # found anywhere near, the alternative is an empty root list, which
        # means "no filtering" and quietly floods the report with every
        # dependency in the registry.
        add(root, None)
        # The manifest governing this root: itself, or the nearest ancestor.
        # A relative DWARF path is relative to the *crate root*, the directory
        # holding Cargo.toml — not to the `src/` the caller probably passed.
        for candidate in (root, *root.parents):
            manifest = candidate / "Cargo.toml"
            if manifest.is_file():
                add(candidate, _crate_name(manifest))
                break
        # Workspaces: every crate underneath is fair game.
        for depth in range(1, _CRATE_SEARCH_DEPTH + 1):
            for manifest in root.glob("/".join(["*"] * depth) + "/Cargo.toml"):
                add(manifest.parent, _crate_name(manifest))

    _ROOTS_CACHE[cache_key] = pairs
    return pairs


def report(source_roots: Optional[List[Union[str, Path]]] = None) -> dict:
    """Full coverage data.

    Returns ``{"files": {path: {line: hits}}, "programs": [...]}``. Each program
    entry carries ``program_id``, ``instructions_covered`` /
    ``instructions_total`` (distinct SBF instructions, useful even with no debug
    info), ``has_debug_info`` and ``text_mismatch``.

    `source_roots` defaults to the current directory; lines outside it — the
    Rust standard library, dependencies — are attributed to the nearest enclosing
    call site inside it, or dropped if there is none.
    """
    return _native.coverage_report(_roots(source_roots))


def write_lcov(
    path: Union[str, Path],
    source_roots: Optional[List[Union[str, Path]]] = None,
) -> int:
    """Write an LCOV report, returning the number of source files in it.

    LCOV is what ``genhtml``, codecov and the editor coverage gutters read::

        genhtml --output-directory htmlcov coverage.lcov

    A ``<path>.meta.json`` sidecar is written alongside, recording which build
    each program's line numbers describe; :func:`merge_lcov` uses it to refuse a
    merge across mismatched builds.

    Collect, format and write happen in a single native call. Doing them as
    separate Python steps yields the GIL between each, and a live export running
    while the main thread is sending transactions then took **1192 ms** instead
    of 4 — see :class:`LiveExporter`. One call also means the report and its
    sidecar describe the same instant.
    """
    return _native.coverage_lcov(fspath(path), _roots(source_roots))


# --------------------------------------------------------------------------- #
# Live export
# --------------------------------------------------------------------------- #

class LiveExporter:
    """Emits the coverage report on an interval, from a background thread.

    Counts accumulate continuously — the collector bumps them after every
    transaction — so a snapshot is meaningful at any moment. What costs
    something is turning counts into lines: the DWARF walk behind
    :func:`report`. That result is cached per program, so repeated exports pay
    it once.

    The thread is a daemon and every export is wrapped: a coverage report must
    never be the reason a test run dies or hangs.
    """

    def __init__(
        self,
        interval: float,
        emit: "Callable[[dict], None]",
        source_roots: Optional[List[Union[str, Path]]] = None,
    ) -> None:
        self._interval = interval
        self._emit = emit
        self._roots = source_roots
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._interval <= 0 or self._thread is not None:
            return
        # Export once up front, before anything competes for the GIL. The first
        # export is the expensive one — it parses the program's DWARF and walks
        # the project for crate roots — and paying that while a transaction loop
        # is running turned a 1ms export into 1194ms of GIL tug-of-war. Warmed
        # here, every later export is cheap, and an editor gets a report to show
        # immediately instead of after the first interval.
        self.export_once()
        self._thread = threading.Thread(
            target=self._loop, name="wake-sol-coverage", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        # `wait` rather than `sleep`: stopping is immediate instead of waiting
        # out the remainder of an interval at the end of a run.
        while not self._stop.wait(self._interval):
            self.export_once()

    def export_once(self) -> None:
        try:
            self._emit(report(self._roots))
        except Exception:
            pass

    def stop(self, *, final: bool = True) -> None:
        """Stop the thread and, by default, emit one last up-to-date report."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if final:
            self.export_once()


# --------------------------------------------------------------------------- #
# Merging
# --------------------------------------------------------------------------- #

def _meta_path(lcov: Path) -> Path:
    return lcov.with_name(lcov.name + ".meta.json")


def _write_meta(lcov: Path, data: dict) -> None:
    meta = {
        p["program_id"]: {
            "text_sha256": p.get("text_sha256", ""),
            "instructions_total": p.get("instructions_total", 0),
        }
        for p in data.get("programs", [])
    }
    try:
        _meta_path(lcov).write_text(json.dumps({"programs": meta}, indent=2) + "\n")
    except OSError:
        pass  # the report itself is written; a missing sidecar only costs the check


def merge_reports(dst: dict, src: dict) -> None:
    """Merge one :func:`report` into `dst`, in place.

    Line counts **sum**. They must not be merged at the instruction level
    instead: a line's count is the maximum over the separate blocks it owns,
    which is right within one execution context because a single pass enters all
    of them — but two processes running different transactions are disjoint
    events that add. Merging raw counters first and folding afterwards would take
    a maximum across processes and undercount.

    Program entries are keyed by id. ``instructions_covered`` is a count of
    distinct instructions, not of executions, so it is combined with ``max``
    rather than summed — a lower bound on the union, since the sets themselves
    are not retained.
    """
    files = dst.setdefault("files", {})
    for path, lines in src.get("files", {}).items():
        into = files.setdefault(path, {})
        for line, count in lines.items():
            line = int(line)
            into[line] = into.get(line, 0) + count

    programs = {p["program_id"]: p for p in dst.setdefault("programs", [])}
    for p in src.get("programs", []):
        known = programs.get(p["program_id"])
        if known is None:
            programs[p["program_id"]] = dict(p)
            continue
        known["instructions_covered"] = max(
            known.get("instructions_covered", 0), p.get("instructions_covered", 0)
        )
        known["lines_resolved"] = max(
            known.get("lines_resolved", 0), p.get("lines_resolved", 0)
        )
        known["has_debug_info"] = known.get("has_debug_info") or p.get("has_debug_info")
        known["text_mismatch"] = known.get("text_mismatch") or p.get("text_mismatch")
    dst["programs"] = sorted(programs.values(), key=lambda p: p["program_id"])


def parse_lcov(text: str) -> Dict[str, Dict[int, int]]:
    """`{path: {line: hits}}` from LCOV text.

    Only ``SF:``/``DA:`` are read. Records this package does not emit — ``BRDA``,
    ``FN`` — are ignored rather than rejected, so a file from another tool merges
    without complaint, losing only the parts we have no counts for.
    """
    out: Dict[str, Dict[int, int]] = {}
    current: Optional[Dict[int, int]] = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            current = out.setdefault(line[3:], {})
        elif line.startswith("DA:") and current is not None:
            number, _, count = line[3:].partition(",")
            try:
                current[int(number)] = current.get(int(number), 0) + int(count.split(",")[0])
            except ValueError:
                continue
        elif line == "end_of_record":
            current = None
    return out


def format_lcov(files: Dict[str, Dict[int, int]]) -> str:
    """LCOV text for `{path: {line: hits}}`, matching what the Rust side emits."""
    out: List[str] = []
    for path in sorted(files):
        lines = files[path]
        out.append("TN:")
        out.append(f"SF:{path}")
        out.extend(f"DA:{n},{lines[n]}" for n in sorted(lines))
        out.append(f"LF:{len(lines)}")
        out.append(f"LH:{sum(1 for c in lines.values() if c)}")
        out.append("end_of_record")
    return "\n".join(out) + ("\n" if out else "")


def merge_lcov(
    paths: List[Union[str, Path]],
    out: Union[str, Path],
    *,
    force: bool = False,
) -> int:
    """Merge LCOV reports into `out`; returns the number of source files.

    Refuses when the inputs describe different builds of the same program —
    their line numbers are not comparable, and a merged report would look
    perfectly healthy while being meaningless. The check reads the
    ``.meta.json`` sidecars; inputs without one cannot be checked and are
    merged with a warning to stderr. `force` downgrades a mismatch to a warning.
    """
    inputs = [Path(fspath(p)) for p in paths]
    _check_builds(inputs, force=force)

    merged: Dict[str, Dict[int, int]] = {}
    for path in inputs:
        for file, lines in parse_lcov(path.read_text()).items():
            into = merged.setdefault(file, {})
            for number, count in lines.items():
                into[number] = into.get(number, 0) + count

    out_path = Path(fspath(out))
    out_path.write_text(format_lcov(merged))

    # Carry the identity forward so a merged report can itself be merged.
    metas = [_read_meta(p) for p in inputs]
    combined: dict = {}
    for meta in metas:
        combined.update(meta)
    if combined:
        _meta_path(out_path).write_text(
            json.dumps({"programs": combined}, indent=2) + "\n"
        )
    return len(merged)


def _read_meta(lcov: Path) -> dict:
    try:
        return json.loads(_meta_path(lcov).read_text()).get("programs", {})
    except (OSError, ValueError):
        return {}


def _check_builds(paths: List[Path], *, force: bool) -> None:
    seen: Dict[str, tuple] = {}
    unchecked = []
    for path in paths:
        meta = _read_meta(path)
        if not meta:
            unchecked.append(path.name)
            continue
        for program_id, info in meta.items():
            digest = info.get("text_sha256") or ""
            if not digest:
                continue
            if program_id in seen and seen[program_id][0] != digest:
                message = (
                    f"{path.name} and {seen[program_id][1]} contain different builds "
                    f"of program {program_id} "
                    f"({digest[:12]}… vs {seen[program_id][0][:12]}…). Their line "
                    f"numbers describe different code and must not be summed."
                )
                if not force:
                    raise ValueError(message + " Pass force=True to merge anyway.")
                print(f"warning: {message}", file=sys.stderr)
            seen.setdefault(program_id, (digest, path.name))
    if unchecked:
        print(
            f"warning: no .meta.json beside {', '.join(unchecked)} — cannot verify "
            f"these came from the same build",
            file=sys.stderr,
        )


def summary(
    source_roots: Optional[List[Union[str, Path]]] = None,
    *,
    data: Optional[dict] = None,
) -> str:
    """A per-file ``hit/total`` table, as printed in the pytest summary.

    `data` renders an already-collected report — the multiprocess server passes
    the merge of its workers', which it cannot re-collect because the counters
    live in the workers' address spaces.
    """
    data = report(source_roots) if data is None else data
    files: FileLines = data["files"]
    programs = data["programs"]

    lines: List[str] = []

    # A program that ran but resolved nothing is the common misconfiguration
    # (built without `debug = true`, or the sidecar never found). Say so
    # explicitly — an empty table alone reads as "no code ran".
    for p in programs:
        if p["text_mismatch"]:
            lines.append(
                f"⚠ {p['program_id']}: registered debug ELF does not match the "
                f"deployed program — skipped"
            )
        elif not p["instructions_covered"]:
            continue  # never ran; nothing to explain
        elif not p["has_debug_info"]:
            # Either it was built without DWARF, or it was deployed from bytes
            # so the unstripped sibling was never found. Indistinguishable from
            # here, so name both.
            lines.append(
                f"⚠ {p['program_id']}: {p['instructions_covered']} instructions "
                f"executed, no line info. Build it with `[profile.release] "
                f"debug = true` and `cargo build-sbf --disable-remap-cwd`; if it "
                f"was deployed with `add_program(...)` rather than "
                f"`add_program_from_file(...)`, point coverage at the unstripped "
                f"ELF with `coverage.add_debug_elf(...)`."
            )
        elif not p["lines_resolved"]:
            lines.append(
                f"⚠ {p['program_id']}: has line info, but none of it is under "
                f"the source roots searched — pass --coverage-source pointing at "
                f"the program's directory."
            )

    if not files:
        lines.append("No source-line coverage collected.")
        return "\n".join(lines)

    width = max(len(_display(f)) for f in files)
    total_hit = total_lines = 0
    for path in sorted(files):
        counts = files[path]
        hit = sum(1 for c in counts.values() if c)
        total = len(counts)
        total_hit += hit
        total_lines += total
        lines.append(f"{_display(path):<{width}}  {hit:>5}/{total:<5} {_pct(hit, total):>6}")

    if len(files) > 1:
        lines.append(f"{'TOTAL':<{width}}  {total_hit:>5}/{total_lines:<5} "
                     f"{_pct(total_hit, total_lines):>6}")
    return "\n".join(lines)


def _display(path: str) -> str:
    """Relative to the cwd when that is shorter; absolute DWARF paths are long."""
    try:
        return str(Path(path).relative_to(Path.cwd()))
    except ValueError:
        return path


def _pct(hit: int, total: int) -> str:
    return f"{100.0 * hit / total:.1f}%" if total else "—"
