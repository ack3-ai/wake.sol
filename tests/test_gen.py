"""Acceptance tests for ``solana-fuzzer gen`` (§7 / §12.4): generate the
pytypes package from a hand-authored Anchor IDL, then prove the generated
modules import, round-trip through the engine, emit byte-identical instruction
data to the canonical hand-written fixture, and regenerate deterministically
(the ``--check`` drift gate). Refusal stubs and address-keying errors are
covered too.
"""

import importlib
import inspect
import json
import sys
from pathlib import Path

import pytest

import fixture_program as fp
from solana_fuzzer import decode_instruction
from solana_fuzzer._codec import Mode, Opt, RefuseToDecode, compile_type, decode, encode
from solana_fuzzer._gen import run_gen
from solana_fuzzer._gen.run import _build_files, _check, _discover
from solana_fuzzer._native import Pubkey

FIXTURE_DIR = Path(__file__).parent / "fixtures"
ADDR = "4Q7CGJjsU5jR3PCVa7sXhCf3qccWq2Hmh1ekAEC8QhS2"
NONE_ROOT = "/nonexistent-idl-root"


def _generate(out_dir):
    return run_gen(target_idls=(str(FIXTURE_DIR),), dep_idls=(NONE_ROOT,),
                   out=str(out_dir))


@pytest.fixture(scope="module")
def gen(tmp_path_factory):
    """Generate the package once, import it (exercising self-registration), and
    yield the program module."""
    root = tmp_path_factory.mktemp("genroot")
    assert _generate(root / "pytypes") == 0
    sys.path.insert(0, str(root))
    for name in [n for n in sys.modules if n == "pytypes" or n.startswith("pytypes.")]:
        del sys.modules[name]
    pytypes = importlib.import_module("pytypes")
    try:
        yield pytypes.fixture_program
    finally:
        sys.path.remove(str(root))
        for name in [n for n in sys.modules
                     if n == "pytypes" or n.startswith("pytypes.")]:
            del sys.modules[name]


def _alltypes(mod, *, referrer=None, delegate=None, expiry=Opt(Opt(99))):
    return mod.AllTypes(
        id=2**63, price=2**100, tick=-5, ratio=3.5, is_bid=True,
        memo="héllo, мир", payload=b"\x00\xff\x10", maker=Pubkey(10),
        referrer=referrer, delegate=delegate, tags=[1, 2, 3],
        checksum=bytes(range(32)), samples=[1, -2, 3, -4], expiry=expiry,
        side=mod.Side.Ask, action=mod.Action.Pair(7, Pubkey(12)),
        inner=mod.Inner(a=9, b=False),
    )


# --------------------------------------------------------------------------- #
# the generated package imports and self-registers
# --------------------------------------------------------------------------- #
def test_generated_package_registers(gen):
    assert gen.PROGRAM_ID == Pubkey(ADDR)
    assert gen.PROGRAM_NAME == "fixture_program"
    # registration happened on import -> dispatch resolves the program
    dec = decode_instruction(gen.PROGRAM_ID, b"\x99" * 8, 0)
    assert dec.program_name == "fixture_program"


# --------------------------------------------------------------------------- #
# round-trip through the generated builder + dispatch
# --------------------------------------------------------------------------- #
def test_do_swap_roundtrip_via_dispatch(gen):
    user, pool = Pubkey(1), Pubkey(2)
    ix = gen.FixtureProgram().do_swap(123_456_789, gen.Side.Ask, user=user, pool=pool)

    dec = decode_instruction(gen.PROGRAM_ID, ix.data, len(ix.accounts))
    assert dec.name == "do_swap"
    assert dec.args == {"amount_in": 123_456_789, "side": gen.Side.Ask}
    assert dec.account_names == ["user", "pool", "referrer"]
    assert ix.accounts[0].is_signer and ix.accounts[0].is_writable
    # omitted interior optional -> program-ID sentinel at its fixed slot
    assert ix.accounts[2].pubkey == gen.PROGRAM_ID
    assert not ix.accounts[2].is_signer and not ix.accounts[2].is_writable


