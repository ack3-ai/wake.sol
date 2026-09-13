"""Source-line coverage of the SBF program under test.

Drives the native-counter program and checks that the reported hit counts are
the real ones: executed statements carry exactly the number of transactions
sent, and the two error paths — which no transaction here takes — carry 0.

Needs the unstripped build alongside the deployed one, which the program's
manifest already asks for; see the skip message for the command.
"""

import json
import time
from pathlib import Path

import pytest

from wake_sol import Account, Instruction, Pubkey, coverage, svm, writable
from wake_sol import _pytest_plugin as _plugin

pytest_plugins = ["pytester"]

_PROGRAM = Path(__file__).parent.parent / "programs/native-counter"
_DEPLOY_SO = _PROGRAM / "target/deploy/native_counter.so"
_DEBUG_SO = _PROGRAM / "target/sbpf-solana-solana/release/native_counter.so"
_SRC = _PROGRAM / "src/lib.rs"

PROGRAM_ID = Pubkey(bytes([0xC1] * 32))

pytestmark = pytest.mark.skipif(
    not _DEBUG_SO.exists(),
    reason="native-counter debug build missing (run: cd programs/native-counter "
           "&& cargo build-sbf --disable-remap-cwd)",
)


def _line_of(needle: str) -> int:
    """The 1-based line of the only statement containing `needle`.

    Asserting on line *numbers* would break every time the program is edited;
    asserting on the statement text keeps the test about behaviour.
    """
    for i, text in enumerate(_SRC.read_text().splitlines(), start=1):
        if needle in text:
            return i
    raise AssertionError(f"{needle!r} not found in {_SRC}")


@pytest.fixture
def collected():
    """Run N increments under coverage and return `(report, N)`.

    Coverage is process-global and armed until disarmed, so it is switched off
    again on the way out — otherwise every later test in the session would run
    with register tracing on.
    """
    n = 3
    coverage.enable()
    coverage.reset()
    try:
        svm.reset()  # rebuild so the program loads with tracing compiled in
        svm.transaction_history = False  # byte-identical txs, sent repeatedly
        svm.add_program_from_file(PROGRAM_ID, _DEPLOY_SO)

        payer = Account.new()
        svm.airdrop(payer, 1_000_000_000)
        counter = Account.new()
        svm.set_account(
            counter,
            lamports=svm.minimum_balance_for_rent_exemption(8),
            data=bytes(8),
            owner=PROGRAM_ID,
        )
        for _ in range(n):
            payer.tx(Instruction(PROGRAM_ID, [writable(counter)], b""))

        assert int.from_bytes(counter.data[:8], "little") == n
        yield coverage.report([_PROGRAM]), n
    finally:
        coverage.disable()
        coverage.reset()
        svm.reset()


def test_program_resolves_to_its_own_source(collected):
    report, _ = collected
    program = next(
        p for p in report["programs"] if p["program_id"] == str(PROGRAM_ID)
    )
    assert program["has_debug_info"], "the sibling debug ELF was not picked up"
    assert not program["text_mismatch"]
    # Some but not all of the program ran: it has untaken error paths.
    assert 0 < program["instructions_covered"] < program["instructions_total"]

    assert str(_SRC) in report["files"], report["files"].keys()


def test_executed_statements_count_transactions(collected):
    """A statement's count is how many times it ran — not how many instructions
    it compiled into, which is the failure mode this fold exists to avoid."""
    report, n = collected
    lines = report["files"][str(_SRC)]

    for statement in (
        "let mut data = counter.try_borrow_mut_data()?",
        "let value = u64::from_le_bytes",
        'msg!("counter incremented to {}", next)',
    ):
        line = _line_of(statement)
        assert lines[line] == n, f"{statement!r} (line {line}) reported {lines[line]}"


