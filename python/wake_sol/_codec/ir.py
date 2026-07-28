"""The ``FieldType`` IR: compiled once per type from annotations, walked by the
codec. The generated Python annotations are the single source of truth; the IR
is a compiled projection of one ``get_type_hints(..., include_extras=True)``
walk per type.

Each node exposes ``read(cursor, path)`` (decode), ``write(value, out, path)``
(encode), ``is_composite`` (drives the shared depth guard), and
``min_wire_size(stack)`` (the V-5 length floor; back-edges floor to 0).
"""

from __future__ import annotations

import dataclasses
import struct
import types as _types
import typing
from enum import IntEnum

from .aliases import float_spec, int_spec
from .aliases import pubkey as _pubkey
from .carriers import COption, Opt, BorshEnumMeta
from .codec import BorshError
from .meta import GenError

_NoneType = type(None)
_UNION_TYPE = getattr(_types, "UnionType", None)   # PEP 604 `X | Y`; 3.10+


# --------------------------------------------------------------------------- #
# union / option detection
# --------------------------------------------------------------------------- #
def _is_union(ann):
    origin = typing.get_origin(ann)
    return origin is typing.Union or (_UNION_TYPE is not None and origin is _UNION_TYPE)


def option_inner(ann):
    """Return ``T`` if ``ann`` is a 2-arg union containing ``NoneType``, else
    ``None`` — normalizing both ``Optional[T]`` and ``T | None``."""
    if not _is_union(ann):
        return None
    args = typing.get_args(ann)
    non_none = [a for a in args if a is not _NoneType]
    if len(args) == 2 and len(non_none) == 1:
        return non_none[0]
    return None


# --------------------------------------------------------------------------- #
# nodes
# --------------------------------------------------------------------------- #
class _Node:
    is_composite = False

    def min_wire_size(self, _stack=()):
        return 0


class IntNode(_Node):
    def __init__(self, alias, nbytes, signed):
        self.alias, self.nbytes, self.signed = alias, nbytes, signed

    def read(self, cur, path):
        return int.from_bytes(cur.take(self.nbytes, path), "little", signed=self.signed)

    def write(self, value, out, path):
        if not isinstance(value, int) or isinstance(value, bool):
            raise BorshError(f"expected int, got {type(value).__name__}",
                             offset=len(out), path=path)
        try:
            out += value.to_bytes(self.nbytes, "little", signed=self.signed)
        except OverflowError:
            raise BorshError(f"int {value} out of range for {self.nbytes * 8}-bit",
                             offset=len(out), path=path) from None

    def min_wire_size(self, _stack=()):
        return self.nbytes


class FloatNode(_Node):
    def __init__(self, alias, nbytes):
        self.alias, self.nbytes = alias, nbytes
        self.fmt = "<f" if nbytes == 4 else "<d"

    def read(self, cur, path):
        (v,) = struct.unpack(self.fmt, cur.take(self.nbytes, path))
        if v != v:                                            # NaN (V-4)
            raise BorshError("NaN float", offset=cur.pos - self.nbytes, path=path)
        return v

    def write(self, value, out, path):
        if value != value:
            raise BorshError("NaN float", offset=len(out), path=path)
        out += struct.pack(self.fmt, value)

    def min_wire_size(self, _stack=()):
        return self.nbytes


class BoolNode(_Node):
    def read(self, cur, path):
        b = cur.take(1, path)[0]
        if b not in (0, 1):                                   # V-1
            raise BorshError(f"bool byte {b} not in {{0,1}}", offset=cur.pos - 1, path=path)
        return b == 1

    def write(self, value, out, path):
        if value is True or value == 1:
            out += b"\x01"
        elif value is False or value == 0:
            out += b"\x00"
        else:
            raise BorshError(f"bool must be 0/1, got {value!r}", offset=len(out), path=path)

    def min_wire_size(self, _stack=()):
        return 1


def _read_seq_len(cur, min_elem, path):
    """u32 length with the V-5 bounds: unconditional ``n > remaining`` ceiling,
    plus the tighter ``n > remaining // min_elem`` byte floor. Never pre-allocate."""
    n = int.from_bytes(cur.take(4, path), "little")
    if n > cur.remaining:
        raise BorshError(f"length {n} exceeds remaining {cur.remaining}",
                         offset=cur.pos - 4, path=path)
    if min_elem > 0 and n > cur.remaining // min_elem:
        raise BorshError(
            f"length {n} exceeds buffer (remaining {cur.remaining}, min elem {min_elem})",
            offset=cur.pos - 4, path=path)
    return n


