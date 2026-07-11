"""v1 deterministic-engine acceptance tests: round-trip, negatives (the V-rules),
gen-time guards, discriminator dispatch, and mode-aware trailing bytes — driven
through the engine and the fixture program."""

import struct
import warnings
from dataclasses import dataclass
from typing import Annotated, Optional, Union

import pytest

import fixture_program as fp
from solana_fuzzer import AccountMeta, Pubkey, decode_instruction
from solana_fuzzer._codec import (
    AccountFlagOverride,
    BorshError,
    GenError,
    InstructionMeta,
    Mode,
    Opt,
    RefuseToDecode,
    Serialization,
    build_interface_from_module,
    compile_field,
    compile_type,
    decode,
    encode,
    f64,
    instruction,
    u8,
    u64,
)


# A module-level recursive type for the depth-guard test (so get_type_hints can
# resolve the forward reference from this module's globals).
@dataclass
class _Nest:
    child: Optional["_Nest"]


def _roundtrip(value, node):
    """decode(encode(v)) == v  and  encode(decode(b)) == b (byte-exact)."""
    b = encode(value, node)
    assert decode(b, node, Mode.IX_DATA) == value
    assert encode(decode(b, node, Mode.IX_DATA), node) == b
    return b


def _sample_alltypes(expiry=Opt(Opt(99)), referrer=Pubkey(11), delegate=None):
    return fp.AllTypes(
        id=2**63, price=2**100, tick=-5, ratio=3.5, is_bid=True,
        memo="héllo, мир", payload=b"\x00\xff\x10", maker=Pubkey(10),
        referrer=referrer, delegate=delegate, tags=[1, 2, 3],
        checksum=bytes(range(32)), samples=[1, -2, 3, -4], expiry=expiry,
        side=fp.Side.Ask, action=fp.Action.Pair(7, Pubkey(12)),
        inner=fp.Inner(a=9, b=False),
    )


# --------------------------------------------------------------------------- #
# round-trip — builder + dispatch (the unified convention end-to-end)
# --------------------------------------------------------------------------- #
def test_do_swap_roundtrip_via_dispatch():
    user, pool = Pubkey(1), Pubkey(2)
    ix = fp.Fixture().do_swap(123_456_789, fp.Side.Ask, user=user, pool=pool)

    dec = decode_instruction(fp.PROGRAM_ID, ix.data, len(ix.accounts))
    assert dec.program_name == "Fixture Program"
    assert dec.name == "do_swap"
    assert dec.args == {"amount_in": 123_456_789, "side": fp.Side.Ask}
    assert dec.account_names == ["user", "pool", "referrer"]

    metas = ix.accounts
    assert metas[0].pubkey == user and metas[0].is_signer and metas[0].is_writable
    assert metas[1].pubkey == pool and metas[1].is_writable and not metas[1].is_signer
    # omitted interior optional -> program-ID sentinel at its FIXED slot, no shift
    assert metas[2].pubkey == fp.PROGRAM_ID
    assert not metas[2].is_signer and not metas[2].is_writable


def test_do_swap_optional_present_keeps_address():
    ix = fp.Fixture().do_swap(1, fp.Side.Bid, user=Pubkey(1), pool=Pubkey(2),
                              referrer=Pubkey(3))
    assert ix.accounts[2].pubkey == Pubkey(3)


def test_noop_zero_data_args():
    ix = fp.Fixture().noop(account=Pubkey(1))
    assert ix.data == fp.DISC_NOOP
    dec = decode_instruction(fp.PROGRAM_ID, ix.data, 1)
    assert dec.name == "noop" and dec.args == {} and dec.account_names == ["account"]


def test_store_alltypes_roundtrip_via_dispatch():
    v = _sample_alltypes()
    ix = fp.Fixture().store(v, account=Pubkey(1))
    dec = decode_instruction(fp.PROGRAM_ID, ix.data, len(ix.accounts))
    assert dec.name == "store"
    assert dec.args["cfg"] == v


def test_encode_ix_backcompat_matches_layout():
    # `encode_ix` (per-arg annotations) is retained for hand-written builders; it
    # must keep producing the exact bytes of `encode_ix_layout` (precompiled
    # layout, as generated code and the migrated fixture now use). Covers scalar,
    # IntEnum, nested-struct, and zero-arg shapes.
    from solana_fuzzer._codec import compile_layout, encode_ix, encode_ix_layout
    cases = [
        (fp.DISC_SWAP, [(7, u64), (fp.Side.Ask, fp.Side)]),
        (fp.DISC_STORE, [(_sample_alltypes(), fp.AllTypes)]),
        (fp.DISC_NOOP, []),
    ]
    for disc, args in cases:
        layout = compile_layout(*[ann for _, ann in args])
        values = [v for v, _ in args]
        assert encode_ix(disc, *args) == encode_ix_layout(disc, layout, *values)
    # arity mismatch between layout and values is caught loudly (codegen guard)
    with pytest.raises(ValueError):
        encode_ix_layout(fp.DISC_STORE, compile_layout(u64), 1, 2)


