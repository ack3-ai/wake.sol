"""Return-value decoding: `gen` emits the IDL `returns` type onto the
instruction meta, the runtime decodes return-data bytes to it, and the low-level
`decode_return_value` resolver raises (never guesses) when it can't.

No on-chain program here sets return data, so the decode/resolution path is
driven directly with crafted bytes (the same path `TransactionResult.return_value`
uses); the Rust wiring is smoke-checked via the no-return airdrop path.
"""

from __future__ import annotations

import importlib
import json
import sys

import pytest

from wake_sol import LiteSVM, Pubkey, ReturnDataError, u64
from wake_sol._gen import run_gen
from wake_sol._interface import decode_return_value

ADDR = "Ret1111111111111111111111111111111111111111"  # base58, unique to this test
GET_ANSWER = bytes([1, 2, 3, 4, 5, 6, 7, 8])           # -> returns u64
GET_PAIR = bytes([11, 12, 13, 14, 15, 16, 17, 18])     # -> returns Pair {a:u32,b:u64}
NOOP = bytes([21, 22, 23, 24, 25, 26, 27, 28])         # -> no returns

_IDL = {
    "address": ADDR,
    "metadata": {"name": "retprog", "version": "0.0.0"},
    "instructions": [
        {"name": "get_answer", "discriminator": list(GET_ANSWER),
         "accounts": [], "args": [], "returns": "u64"},
        {"name": "get_pair", "discriminator": list(GET_PAIR),
         "accounts": [], "args": [], "returns": {"defined": {"name": "Pair"}}},
        {"name": "noop", "discriminator": list(NOOP), "accounts": [], "args": []},
    ],
    "types": [
        {"name": "Pair", "type": {"kind": "struct", "fields": [
            {"name": "a", "type": "u32"},
            {"name": "b", "type": "u64"},
        ]}},
    ],
}


@pytest.fixture(scope="module")
def gen(tmp_path_factory):
    """Generate + import the retprog package (self-registers on import)."""
    root = tmp_path_factory.mktemp("retroot")
    idl_dir = root / "idl"
    idl_dir.mkdir()
    (idl_dir / f"{ADDR}.json").write_text(json.dumps(_IDL))
    assert run_gen(target_idls=(str(idl_dir),), dep_idls=("/nonexistent",),
                   out=str(root / "pytypes")) == 0
    sys.path.insert(0, str(root))
    for name in [n for n in sys.modules if n == "pytypes" or n.startswith("pytypes.")]:
        del sys.modules[name]
    importlib.import_module("pytypes")   # registers retprog into REGISTRY
    try:
        yield
    finally:
        sys.path.remove(str(root))
        for name in [n for n in sys.modules
                     if n == "pytypes" or n.startswith("pytypes.")]:
            del sys.modules[name]


# --- the emitter carries `returns` onto the meta ------------------------------
def test_gen_emits_returns_type(tmp_path):
    idl_dir = tmp_path / "idl"
    idl_dir.mkdir()
    (idl_dir / f"{ADDR}.json").write_text(json.dumps(_IDL))
    out = tmp_path / "pytypes"
    assert run_gen(target_idls=(str(idl_dir),), dep_idls=("/nonexistent",),
                   out=str(out)) == 0
    src = "\n".join(p.read_text() for p in out.rglob("*.py"))
    assert "returns_type=u64" in src          # scalar return
    assert "returns_type=Pair" in src         # defined-type return
    # only the two return-bearing instructions carry it
    assert src.count("returns_type=") == 2


# --- the resolver used by the low-level return_value path ----------------------
def test_decode_scalar_return(gen):
    assert decode_return_value(ADDR, GET_ANSWER, (42).to_bytes(8, "little")) == 42


def test_decode_defined_struct_return(gen):
    raw = (7).to_bytes(4, "little") + (99).to_bytes(8, "little")
    val = decode_return_value(ADDR, GET_PAIR, raw)
    assert (val.a, val.b) == (7, 99)


def test_bad_bytes_raise(gen):
    # u64 needs 8 bytes; 3 is a strict-decode failure -> raise, never guess.
    with pytest.raises(ReturnDataError):
        decode_return_value(ADDR, GET_ANSWER, b"\x01\x02\x03")


def test_trailing_bytes_raise(gen):
    with pytest.raises(ReturnDataError):
        decode_return_value(ADDR, GET_ANSWER, (42).to_bytes(8, "little") + b"\x00")


def test_no_return_type_raises(gen):
    with pytest.raises(ReturnDataError, match="no return type"):
        decode_return_value(ADDR, NOOP, (42).to_bytes(8, "little"))


def test_unregistered_program_raises(gen):
    unregistered = "Unreg11111111111111111111111111111111111111"  # not a built-in
    with pytest.raises(ReturnDataError, match="no generated interface"):
        decode_return_value(unregistered, GET_ANSWER, (42).to_bytes(8, "little"))


def test_unattributable_raises(gen):
    # program registered but the return data couldn't be tied to an instruction
    with pytest.raises(ReturnDataError, match="attributed"):
        decode_return_value(ADDR, None, (42).to_bytes(8, "little"))


# --- explicit decode_return_as (the .decode_return(T) hatch) -------------------
def test_decode_return_as_explicit(gen):
    from wake_sol._interface import decode_return_as
    assert decode_return_as(u64, (7).to_bytes(8, "little")) == 7
    with pytest.raises(ReturnDataError):
        decode_return_as(u64, b"\x01")            # too short
    with pytest.raises(ReturnDataError):
        decode_return_as(u64, None)               # no data


# --- Rust wiring on the no-return path ----------------------------------------
def test_no_return_data_is_none():
    svm = LiteSVM()
    res = svm.airdrop(Pubkey(7), 1_000_000_000)
    assert res.success
    assert res.raw_return_value is None
    assert res.return_program_id is None
    assert res.return_value is None         # no data -> None, not a raise


def test_decode_return_on_no_data_raises():
    svm = LiteSVM()
    res = svm.airdrop(Pubkey(8), 1_000_000_000)
    with pytest.raises(ReturnDataError):
        res.decode_return(u64)