class StrNode(_Node):
    def read(self, cur, path):
        n = _read_seq_len(cur, 1, path)
        raw = cur.take(n, path)
        try:
            return raw.decode("utf-8")                        # strict (V-3)
        except UnicodeDecodeError as e:
            raise BorshError(f"invalid UTF-8: {e}", offset=cur.pos - n, path=path) from None

    def write(self, value, out, path):
        if not isinstance(value, str):
            raise BorshError(f"expected str, got {type(value).__name__}",
                             offset=len(out), path=path)
        b = value.encode("utf-8")
        out += len(b).to_bytes(4, "little")
        out += b

    def min_wire_size(self, _stack=()):
        return 4


class BytesNode(_Node):
    """IDL ``bytes`` / ``Vec<u8>``: u32 len + raw."""

    def read(self, cur, path):
        n = _read_seq_len(cur, 1, path)
        return bytes(cur.take(n, path))

    def write(self, value, out, path):
        b = bytes(value)
        out += len(b).to_bytes(4, "little")
        out += b

    def min_wire_size(self, _stack=()):
        return 4


class FixedBytesNode(_Node):
    """``[u8;N]`` -> exactly N raw bytes, no prefix, decodes to ``bytes``."""

    def __init__(self, n):
        self.n = n

    def read(self, cur, path):
        return bytes(cur.take(self.n, path))

    def write(self, value, out, path):
        b = bytes(value)
        if len(b) != self.n:
            raise BorshError(f"fixed bytes length {len(b)} != {self.n}",
                             offset=len(out), path=path)
        out += b

    def min_wire_size(self, _stack=()):
        return self.n


class PubkeyNode(_Node):
    def read(self, cur, path):
        return _pubkey(bytes(cur.take(32, path)))

    def write(self, value, out, path):
        out += _pubkey(value).to_bytes()

    def min_wire_size(self, _stack=()):
        return 32


class OptionNode(_Node):
    is_composite = True

    def __init__(self, inner):
        self.inner = inner

    def read(self, cur, path):
        with cur.descend(path):
            tag = cur.take(1, path)[0]
            if tag == 0:
                return None
            if tag == 1:
                return self.inner.read(cur, path)
            raise BorshError(f"option tag {tag} not in {{0,1}}", offset=cur.pos - 1, path=path)

    def write(self, value, out, path):
        if value is None:
            out += b"\x00"
        else:
            out += b"\x01"
            self.inner.write(value, out, path)

    def min_wire_size(self, _stack=()):
        return 1


class OptNode(_Node):
    """Non-collapsing ``Opt[...]`` — returns ``Opt(inner)`` / ``None`` so
    ``Some(None)`` and ``None`` stay distinct."""

    is_composite = True

    def __init__(self, inner):
        self.inner = inner

    def read(self, cur, path):
        with cur.descend(path):
            tag = cur.take(1, path)[0]
            if tag == 0:
                return None
            if tag == 1:
                return Opt(self.inner.read(cur, path))
            raise BorshError(f"option tag {tag} not in {{0,1}}", offset=cur.pos - 1, path=path)

    def write(self, value, out, path):
        if value is None:
            out += b"\x00"
        else:
            if not isinstance(value, Opt):
                raise BorshError(f"expected Opt(...) or None, got {type(value).__name__}",
                                 offset=len(out), path=path)
            out += b"\x01"
            self.inner.write(value.value, out, path)

    def min_wire_size(self, _stack=()):
        return 1


class VecNode(_Node):
    is_composite = True

    def __init__(self, elem):
        self.elem = elem

    def read(self, cur, path):
        with cur.descend(path):
            n = _read_seq_len(cur, self.elem.min_wire_size(), path)
            return [self.elem.read(cur, path + (i,)) for i in range(n)]

    def write(self, value, out, path):
        out += len(value).to_bytes(4, "little")
        for i, v in enumerate(value):
            self.elem.write(v, out, path + (i,))

    def min_wire_size(self, _stack=()):
        return 4