def test_store_alltypes_roundtrip_via_dispatch(gen):
    v = _alltypes(gen, referrer=Pubkey(11))
    ix = gen.FixtureProgram().store(v, account=Pubkey(1))
    dec = decode_instruction(gen.PROGRAM_ID, ix.data, len(ix.accounts))
    assert dec.name == "store"
    assert dec.args["cfg"] == v


def test_noop_zero_data_args(gen):
    ix = gen.FixtureProgram().noop(account=Pubkey(1))
    dec = decode_instruction(gen.PROGRAM_ID, ix.data, 1)
    assert dec.name == "noop" and dec.args == {} and dec.account_names == ["account"]


# --------------------------------------------------------------------------- #
# direct codec over the generated type surface
# --------------------------------------------------------------------------- #
def test_alltypes_roundtrip_direct(gen):
    node = compile_type(gen.AllTypes)
    for v in (_alltypes(gen, referrer=Pubkey(11)),
              _alltypes(gen, referrer=None, delegate=Pubkey(5)),
              _alltypes(gen, expiry=None),
              _alltypes(gen, expiry=Opt(None))):
        b = encode(v, node)
        assert decode(b, node, Mode.IX_DATA) == v
        assert encode(decode(b, node, Mode.IX_DATA), node) == b


def test_generated_enums(gen):
    sn = compile_type(gen.Side)
    assert encode(gen.Side.Ask, sn) == b"\x01"
    an = compile_type(gen.Action)
    for v in (gen.Action.Noop(), gen.Action.Move(1, 2),
              gen.Action.Label("hi"), gen.Action.Pair(3, Pubkey(4))):
        assert decode(encode(v, an), an, Mode.IX_DATA) == v
    assert encode(gen.Action.Noop(), an) == b"\x00"
    assert isinstance(gen.Action.Move(1, 2), gen.Action)


def test_account_discriminator_and_slack(gen):
    node = compile_type(gen.Position)
    pos = gen.Position(owner=Pubkey(1), amount=42, bump=255)
    body = encode(pos, node)
    assert gen.Position.__borsh_meta__.discriminator == b"\xaa\xc0\x97\x2c\x1b\x3f\x10\x52"
    buf = gen.Position.__borsh_meta__.discriminator + body + b"\x00" * 16
    assert decode(buf[8:], node, Mode.ACCOUNT_DATA) == pos        # V-11 realloc slack
    with pytest.raises(Exception):
        decode(buf[8:], node, Mode.IX_DATA)                       # V-9 trailing bytes


# --------------------------------------------------------------------------- #
# ergonomics: typed account kwargs + .encode()/.decode() on dataclasses
# --------------------------------------------------------------------------- #
def test_account_kwargs_are_annotated(gen):
    sig = inspect.signature(gen.FixtureProgram.do_swap)
    assert sig.parameters["user"].annotation == "MetaLike"
    assert sig.parameters["referrer"].annotation == "MetaLike | None"
    assert sig.parameters["remaining_accounts"].annotation == "Sequence[MetaLike]"


def test_struct_encode_decode_methods(gen):
    inner = gen.Inner(a=9, b=False)
    data = inner.encode()                         # plain struct -> body only
    assert gen.Inner.decode(data) == inner
    # full AllTypes too (no __borsh_meta__, so no discriminator)
    v = _alltypes(gen, referrer=Pubkey(11))
    assert gen.AllTypes.decode(v.encode()) == v
    assert not hasattr(gen.AllTypes, "__borsh_meta__")


def test_account_encode_decode_discriminator(gen):
    pos = gen.Position(owner=Pubkey(1), amount=42, bump=255)
    disc = gen.Position.__borsh_meta__.discriminator
    full = pos.encode()                           # account -> discriminator + body
    assert full[:8] == disc
    assert gen.Position.decode(full) == pos
    assert gen.Position.decode(full + b"\x00" * 16) == pos    # realloc slack tolerated
    # body-only mode (no discriminator on either side)
    body = pos.encode(with_discriminator=False)
    assert full == disc + body
    assert gen.Position.decode(body, with_discriminator=False) == pos
    # a wrong discriminator is refused, never silently mis-decoded
    with pytest.raises(Exception):
        gen.Position.decode(b"\x00" * 8 + body)