def test_untaken_error_paths_report_zero(collected):
    report, _ = collected
    lines = report["files"][str(_SRC)]

    for statement in (
        "return Err(ProgramError::IncorrectProgramId)",
        "return Err(ProgramError::AccountDataTooSmall)",
    ):
        line = _line_of(statement)
        assert lines[line] == 0, f"{statement!r} (line {line}) reported {lines[line]}"


def test_no_foreign_lines_leak_into_the_program(collected):
    """The SBF toolchain records sources relative to each crate, so `borsh`'s
    `src/lib.rs` is spelled exactly like the program's. Nothing beyond the end of
    the file — the visible symptom of that merge — may appear."""
    report, _ = collected
    lines = report["files"][str(_SRC)]
    longest = len(_SRC.read_text().splitlines())
    assert max(lines) <= longest, f"line {max(lines)} > {longest} lines in {_SRC}"


def test_lcov_report_is_well_formed(collected, tmp_path):
    report, n = collected
    out = tmp_path / "coverage.lcov"
    assert coverage.write_lcov(out, [_PROGRAM]) == len(report["files"])

    text = out.read_text()
    records = [b for b in text.split("end_of_record") if b.strip()]
    assert len(records) == len(report["files"])

    record = next(r for r in records if f"SF:{_SRC}" in r)
    da = {
        int(line.split(":")[1].split(",")[0]): int(line.split(",")[1])
        for line in record.splitlines()
        if line.startswith("DA:")
    }
    assert da[_line_of("let value = u64::from_le_bytes")] == n
    assert da[_line_of("return Err(ProgramError::IncorrectProgramId)")] == 0

    # LF/LH must agree with the DA records, or genhtml reports a bogus rate.
    assert f"LF:{len(da)}" in record
    assert f"LH:{sum(1 for c in da.values() if c)}" in record


def test_disabled_by_default():
    """Tracing costs real time per instruction, so it must never be on unless
    asked for."""
    assert not coverage.enabled()


def _lcov(path: Path, counts: dict, *, program="P1", digest="a" * 64) -> Path:
    """An LCOV report plus the build-identity sidecar, as `write_lcov` emits."""
    path.write_text(coverage.format_lcov({"/src/x.rs": counts}))
    path.with_name(path.name + ".meta.json").write_text(
        json.dumps({"programs": {program: {"text_sha256": digest,
                                           "instructions_total": 10}}})
    )
    return path


def test_merge_sums_line_counts(tmp_path) -> None:
    """Counts add. They are per-line and not per-instruction for a reason: the
    `max` fold is only valid inside one execution context, and two runs are
    disjoint events."""
    a = _lcov(tmp_path / "a.lcov", {10: 3, 11: 0, 12: 1})
    b = _lcov(tmp_path / "b.lcov", {10: 0, 11: 5, 13: 2})

    out = tmp_path / "m.lcov"
    assert coverage.merge_lcov([a, b], out) == 1

    merged = coverage.parse_lcov(out.read_text())["/src/x.rs"]
    assert merged == {10: 3, 11: 5, 12: 1, 13: 2}

    text = out.read_text()
    assert "LF:4" in text and "LH:4" in text


def test_merge_keeps_a_zero_a_zero(tmp_path) -> None:
    """A line no run reached must stay 0 — that is the whole signal."""
    a = _lcov(tmp_path / "a.lcov", {10: 1, 11: 0})
    b = _lcov(tmp_path / "b.lcov", {10: 2, 11: 0})
    out = tmp_path / "m.lcov"
    coverage.merge_lcov([a, b], out)
    assert coverage.parse_lcov(out.read_text())["/src/x.rs"] == {10: 3, 11: 0}


def test_merge_refuses_different_builds(tmp_path) -> None:
    """Two builds of one program have incomparable line numbers, and a merged
    report would look perfectly healthy while meaning nothing."""
    a = _lcov(tmp_path / "a.lcov", {10: 1}, digest="a" * 64)
    b = _lcov(tmp_path / "b.lcov", {10: 1}, digest="b" * 64)

    with pytest.raises(ValueError, match="different builds"):
        coverage.merge_lcov([a, b], tmp_path / "m.lcov")

    # Same program, same build: fine.
    c = _lcov(tmp_path / "c.lcov", {10: 1}, digest="a" * 64)
    assert coverage.merge_lcov([a, c], tmp_path / "ok.lcov") == 1