class FixedArrayNode(_Node):
    is_composite = True

    def __init__(self, elem, n):
        self.elem, self.n = elem, n

    def read(self, cur, path):
        with cur.descend(path):
            return [self.elem.read(cur, path + (i,)) for i in range(self.n)]

    def write(self, value, out, path):
        if len(value) != self.n:
            raise BorshError(f"array length {len(value)} != {self.n}",
                             offset=len(out), path=path)
        for i, v in enumerate(value):
            self.elem.write(v, out, path + (i,))

    def min_wire_size(self, _stack=()):
        if self in _stack:
            return 0
        return self.n * self.elem.min_wire_size(_stack + (self,))


class StructNode(_Node):
    is_composite = True

    def __init__(self, cls):
        self.cls = cls
        self.fields = []        # [(name, node)] — filled after caching

    def read(self, cur, path):
        with cur.descend(path):
            values = [node.read(cur, path + (name,)) for name, node in self.fields]
            return self.cls(*values)

    def write(self, value, out, path):
        for name, node in self.fields:
            node.write(getattr(value, name), out, path + (name,))

    def min_wire_size(self, _stack=()):
        if self in _stack:
            return 0
        s = _stack + (self,)
        return sum(node.min_wire_size(s) for _, node in self.fields)


class IntEnumNode(_Node):
    is_composite = True

    def __init__(self, cls):
        self.cls = cls
        self._values = {m.value for m in cls}

    def read(self, cur, path):
        with cur.descend(path):
            tag = cur.take(1, path)[0]
            if tag not in self._values:                       # V-2
                raise BorshError(f"enum tag {tag} out of range for {self.cls.__name__}",
                                 offset=cur.pos - 1, path=path)
            return self.cls(tag)

    def write(self, value, out, path):
        out += int(value).to_bytes(1, "little")

    def min_wire_size(self, _stack=()):
        return 1


class BorshEnumNode(_Node):
    is_composite = True

    def __init__(self, cls):
        self.cls = cls
        self.variants = {}      # tag -> (VariantCls, [(name, node)]) — filled after caching

    def read(self, cur, path):
        with cur.descend(path):
            tag = cur.take(1, path)[0]
            entry = self.variants.get(tag)
            if entry is None:                                 # V-2
                raise BorshError(f"enum tag {tag} has no variant in {self.cls.__name__}",
                                 offset=cur.pos - 1, path=path)
            vcls, fields = entry
            values = [node.read(cur, path + (vcls.__name__,)) for _, node in fields]
            return vcls(*values)

    def write(self, value, out, path):
        vcls = type(value)
        tag = getattr(vcls, "__variant_tag__", None)
        entry = self.variants.get(tag) if tag is not None else None
        if entry is None or entry[0] is not vcls:
            raise BorshError(f"{vcls.__name__} is not a variant of {self.cls.__name__}",
                             offset=len(out), path=path)
        out += int(tag).to_bytes(1, "little")
        for name, node in entry[1]:
            node.write(getattr(value, name), out, path + (name,))

    def min_wire_size(self, _stack=()):
        if self in _stack:
            return 0
        s = _stack + (self,)
        if not self.variants:
            return 1
        return 1 + min(sum(node.min_wire_size(s) for _, node in fields)
                       for _, fields in self.variants.values())


# --------------------------------------------------------------------------- #
# compiler
# --------------------------------------------------------------------------- #
_TYPE_CACHE = {}   # cls -> Node  (also breaks import-time reference cycles)


def _dataclass_fields(cls):
    """[(field_name, resolved_annotation)] in declaration order."""
    hints = typing.get_type_hints(cls, include_extras=True)
    return [(f.name, hints[f.name]) for f in dataclasses.fields(cls)]