# --------------------------------------------------------------------------- #
# fixed-address accounts default to their pubkey (IDL `address` + well-known)
# --------------------------------------------------------------------------- #
def test_fixed_address_accounts_default(gen):
    # `init` has payer (required), system_program (IDL address) and
    # token_program (no address -> well-known name fallback).
    ix = gen.FixtureProgram().init(payer=Pubkey(9))
    pks = [str(a.pubkey) for a in ix.accounts]
    assert pks[0] == str(Pubkey(9))
    assert pks[1] == "11111111111111111111111111111111"              # IDL address
    assert pks[2] == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"   # well-known name
    # defaults are plain overridable kwargs
    ix2 = gen.FixtureProgram().init(payer=Pubkey(9), token_program=Pubkey(7))
    assert ix2.accounts[2].pubkey == Pubkey(7)
    # the decoder still labels every slot by role (defaults are build-side only)
    dec = decode_instruction(gen.PROGRAM_ID, ix.data, len(ix.accounts))
    assert dec.name == "init"
    assert dec.account_names == ["payer", "system_program", "token_program"]


def test_account_slot_accepts_metalike_forms(gen):
    from solana_fuzzer._native import Account
    user = Account(Pubkey(9))                       # a live Account view
    ix = gen.FixtureProgram().do_swap(1, gen.Side.Bid, user=user, pool=Pubkey(2))
    assert ix.accounts[0].pubkey == Pubkey(9)       # Account coerced to its address
    # other address-like forms coerce too
    for val, expected in ((str(Pubkey(3)), Pubkey(3)), (5, Pubkey(5))):
        ix = gen.FixtureProgram().do_swap(1, gen.Side.Bid, user=val, pool=Pubkey(2))
        assert ix.accounts[0].pubkey == expected


def test_wellknown_addresses_are_valid_base58():
    from solana_fuzzer._gen import wellknown
    for name, addr in wellknown.NAME_TO_ADDRESS.items():
        assert Pubkey(addr)                              # raises if malformed
        assert wellknown.resolve(name.upper()) == addr   # normalization


# --------------------------------------------------------------------------- #
# generated output matches the canonical hand-written fixture, byte-for-byte
# --------------------------------------------------------------------------- #
def test_generated_data_matches_handwritten_fixture(gen):
    # identical discriminators + Borsh layout -> identical instruction data
    g = gen.FixtureProgram().do_swap(7, gen.Side.Bid, user=Pubkey(1), pool=Pubkey(2))
    h = fp.Fixture().do_swap(7, fp.Side.Bid, user=Pubkey(1), pool=Pubkey(2))
    assert g.data == h.data

    gv = _alltypes(gen, referrer=Pubkey(11))
    hv = fp.AllTypes(
        id=gv.id, price=gv.price, tick=gv.tick, ratio=gv.ratio, is_bid=gv.is_bid,
        memo=gv.memo, payload=gv.payload, maker=gv.maker, referrer=gv.referrer,
        delegate=gv.delegate, tags=gv.tags, checksum=gv.checksum,
        samples=gv.samples, expiry=gv.expiry, side=fp.Side.Ask,
        action=fp.Action.Pair(7, Pubkey(12)), inner=fp.Inner(a=9, b=False),
    )
    assert gen.FixtureProgram().store(gv, account=Pubkey(1)).data == \
        fp.Fixture().store(hv, account=Pubkey(1)).data


