"""Network-free tests for mainnet forking (design/forking-spec/).

These exercise the fork machinery in **offline** mode against a hand-seeded
snapshot directory — no RPC — so they run in CI. They cover: offline hydration
from the snapshot, owner-scoped exclude (audited-program state blanked),
local-wins precedence, the negative (confirmed-absent) cache, and the hard
offline-miss error. Real-mainnet forking (fetch a live program + run a tx) is a
manual / env-gated exercise, not part of the hermetic suite.
"""

import base64
import json

import pytest

from solana_fuzzer import Account, LiteSVM, Pubkey

SYSTEM = Pubkey(bytes(32))                  # 32 zero bytes = System program id

PROGRAM = Pubkey(bytes([2] * 32))           # excluded (the "audit target")
FORKED = Pubkey(bytes([1] * 32))            # a plain forked account (System-owned)
PSTATE = Pubkey(bytes([3] * 32))            # owned by PROGRAM -> must be blanked
LOCAL = Pubkey(bytes([4] * 32))             # set_account'd locally -> local-wins
GONE = Pubkey(bytes([5] * 32))              # confirmed-absent on chain
MISSING = Pubkey(bytes([9] * 32))           # not in the snapshot at all


def _seed(cache, pubkey, *, lamports, owner, data=b"", executable=False, rent_epoch=0):
    obj = {
        "lamports": lamports,
        "owner": str(owner),
        "executable": executable,
        "rent_epoch": rent_epoch,
        "data": base64.b64encode(data).decode(),
    }
    (cache / f"{pubkey}.json").write_text(json.dumps(obj))


def _seed_absent(cache, pubkey):
    (cache / f"{pubkey}.json").write_text(json.dumps({"absent": True}))


@pytest.fixture
def forked(tmp_path):
    cache = tmp_path / "fork-cache"
    cache.mkdir()
    _seed(cache, FORKED, lamports=42, owner=SYSTEM, data=b"hi")
    _seed(cache, PSTATE, lamports=100, owner=PROGRAM, data=b"state")
    _seed(cache, LOCAL, lamports=999, owner=SYSTEM)
    _seed_absent(cache, GONE)
    svm = LiteSVM()
    svm.fork(cache=str(cache), offline=True, exclude=[PROGRAM])
    return svm


def test_offline_hydrates_from_snapshot(forked):
    acc = Account(FORKED, forked)
    assert acc.exists
    assert acc.lamports == 42
    assert acc.data == b"hi"
    assert acc.owner == SYSTEM


def test_exclude_blanks_owned_state(forked):
    # PSTATE is in the snapshot but owned by the excluded program, so it is not
    # hydrated — the audited program starts from scratch.
    acc = Account(PSTATE, forked)
    assert not acc.exists
    with pytest.raises(LookupError):
        _ = acc.lamports


def test_local_wins_over_fork(forked):
    forked.set_account(LOCAL, lamports=555)
    acc = Account(LOCAL, forked)
    assert acc.lamports == 555          # the local value, never the snapshot's 999


def test_confirmed_absent_stays_empty(forked):
    # A negatively-cached address reads as empty without error (and without
    # a network call, which offline mode could not make anyway).
    assert not Account(GONE, forked).exists


def test_offline_miss_is_hard_error(forked):
    # An address absent from the snapshot cannot be resolved offline: hard error,
    # never a silent empty decode.
    with pytest.raises(RuntimeError):
        _ = Account(MISSING, forked).exists


def test_unfork_disables_hydration(forked):
    # With the (offline) fork on, a not-in-snapshot address is a hard miss; after
    # unfork() there is no fork, so it's simply absent — no hydration, no error.
    forked.unfork()
    assert not Account(MISSING, forked).exists


def test_without_fork_reads_are_unchanged():
    # No fork configured: a missing account raises as before, no hydration.
    svm = LiteSVM()
    with pytest.raises(LookupError):
        _ = Account(FORKED, svm).lamports


# --- fork_programs / forked_accounts --------------------------------------- #
#
# litesvm *compiles* an executable account on set_account, so a program snapshot
# must hold real bytecode. We seed a genuinely-compiled loader-v2 program (raw
# ELF in the account itself, no separate programdata). The upgradeable
# program->programdata expansion path is exercised live against mainnet (JUP) in
# the manual verification, per this module's header.

import pathlib

BPFLOADER2 = Pubkey("BPFLoader2111111111111111111111111111111111")
V2PROG = Pubkey(bytes([8] * 32))
_PROGRAM_SO = (
    pathlib.Path(__file__).parent.parent
    / "programs/native-counter/target/deploy/native_counter.so"
)


@pytest.fixture
def progs(tmp_path):
    cache = tmp_path / "fork-cache"
    cache.mkdir()
    _seed(
        cache, V2PROG, lamports=1, owner=BPFLOADER2,
        data=_PROGRAM_SO.read_bytes(), executable=True,
    )
    _seed(cache, FORKED, lamports=42, owner=SYSTEM, data=b"hi")   # plain, non-executable
    svm = LiteSVM()
    svm.fork(cache=str(cache), offline=True)
    return svm


def test_fork_programs_pins_program(progs):
    assert progs.fork_programs(V2PROG) == 1
    assert Account(V2PROG, progs).executable
    execs = [str(a.pubkey) for a in progs.forked_accounts() if a.executable]
    assert execs == [str(V2PROG)]


def test_fork_programs_rejects_non_program(progs):
    # A non-executable id fails loudly rather than silently under-forking.
    with pytest.raises(RuntimeError):
        progs.fork_programs(FORKED)


def test_fork_programs_requires_fork():
    with pytest.raises(RuntimeError):
        LiteSVM().fork_programs(V2PROG)


def test_fork_programs_local_wins_counts_but_not_sourced(progs):
    # A locally-provided program (your own build) satisfies fork_programs without
    # being re-fetched or listed among fork-sourced accounts.
    progs.set_account(
        V2PROG, lamports=5, owner=BPFLOADER2,
        data=_PROGRAM_SO.read_bytes(), executable=True,
    )
    assert progs.fork_programs(V2PROG) == 1
    assert progs.forked_accounts() == []             # nothing fork-sourced


def test_forked_accounts_lists_present_excludes_blanked_and_absent(forked):
    # Drives the lazy-hydration path (no executables needed): a present account is
    # listed; an owner-blanked (excluded) one and a confirmed-absent one are not.
    assert forked.forked_accounts() == []            # nothing hydrated yet
    assert Account(FORKED, forked).exists            # present -> listed
    assert not Account(PSTATE, forked).exists        # excluded-owner -> blanked
    assert not Account(GONE, forked).exists          # confirmed-absent
    addrs = [str(a.pubkey) for a in forked.forked_accounts()]
    assert addrs == [str(FORKED)]


def test_forked_accounts_excludes_local(forked):
    forked.set_account(LOCAL, lamports=555)
    assert Account(LOCAL, forked).lamports == 555    # local-wins, not fork-sourced
    assert forked.forked_accounts() == []