def test_merge_force_overrides_the_build_check(tmp_path) -> None:
    a = _lcov(tmp_path / "a.lcov", {10: 1}, digest="a" * 64)
    b = _lcov(tmp_path / "b.lcov", {10: 2}, digest="b" * 64)
    out = tmp_path / "m.lcov"
    assert coverage.merge_lcov([a, b], out, force=True) == 1
    assert coverage.parse_lcov(out.read_text())["/src/x.rs"] == {10: 3}


def test_merge_without_sidecars_still_works(tmp_path) -> None:
    """A report from another tool has no sidecar; merge it rather than refuse."""
    a = tmp_path / "a.lcov"
    a.write_text(coverage.format_lcov({"/src/x.rs": {10: 1}}))
    b = tmp_path / "b.lcov"
    b.write_text(coverage.format_lcov({"/src/x.rs": {10: 2}}))
    out = tmp_path / "m.lcov"
    assert coverage.merge_lcov([a, b], out) == 1
    assert coverage.parse_lcov(out.read_text())["/src/x.rs"] == {10: 3}


def test_merged_report_carries_identity_forward(tmp_path) -> None:
    """So a merge of merges is still checked."""
    a = _lcov(tmp_path / "a.lcov", {10: 1})
    out = tmp_path / "m.lcov"
    coverage.merge_lcov([a], out)
    meta = json.loads((tmp_path / "m.lcov.meta.json").read_text())
    assert meta["programs"]["P1"]["text_sha256"] == "a" * 64


def test_parse_lcov_ignores_records_we_do_not_emit(tmp_path) -> None:
    """Branch and function records from another tool must not break parsing."""
    text = (
        "TN:\nSF:/src/x.rs\nFN:10,foo\nFNDA:2,foo\nDA:10,2\n"
        "BRDA:10,0,0,1\nLF:1\nLH:1\nend_of_record\n"
    )
    assert coverage.parse_lcov(text) == {"/src/x.rs": {10: 2}}


def test_cov_does_not_swallow_a_following_path(pytester: pytest.Pytester) -> None:
    """`--cov tests/foo.py` must run that file, not error and not run everything.

    The count is optional, so argparse offers the next token to `--cov` first —
    and the most natural invocation of this feature puts a test path there. A
    non-numeric value is recognised and handed back to pytest as a path.
    """
    pytester.makepyfile(
        test_wanted="def test_wanted(): pass",
        test_other="def test_other(): pass",
    )
    res = pytester.runpytest_subprocess("--cov", "test_wanted.py")
    res.assert_outcomes(passed=1)           # not 2: the other file is not collected
    res.stdout.fnmatch_lines(["*Coverage:*"])  # and coverage is still on


def test_cov_still_takes_a_count(pytester: pytest.Pytester) -> None:
    """The digit form keeps working, and is not mistaken for a path."""
    config = pytester.parseconfig("--cov", "2")
    assert _plugin.coverage_proc_count(config) == 2
    assert config.getoption("file_or_dir") == []


def test_live_sync_defaults_to_five_seconds(pytester: pytest.Pytester) -> None:
    """A report refreshes while the run is in progress unless asked otherwise.

    An export costs ~4ms, so 5s is ~0.08% — cheap enough that stale coverage in
    an editor is the worse default.
    """
    config = pytester.parseconfig("--coverage-report", "c.lcov")
    assert config.getoption("--coverage-sync") == 5.0
    assert _plugin.coverage_sync_interval(config) == 5.0


