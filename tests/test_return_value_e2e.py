"""End-to-end return value against a **real** SBF program.

`programs/native-adder` implements `add(a, b) -> u64` by writing the sum to the
transaction return data via `set_return_data`. We generate pytypes from a hand
IDL (with `returns: u64`), deploy the `.so`, build the instruction through the
generated builder, and assert `result.return_value` decodes to the sum — the full
gen → emit → runtime-decode path exercised for real (not crafted bytes).
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path

import pytest

from wake_sol import Account, Instruction, LiteSVM, Pubkey, u64
from wake_sol._gen import run_gen

SO = Path(__file__).parent.parent / "programs/native-adder/target/deploy/native_adder.so"
PROGRAM_ID = Pubkey(bytes([0xAD] * 32))
ADDR = str(PROGRAM_ID)
DISC = [10, 20, 30, 40, 50, 60, 70, 80]   # arbitrary 8-byte discriminator; program ignores it

_IDL = {
    "address": ADDR,
    "metadata": {"name": "adder", "version": "0.0.0"},
    "instructions": [{
        "name": "add",
        "discriminator": DISC,
        "accounts": [],
        "args": [{"name": "a", "type": "u64"}, {"name": "b", "type": "u64"}],
        "returns": "u64",
    }],
}

pytestmark = pytest.mark.skipif(
    not SO.exists(),
    reason="native-adder .so not built (run: cd programs/native-adder && cargo build-sbf)",
)


@pytest.fixture(scope="module")
def adder():
    """Generate + import the adder pytypes package (self-registers on import)."""
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
        yield pytypes.adder
    finally:
        sys.path.remove(str(root))
        for n in [n for n in sys.modules if n == "pytypes" or n.startswith("pytypes.")]:
            del sys.modules[n]


def _funded_payer():
    svm = LiteSVM()
    svm.add_program(PROGRAM_ID, SO.read_bytes())
    payer = Account.new(svm)
    svm.airdrop(payer, 1_000_000_000)
    return payer


def test_return_value_decoded_on_simulate(adder):
    payer = _funded_payer()
    res = payer.simulate(adder.Adder.add(5, 37))
    assert res.success, res.logs
    assert res.return_value == 42                       # decoded via IDL returns=u64
    assert res.raw_return_value == (42).to_bytes(8, "little")
    assert str(res.return_program_id) == ADDR


def test_return_value_decoded_on_commit(adder):
    payer = _funded_payer()
    res = payer.tx(adder.Adder.add(1000, 337))        # committed, not simulated
    assert res.success, res.logs
    assert res.return_value == 1337


def test_decode_return_explicit(adder):
    payer = _funded_payer()
    res = payer.simulate(adder.Adder.add(2, 3))
    assert res.decode_return(u64) == 5


def test_wraps_like_u64(adder):
    payer = _funded_payer()
    res = payer.simulate(adder.Adder.add(2**64 - 1, 1))   # wrapping_add -> 0
    assert res.return_value == 0


# --- rendered call trace (the visual we actually care about) -------------------
def test_call_trace_visual_success(adder):
    payer = _funded_payer()
    out = str(payer.simulate(adder.Adder.add(5, 37)).call_trace)
    assert "✓" in out            # per-node success glyph (wake-style)
    assert "CU]" in out          # per-node compute units
    assert "➞ 42" in out         # decoded return value on the ➞ line


def test_call_trace_visual_failure(adder):
    payer = _funded_payer()
    try:
        payer.simulate(Instruction(PROGRAM_ID, [], b"\x00\x00"))   # bad data
        raise AssertionError("expected failure")
    except Exception as e:
        out = str(e.tx.call_trace)
    assert "✗" in out                            # per-node failure glyph
    assert "invalid instruction data" in out     # decoded error, shown once
    # the error appears exactly once (no log/➞ duplication)
    assert out.count("invalid instruction data") == 1