# --------------------------------------------------------------------------- #
# round-trip — direct codec over the type surface
# --------------------------------------------------------------------------- #
def test_alltypes_roundtrip_direct():
    node = compile_type(fp.AllTypes)
    _roundtrip(_sample_alltypes(), node)


def test_option_both_states():
    node = compile_type(fp.AllTypes)
    _roundtrip(_sample_alltypes(referrer=None, delegate=Pubkey(5)), node)


def test_opt_nested_keeps_some_none_distinct():
    node = compile_field(Opt[Opt[u64]])
    for v in (None, Opt(None), Opt(Opt(7))):
        _roundtrip(v, node)
    assert encode(None, node) == b"\x00"
    assert encode(Opt(None), node) == b"\x01\x00"
    assert encode(Opt(Opt(7)), node)[:2] == b"\x01\x01"


def test_borsh_enum_all_variant_shapes():
    node = compile_type(fp.Action)
    for v in (fp.Action.Noop(), fp.Action.Move(1, 2),
              fp.Action.Label("hi"), fp.Action.Pair(3, Pubkey(4))):
        _roundtrip(v, node)
    assert encode(fp.Action.Noop(), node) == b"\x00"        # unit variant = bare tag
    assert encode(fp.Action.Move(1, 2), node)[0] == 1
    assert isinstance(fp.Action.Move(1, 2), fp.Action)       # metaclass __instancecheck__


def test_int_enum_roundtrip():
    node = compile_type(fp.Side)
    for v in (fp.Side.Bid, fp.Side.Ask):
        _roundtrip(v, node)
    assert encode(fp.Side.Ask, node) == b"\x01"


def test_tuple_and_unit_structs():
    pn = compile_type(fp.Pixel)
    _roundtrip(fp.Pixel(1, 2), pn)
    en = compile_type(fp.Empty)
    assert encode(fp.Empty(), en) == b""
    assert decode(b"", en, Mode.IX_DATA) == fp.Empty()


def test_fixed_bytes_vs_bytes_wire():
    fixed = compile_field(Annotated[bytes, 4])
    assert encode(b"\x01\x02\x03\x04", fixed) == b"\x01\x02\x03\x04"   # no prefix
    assert decode(b"\x01\x02\x03\x04", fixed, Mode.IX_DATA) == b"\x01\x02\x03\x04"
    bare = compile_field(bytes)
    assert encode(b"\x01\x02\x03\x04", bare) == b"\x04\x00\x00\x00\x01\x02\x03\x04"


# --------------------------------------------------------------------------- #
# account decode: discriminator strip + mode-aware trailing bytes
# --------------------------------------------------------------------------- #
def test_account_decode_ignores_realloc_slack():
    node = compile_type(fp.Position)
    v = fp.Position(owner=Pubkey(1), amount=42, bump=255)
    body = encode(v, node)
    buf = fp.Position.__borsh_meta__.discriminator + body + b"\x00" * 16  # slack
    got = decode(buf[8:], node, Mode.ACCOUNT_DATA)                        # V-11: ok
    assert got == v
    assert encode(got, node) == body          # round-trip == the consumed prefix
    with pytest.raises(BorshError):                                       # V-9: refuse
        decode(buf[8:], node, Mode.IX_DATA)


# --------------------------------------------------------------------------- #
# negatives — every one MUST raise (the V-rules)
# --------------------------------------------------------------------------- #
def test_v1_bool_byte():
    with pytest.raises(BorshError):
        decode(b"\x02", compile_field(bool), Mode.IX_DATA)


def test_v2_option_tag():
    with pytest.raises(BorshError):
        decode(b"\x02\x00", compile_field(Optional[u8]), Mode.IX_DATA)


def test_v2_enum_tag_out_of_range():
    with pytest.raises(BorshError):
        decode(b"\x09", compile_type(fp.Side), Mode.IX_DATA)
    with pytest.raises(BorshError):
        decode(b"\x09", compile_type(fp.Action), Mode.IX_DATA)


def test_v3_invalid_utf8():
    with pytest.raises(BorshError):
        decode(b"\x01\x00\x00\x00\xff", compile_field(str), Mode.IX_DATA)