def test_live_sync_can_be_turned_off(pytester: pytest.Pytester) -> None:
    config = pytester.parseconfig("--coverage-report", "c.lcov", "--coverage-sync", "0")
    assert _plugin.coverage_sync_interval(config) == 0.0
    assert _plugin.start_live_coverage(config, lambda _d: None) is None


def test_live_exporter_warms_before_starting(tmp_path) -> None:
    """`start()` exports once up front.

    Not cosmetic: the first export parses the program's DWARF and walks the
    project for crate roots, and paying that while transactions are running cost
    1194ms of GIL contention instead of ~1ms. Warming it while nothing competes
    is what makes every later export cheap.
    """
    calls = []
    exporter = coverage.LiveExporter(0.05, lambda data: calls.append(data))
    exporter.start()
    try:
        assert len(calls) == 1, "no warm-up export"
    finally:
        exporter.stop(final=False)


def test_live_exporter_fires_on_the_interval(tmp_path) -> None:
    calls = []
    exporter = coverage.LiveExporter(0.05, lambda data: calls.append(data))
    exporter.start()
    try:
        deadline = time.time() + 0.6
        while time.time() < deadline and len(calls) < 4:
            time.sleep(0.02)
    finally:
        exporter.stop(final=False)
    assert len(calls) >= 4, f"only {len(calls)} exports in 0.6s at a 0.05s interval"


def test_live_exporter_survives_a_failing_export() -> None:
    """A coverage report must never be the reason a test run dies."""
    calls = []

    def boom(_data):
        calls.append(1)
        raise RuntimeError("export failed")

    exporter = coverage.LiveExporter(0.05, boom)
    exporter.start()  # warm-up raises inside export_once
    try:
        deadline = time.time() + 0.4
        while time.time() < deadline and len(calls) < 3:
            time.sleep(0.02)
    finally:
        exporter.stop(final=False)
    assert len(calls) >= 3, "the thread died on the first failing export"


def test_live_exporter_stop_is_prompt() -> None:
    """`stop` must not wait out the remaining interval at the end of a run."""
    exporter = coverage.LiveExporter(30.0, lambda _data: None)
    exporter.start()
    t0 = time.time()
    exporter.stop(final=False)
    assert time.time() - t0 < 1.0


def test_roots_are_memoized() -> None:
    """Root resolution globs the filesystem, which drops the GIL on every call
    and made a live export 1000x slower. It must happen once."""
    roots = coverage._roots([_PROGRAM])
    assert coverage._roots([_PROGRAM]) is roots


def test_cov_is_an_alias_for_coverage(pytester: pytest.Pytester) -> None:
    """``--cov`` reaches the same option as ``--coverage``."""
    pytester.makepyfile("def test_nothing(): pass")
    for flag in ("--coverage", "--cov"):
        res = pytester.runpytest_subprocess(flag)
        res.assert_outcomes(passed=1)
        res.stdout.fnmatch_lines(["*Coverage:*"])


def test_cov_alias_yields_to_pytest_cov(pytester: pytest.Pytester) -> None:
    """When pytest-cov is importable it keeps ``--cov``, and we must not claim it.

    Two plugins registering one option name aborts the entire run, so the check
    has to happen before registration rather than being caught afterwards. The
    stub only has to be *importable* — that is all `_cov_alias_free` inspects.
    """
    # `pytester.popen` injects its cwd into the child's PYTHONPATH, so a package
    # dropped here is importable in the subprocess with no env plumbing.
    (pytester.path / "pytest_cov").mkdir()
    (pytester.path / "pytest_cov" / "__init__.py").write_text("")
    pytester.makepyfile("def test_nothing(): pass")

    res = pytester.runpytest_subprocess("--cov")
    assert res.ret != 0
    assert "unrecognized arguments: --cov" in res.stderr.str() + res.stdout.str()

    # `--coverage` is unaffected, so a project can run both plugins.
    res = pytester.runpytest_subprocess("--coverage")
    res.assert_outcomes(passed=1)