def compile_type(cls):
    """Compile a defined type (struct/account ``@dataclass``, ``IntEnum``, or
    ``BorshEnum``) into its IR node, caching to break cycles."""
    cached = _TYPE_CACHE.get(cls)
    if cached is not None:
        return cached
    if isinstance(cls, type) and issubclass(cls, IntEnum):
        node = IntEnumNode(cls)
        _TYPE_CACHE[cls] = node
        return node
    if isinstance(cls, BorshEnumMeta):
        node = BorshEnumNode(cls)
        _TYPE_CACHE[cls] = node                               # cache before filling
        try:
            for tag, vcls in cls.__variants_by_tag__.items():
                node.variants[tag] = (
                    vcls,
                    [(n, compile_field(a, f"{cls.__name__}.{vcls.__name__}.{n}"))
                     for n, a in _dataclass_fields(vcls)],
                )
        except Exception:
            _TYPE_CACHE.pop(cls, None)                        # don't cache a failed compile
            raise
        return node
    if dataclasses.is_dataclass(cls):
        node = StructNode(cls)
        _TYPE_CACHE[cls] = node                               # cache before filling
        try:
            node.fields = [(n, compile_field(a, f"{cls.__name__}.{n}"))
                           for n, a in _dataclass_fields(cls)]
        except Exception:
            _TYPE_CACHE.pop(cls, None)
            raise
        return node
    raise GenError(f"cannot compile type {cls!r}: not a dataclass, IntEnum, or BorshEnum")


def compile_field(ann, where="<field>"):
    """Lower one resolved annotation into an IR node (resolution order: bare
    guard -> Annotated -> Opt -> option -> reject-other-union -> vec -> scalar
    -> defined type)."""
    # bare-numeric guard (§2.5): width unrecoverable
    if ann is int or ann is float:
        raise GenError(
            f"{where}: numeric field collapsed to bare {ann.__name__!r}; a "
            "width-carrying alias (u8..u256 / i8..i256 / f32 / f64) is required")

    # Annotated -> fixed bytes / fixed array (no length prefix)
    if hasattr(ann, "__metadata__"):
        inner = ann.__origin__
        n = ann.__metadata__[0]
        if not isinstance(n, int):
            raise GenError(f"{where}: array length metadata must be int, got {n!r}")
        if inner is bytes:
            return FixedBytesNode(n)
        if typing.get_origin(inner) is list:
            (elem,) = typing.get_args(inner)
            return FixedArrayNode(compile_field(elem, where), n)
        raise GenError(f"{where}: unsupported Annotated inner type {inner!r}")

    # COption is SPL-Pack-only — the v1 Borsh engine ships no COption branch
    if ann is COption or typing.get_origin(ann) is COption:
        raise GenError(f"{where}: COption is decoded by the SPL Pack built-in, "
                       "not the Borsh engine")

    # non-collapsing Opt[...]
    if typing.get_origin(ann) is Opt:
        (inner,) = typing.get_args(ann)
        return OptNode(compile_field(inner, where))

    # option (both spellings)
    inner = option_inner(ann)
    if inner is not None:
        return OptionNode(compile_field(inner, where))
    if _is_union(ann):
        raise GenError(f"{where}: untagged union {ann!r} has no Borsh encoding; "
                       "sum types must be a defined enum")

    # vec
    if typing.get_origin(ann) is list:
        (elem,) = typing.get_args(ann)
        return VecNode(compile_field(elem, where))

    # scalars by identity
    spec = int_spec(ann)
    if spec is not None:
        return IntNode(ann, spec[0], spec[1])
    fspec = float_spec(ann)
    if fspec is not None:
        return FloatNode(ann, fspec)
    if ann is bool:
        return BoolNode()
    if ann is str:
        return StrNode()
    if ann is bytes:
        return BytesNode()
    if ann is _pubkey:                                        # native Pubkey class
        return PubkeyNode()

    # defined type (dataclass struct/account, IntEnum, or BorshEnum)
    if isinstance(ann, BorshEnumMeta):
        return compile_type(ann)
    if isinstance(ann, type) and (dataclasses.is_dataclass(ann) or issubclass(ann, IntEnum)):
        return compile_type(ann)

    raise GenError(f"{where}: unsupported annotation {ann!r}")


def compile_layout(*anns):
    """Lower an instruction's positional arg annotations into a tuple of IR
    nodes, once. The encode-side mirror of the decode layout compiled at
    registration (``build_interface_from_module``): generated builders hold the
    returned tuple at module scope and feed it to ``encode_ix_layout`` on every
    call, so each annotation is lowered once per instruction instead of once per
    encode. Goes through ``compile_field`` (hence ``_TYPE_CACHE``), so defined
    types share the exact node the decode side uses."""
    return tuple(compile_field(a, "<ix-arg>") for a in anns)