def test_v4_nan_float():
    with pytest.raises(BorshError):
        decode(struct.pack("<d", float("nan")), compile_field(f64), Mode.IX_DATA)


def test_v5_length_prefix_overrun():
    node = compile_field(list[u64])
    with pytest.raises(BorshError):                 # claimed 4 billion elements
        decode(b"\xff\xff\xff\xff", node, Mode.IX_DATA)
    with pytest.raises(BorshError):                 # length exceeds remaining
        decode(b"\x05\x00\x00\x00\x01", node, Mode.IX_DATA)


def test_v6_depth_guard():
    node = compile_type(_Nest)
    deep = b"\x01" * 80 + b"\x00"                    # 80 nested Some(...) then None
    with pytest.raises(BorshError):
        decode(deep, node, Mode.IX_DATA)


def test_v7_read_past_end():
    with pytest.raises(BorshError):
        decode(b"\x01\x02", compile_field(u64), Mode.IX_DATA)


def test_v9_trailing_bytes_ix_data():
    with pytest.raises(BorshError):
        decode(b"\x01\x02", compile_field(u8), Mode.IX_DATA)


# --------------------------------------------------------------------------- #
# gen-time guards — refuse at compile, not at decode
# --------------------------------------------------------------------------- #
def test_width_alias_min_max_bits():
    from solana_fuzzer._codec import i8, i256, u8, u256
    assert (u8.min, u8.max, u8.bits, u8.signed) == (0, 255, 8, False)
    assert (i8.min, i8.max, i8.bits, i8.signed) == (-128, 127, 8, True)
    assert u256.max == 2**256 - 1
    assert i256.min == -(2**255) and i256.max == 2**255 - 1
    # constructing out of range raises; in range is a plain int
    with pytest.raises(ValueError):
        u8(256)
    with pytest.raises(ValueError):
        i8(-129)
    assert u8(255) == 255 and isinstance(u8(255), int)


def test_gen_guard_bare_numeric():
    with pytest.raises(GenError):
        compile_field(int)
    with pytest.raises(GenError):
        compile_field(float)


def test_gen_guard_untagged_union():
    with pytest.raises(GenError):
        compile_field(Union[u64, fp.Side])          # two non-None args
    with pytest.raises(GenError):
        compile_field(Union[u64, fp.Side, None])    # >2 args


# --------------------------------------------------------------------------- #
# dispatch + non-borsh refusal + flag override
# --------------------------------------------------------------------------- #
def test_matched_borsh_decode_failure_propagates():
    # do_swap discriminator + a truncated body: matched, but the u64 amount has
    # no bytes -> the do-now fix makes this RAISE (never silent empty args).
    with pytest.raises(BorshError):
        decode_instruction(fp.PROGRAM_ID, fp.DISC_SWAP + b"\x01", 3)


def test_dispatch_miss_is_graceful():
    dec = decode_instruction(fp.PROGRAM_ID, b"\x99" * 8, 2)
    assert dec.name is None
    assert dec.program_name == "Fixture Program"
    assert dec.account_names == [None, None]


def test_non_borsh_refuses_observably():
    class B:
        @instruction(InstructionMeta(name="legacy", discriminator=b"\xfe",
                                     serialization=Serialization.BINCODE, accounts=()))
        def legacy(self, x: u64, *, acc):
            ...

    iface = build_interface_from_module("t", B, Pubkey(5), "B")
    with pytest.raises(RefuseToDecode):
        iface.decode(b"\xfe" + b"\x00" * 8, 1)


def test_decode_override_supplies_builtin_closure():
    class B:
        @instruction(InstructionMeta(name="legacy", discriminator=b"\xfe",
                                     serialization=Serialization.BINCODE, accounts=()))
        def legacy(self, x: u64, *, acc):
            ...

    def _legacy(data):
        return {"x": struct.unpack_from("<Q", data, 0)[0]}

    iface = build_interface_from_module("t", B, Pubkey(6), "B",
                                        decode_overrides={"legacy": _legacy})
    dec = iface.decode(b"\xfe" + (7).to_bytes(8, "little"), 1)
    assert dec.args == {"x": 7}


def test_flag_override_warns():
    ro = AccountMeta(Pubkey(1), False, False)       # readonly non-signer
    with warnings.catch_warnings():
        warnings.simplefilter("error", AccountFlagOverride)
        with pytest.raises(AccountFlagOverride):    # user slot is signer+writable
            fp.Fixture().do_swap(1, fp.Side.Bid, user=ro, pool=Pubkey(2))