# --------------------------------------------------------------------------- #
# determinism + the --check drift gate (§9.6, §9.7)
# --------------------------------------------------------------------------- #
def test_regeneration_is_byte_identical(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert _generate(a) == 0 and _generate(b) == 0
    for name in ("fixture_program.py", "__init__.py"):
        assert (a / name).read_text() == (b / name).read_text()


def test_check_is_noop_on_fresh_output(tmp_path):
    out = tmp_path / "pytypes"
    assert _generate(out) == 0
    rc = run_gen(target_idls=(str(FIXTURE_DIR),), dep_idls=(NONE_ROOT,),
                 out=str(out), check=True)
    assert rc == 0


def test_check_detects_drift(tmp_path):
    out = tmp_path / "pytypes"
    assert _generate(out) == 0
    (out / "fixture_program.py").write_text("# tampered\n")
    rc = run_gen(target_idls=(str(FIXTURE_DIR),), dep_idls=(NONE_ROOT,),
                 out=str(out), check=True)
    assert rc == 2


def test_manifest_provenance(tmp_path):
    out = tmp_path / "pytypes"
    assert _generate(out) == 0
    manifest = json.loads((out / "_manifest.json").read_text())
    assert set(manifest) == {ADDR}
    entry = manifest[ADDR]
    assert entry["verified"] is False
    assert entry["module"] == "fixture_program"
    assert len(entry["idl_sha256"]) == 64
    assert "generated_at" in entry


# --------------------------------------------------------------------------- #
# refusal stubs (§9.8) and address keying (§9.1)
# --------------------------------------------------------------------------- #
def _write_idl(dir_, name, doc):
    p = dir_ / name
    p.write_text(json.dumps(doc))
    return p


def test_punt_emits_refusing_stub(tmp_path):
    idls = tmp_path / "idls"
    idls.mkdir()
    addr = "GenPunt1111111111111111111111111111111111111"
    _write_idl(idls, f"{addr}.json", {
        "address": addr,
        "metadata": {"name": "punt_program"},
        "instructions": [{
            "name": "weird", "discriminator": [1, 2, 3, 4, 5, 6, 7, 8],
            "accounts": [], "args": [{"name": "x", "type": {"generic": "T"}}],
        }],
        "accounts": [], "types": [],
    })
    out = tmp_path / "pytypes"
    # non-strict: refusing stub, run still succeeds
    assert run_gen(target_idls=(NONE_ROOT,), dep_idls=(str(idls),), out=str(out)) == 0
    src = (out / "punt_program.py").read_text()
    assert "RefusingInterface" in src and "generation_punt" in src

    sys.path.insert(0, str(tmp_path))
    try:
        for n in [n for n in sys.modules if n == "pytypes" or n.startswith("pytypes.")]:
            del sys.modules[n]
        pkg = importlib.import_module("pytypes")
        with pytest.raises(RefuseToDecode):
            decode_instruction(pkg.punt_program.PROGRAM_ID, b"\x01" * 8, 0)
    finally:
        sys.path.remove(str(tmp_path))
        for n in [n for n in sys.modules if n == "pytypes" or n.startswith("pytypes.")]:
            del sys.modules[n]


def test_strict_turns_punt_into_failure(tmp_path):
    idls = tmp_path / "idls"
    idls.mkdir()
    addr = "GenPunt2222222222222222222222222222222222222"
    _write_idl(idls, f"{addr}.json", {
        "address": addr, "metadata": {"name": "punt2"},
        "instructions": [{
            "name": "weird", "discriminator": [1, 2, 3, 4, 5, 6, 7, 8],
            "accounts": [], "args": [{"name": "x", "type": "u256"}],
        }],
        "accounts": [], "types": [],
    })
    rc = run_gen(target_idls=(NONE_ROOT,), dep_idls=(str(idls),),
                 out=str(tmp_path / "pytypes"), strict=True)
    assert rc == 2


def test_unkeyable_idl_errors(tmp_path):
    idls = tmp_path / "idls"
    idls.mkdir()
    _write_idl(idls, "not-an-address.json", {"metadata": {"name": "x"},
                                             "instructions": [], "types": []})
    rc = run_gen(target_idls=(NONE_ROOT,), dep_idls=(str(idls),),
                 out=str(tmp_path / "pytypes"))
    assert rc == 2


def test_discovery_target_wins_over_dep(tmp_path):
    target, dep = tmp_path / "target", tmp_path / "dep"
    target.mkdir()
    dep.mkdir()
    addr = "11111111111111111111111111111119"
    _write_idl(target, f"{addr}.json", {"address": addr,
               "metadata": {"name": "from_target"}, "instructions": [], "types": []})
    _write_idl(dep, f"{addr}.json", {"address": addr,
               "metadata": {"name": "from_dep"}, "instructions": [], "types": []})
    discovered = _discover((str(target),), (str(dep),), verbose=0)
    idl, _path, src = discovered[addr]
    assert src == "target/idl" and idl.name == "from_target"
