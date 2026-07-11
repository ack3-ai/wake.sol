"""The single canonical runtime engine surface (``solana_fuzzer._codec``).

Everything generated modules and built-ins need is re-exported here: the
lowercase width aliases, the carriers, the metadata records + decorators, the
deterministic codec, and the builder/introspection helpers. (Resolves the
§2.2-vs-§4 import-path inconsistency: one public package, internal submodules.)

v1 implements the codec in Python; the Rust (pyo3) port is deferred (§12.3).
"""

from __future__ import annotations

# --- width-carrying aliases + specs ---
from .aliases import (
    u8, u16, u32, u64, u128, u256,
    i8, i16, i32, i64, i128, i256,
    f32, f64, char, pubkey,
    int_spec, float_spec,
)

# --- carriers + enum machinery ---
from .carriers import BorshEnum, BorshEnumMeta, COption, Opt, variant

# --- inert metadata records + decorators ---
from .meta import (
    AccountFlagOverride, AccountSlot, BorshMeta, GenError, IdlPda,
    InstructionMeta, Kind, ProgramError, RefuseToDecode, SerKind, Serialization,
    Seed, anchor_discriminator, event, instruction,
)

# --- deterministic codec core ---
from .codec import MAX_DECODE_DEPTH, BorshError, Cursor, Mode, decode, encode

# --- IR compiler ---
from .ir import compile_field, compile_layout, compile_type, option_inner

# --- builder + introspection ---
from .builder import (
    BorshStruct, MetaLike, as_meta, build_interface_from_module, build_metas,
    encode_ix, encode_ix_layout, make_borsh_decoder, mangle, mangle_back, slot,
)

__all__ = [
    # aliases
    "u8", "u16", "u32", "u64", "u128", "u256",
    "i8", "i16", "i32", "i64", "i128", "i256",
    "f32", "f64", "char", "pubkey", "int_spec", "float_spec",
    # carriers
    "Opt", "COption", "variant", "BorshEnum", "BorshEnumMeta",
    # metadata
    "Serialization", "SerKind", "Kind", "BorshMeta", "AccountSlot", "Seed",
    "IdlPda", "InstructionMeta", "instruction", "event", "ProgramError",
    "AccountFlagOverride", "GenError", "RefuseToDecode", "anchor_discriminator",
    # codec
    "BorshError", "Cursor", "Mode", "decode", "encode", "MAX_DECODE_DEPTH",
    # ir
    "compile_type", "compile_field", "compile_layout", "option_inner",
    # builder
    "encode_ix", "encode_ix_layout", "build_metas", "slot", "as_meta",
    "make_borsh_decoder",
    "mangle", "mangle_back", "build_interface_from_module", "BorshStruct",
    "MetaLike",
]
