"""End-to-end events against a real SBF program (`programs/native-emitter`).

`ping(x)` emits the same `{value, doubled}` payload two ways: `emit!` (a
`Program data:` log) and `emit_cpi!` (a self-CPI tagged with Anchor's event tag).
We generate pytypes with an events[] table, deploy the `.so`, and assert both
events decode, appear on the node, render as `⚡`, and — for `emit_cpi!` — that
the self-CPI is hoisted (Option A) rather than shown as a child call.
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path

import pytest

from wake_sol import Account, LiteSVM, Pubkey
from wake_sol._gen import run_gen

SO = Path(__file__).parent.parent / "programs/native-emitter/target/deploy/native_emitter.so"
PROGRAM_ID = Pubkey(bytes([0xEE] * 32))
ADDR = str(PROGRAM_ID)
_STRUCT = {"kind": "struct", "fields": [
    {"name": "value", "type": "u64"}, {"name": "doubled", "type": "u64"}]}
_IDL = {
    "address": ADDR,
    "metadata": {"name": "emitter", "version": "0.0.0"},
    "instructions": [{
        "name": "ping",
        "discriminator": [9, 9, 9, 9, 9, 9, 9, 9],
        "accounts": [
            {"name": "event_authority", "signer": False, "writable": False},
            {"name": "self_program", "signer": False, "writable": False},
        ],
        "args": [{"name": "x", "type": "u64"}],
    }],
    "events": [
        {"name": "Logged", "discriminator": [1, 1, 1, 1, 1, 1, 1, 1]},  # emit!
        {"name": "Cpied", "discriminator": [2, 2, 2, 2, 2, 2, 2, 2]},   # emit_cpi!
    ],
    "types": [{"name": "Logged", "type": _STRUCT}, {"name": "Cpied", "type": _STRUCT}],
}

pytestmark = pytest.mark.skipif(
    not SO.exists(),
    reason="native-emitter .so not built (cd programs/native-emitter && cargo build-sbf)",
)


@pytest.fixture(scope="module")
def emitter():
    root = Path(tempfile.mkdtemp())
    idl_dir = root / "idl"
    idl_dir.mkdir()
    (idl_dir / f"{ADDR}.json").write_text(json.dumps(_IDL))
    assert run_gen(target_idls=(str(idl_dir),), dep_idls=("/nonexistent",),
                   out=str(root / "pytypes")) == 0
    sys.path.insert(0, str(root))
    for n in [n for n in sys.modules if n == "pytypes" or n.startswith("pytypes.")]:
        del sys.modules[n]
    pytypes = importlib.import_module("pytypes")
    try:
        yield pytypes.emitter
    finally:
        sys.path.remove(str(root))
        for n in [n for n in sys.modules if n == "pytypes" or n.startswith("pytypes.")]:
            del sys.modules[n]


def _run(emitter, x=7):
    svm = LiteSVM()
    svm.add_program(PROGRAM_ID, SO.read_bytes())
    payer = Account.new(svm)
    svm.airdrop(payer, 1_000_000_000)
    ev_auth = Account.new(svm)
    svm.airdrop(ev_auth, 1_000_000)   # the self-CPI references it → must exist
    return payer.simulate(
        emitter.Emitter.ping(x, event_authority=ev_auth, self_program=PROGRAM_ID))


def test_both_events_decoded(emitter):
    res = _run(emitter, 7)
    assert res.success, res.logs
    # flat roll-up: emit! `Logged` + hoisted emit_cpi! `Cpied`
    by_kind = {type(e).__name__: (e.value, e.doubled) for e in res.events}
    assert by_kind == {"Logged": (7, 14), "Cpied": (7, 14)}


def test_events_on_node_and_cpi_hoisted(emitter):
    res = _run(emitter, 5)
    root = res.call_trace[0]
    assert {type(e).__name__ for e in root.events} == {"Logged", "Cpied"}
    # Option A: the emit_cpi! self-CPI is folded into an event, not a child call.
    assert len(root.inner) == 0


def test_events_render(emitter):
    out = str(_run(emitter, 7).call_trace)
    assert "⚡ Logged(value=7, doubled=14)" in out
    assert "⚡ Cpied(value=7, doubled=14)" in out


def test_unknown_event_is_raw(emitter):
    # deploy the same .so at a DIFFERENT id with no generated pytypes → events
    # can't be attributed to a program table → UnknownEvent (raw), never guessed.
    from wake_sol import Instruction
    other = Pubkey(bytes([0xAB] * 32))
    svm = LiteSVM()
    svm.add_program(other, SO.read_bytes())
    payer = Account.new(svm)
    svm.airdrop(payer, 1_000_000_000)
    ev_auth = Account.new(svm)
    svm.airdrop(ev_auth, 1_000_000)
    data = bytes([9] * 8) + (7).to_bytes(8, "little")
    from wake_sol._native import AccountMeta
    ix = Instruction(other, [
        AccountMeta(ev_auth.pubkey, False, False),
        AccountMeta(other, False, False),   # self program account for the CPI
    ], data)
    res = payer.simulate(ix)
    assert res.success, res.logs
    from wake_sol import ReturnDataError  # noqa: F401  (import sanity)
    from wake_sol._interface import UnknownEvent
    assert res.events and all(isinstance(e, UnknownEvent) for e in res.events)
