"""Builder-side helpers (encode_ix / build_metas / slot) and the introspection
that derives a :class:`ProgramInterface` from a builder class — one signature is
the single source of truth for both directions.
"""

from __future__ import annotations

import inspect
import keyword
import warnings
from typing import TypeVar

from .._interface import ProgramInterface
from .._native import Account, AccountMeta, Pubkey
from .._native import Instruction  # noqa: F401  (re-exported for generated modules)
from .codec import BorshError, Cursor, Mode
from .codec import decode as _decode
from .codec import encode as _encode
from .ir import compile_field, compile_type
from .meta import AccountFlagOverride, Kind, Serialization

_RESERVED = {"self", "remaining_accounts"}

#: The value a generated builder accepts for an account slot: an explicit
#: ``AccountMeta``, or any address-like the harness coerces to one — a
#: ``Pubkey``, a live ``Account`` (its address is used), or the base58 ``str`` /
#: raw 32-``bytes`` / big-endian ``int`` forms. Mirrors ``_native``'s
#: ``AccountMeta | AddressLike``. A real runtime object (not a stub-only alias)
#: so it resolves when generated-module annotations are evaluated at
#: registration; ``build_metas`` does the actual coercion via ``AccountMeta``.
MetaLike = AccountMeta | Pubkey | Account | str | bytes | int

#: What the decoders accept for raw bytes — anything ``bytes()`` can consume.
#: ``Account.data`` hands back ``bytes``; ``bytearray`` / ``memoryview`` turn up
#: when a caller slices a buffer instead of copying it.
BytesLike = bytes | bytearray | memoryview


# --------------------------------------------------------------------------- #
# ergonomic, compilation-hidden encode/decode for generated dataclasses
# --------------------------------------------------------------------------- #
_S = TypeVar("_S", bound="BorshStruct")


class BorshStruct:
    """Base class for generated Borsh ``@dataclass`` types, providing ergonomic
    ``.encode()`` / ``.decode()`` that hide the (cached) layout compilation —
    callers never touch ``compile_type``. Because the methods are declared on a
    real base class (not monkey-patched), IDEs and type-checkers see them and
    autocomplete works.

    For an **account-root** type (one whose ``__borsh_meta__`` carries a
    discriminator) ``.encode(with_discriminator=True)`` prepends the
    discriminator — the full on-chain layout, ready for ``svm.set_account`` —
    and ``.decode(data, with_discriminator=True)`` strips/verifies it, decoding
    with account-data semantics (trailing realloc slack ignored). A **plain
    struct** encodes/decodes its Borsh body with instruction-data semantics
    (exact length); the ``with_discriminator`` flag is then a no-op.

    Carries no fields/annotations of its own, so subclass dataclass layout is
    unaffected (the IR compiler only sees the subclass's own fields).
    """

    __slots__ = ()

    def encode(self, with_discriminator: bool = True) -> bytes:
        node = compile_type(type(self))
        body = _encode(self, node)
        meta = getattr(type(self), "__borsh_meta__", None)
        if with_discriminator and meta is not None and meta.discriminator:
            return meta.discriminator + body
        return body

    @classmethod
    def decode(cls: type[_S], data: BytesLike, with_discriminator: bool = True) -> _S:
        node = compile_type(cls)
        meta = getattr(cls, "__borsh_meta__", None)
        is_account = meta is not None and meta.is_account_root
        is_event = meta is not None and meta.kind is Kind.EVENT
        data = bytes(data)
        # Account-roots and events both carry a leading discriminator — strip and
        # verify it (plain structs have none).
        if (is_account or is_event) and with_discriminator and meta.discriminator:
            disc = meta.discriminator
            if data[: len(disc)] != disc:
                raise BorshError("discriminator mismatch", offset=0, path=())
            data = data[len(disc):]
        # Accounts tolerate trailing realloc slack; events (like ix data) are exact.
        mode = Mode.ACCOUNT_DATA if is_account else Mode.IX_DATA
        return _decode(data, node, mode)


# --------------------------------------------------------------------------- #
# name mangling (shared, reversible, reserved-aware)
# --------------------------------------------------------------------------- #
def mangle(name):
    """IDL account/arg name -> a legal, non-reserved Python identifier."""
    if keyword.iskeyword(name) or name in _RESERVED:
        return name + "_"
    return name


def mangle_back(name):
    """Recover the IDL name from a mangled Python identifier."""
    if name.endswith("_") and (keyword.iskeyword(name[:-1]) or name[:-1] in _RESERVED):
        return name[:-1]
    return name


# --------------------------------------------------------------------------- #
# encode side
# --------------------------------------------------------------------------- #
def encode_ix(discriminator, *args):
    """``discriminator`` + Borsh(args). Each arg is a ``(value, annotation)``
    pair, in IDL order. The single trusted encoder (validates as it goes)."""
    out = bytearray(discriminator)
    for value, ann in args:
        compile_field(ann, "<ix-arg>").write(value, out, ())
    return bytes(out)


def encode_ix_layout(discriminator, layout, *values):
    """``discriminator`` + Borsh(``values``). ``layout`` is a tuple of
    pre-compiled IR nodes (one per positional arg, built once at import via
    :func:`~wake_sol._codec.ir.compile_layout`), aligned with ``values`` in
    IDL order. The encode twin of :func:`make_borsh_decoder`: annotations are
    lowered once at registration and the nodes reused, so nothing is compiled on
    the hot path. Emitted by ``wake-sol gen``; hand-written builders can use
    the self-contained :func:`encode_ix` instead."""
    if len(values) != len(layout):
        raise ValueError(f"encode_ix_layout: {len(layout)} layout node(s) but "
                         f"{len(values)} value(s)")
    out = bytearray(discriminator)
    for node, value in zip(layout, values):
        node.write(value, out, ())
    return bytes(out)


def as_meta(value):
    """Coerce an address-like into a read-only non-signer ``AccountMeta`` (used
    for ``remaining_accounts``); an existing ``AccountMeta`` passes through."""
    return value if isinstance(value, AccountMeta) else AccountMeta(value, False, False)


def slot(value, is_signer, is_writable, is_optional):
    """Describe one declared account slot for :func:`build_metas`."""
    return (value, is_signer, is_writable, is_optional)


def build_metas(program_id, *slots):
    """Assemble the per-instruction ``AccountMeta`` vector in declared order.

    An omitted optional (``None``) becomes the **program-ID sentinel** at its
    fixed slot (Anchor convention; privileges off) — nothing is dropped or
    shifted, so interior optionals stay aligned. A ``None`` required slot errors.
    """
    metas = []
    for value, is_signer, is_writable, is_optional in slots:
        if value is None:
            if not is_optional:
                raise ValueError("required account slot is None")
            metas.append(AccountMeta(program_id, False, False))   # sentinel
        elif isinstance(value, AccountMeta):
            if value.is_signer != is_signer or value.is_writable != is_writable:
                warnings.warn(
                    f"AccountMeta flags (signer={value.is_signer}, "
                    f"writable={value.is_writable}) differ from IDL "
                    f"(signer={is_signer}, writable={is_writable})",
                    AccountFlagOverride, stacklevel=3)
            metas.append(value)
        else:
            metas.append(AccountMeta(value, is_signer, is_writable))
    return metas


# --------------------------------------------------------------------------- #
# decode side — derived from the builder signature
# --------------------------------------------------------------------------- #
def make_borsh_decoder(layout):
    """``layout`` = ``[(arg_name, node)]`` -> a closure mapping the
    post-discriminator bytes to ``{arg_name: value}`` (strict trailing-byte
    check, IX_DATA semantics)."""
    names = [n for n, _ in layout]
    nodes = [nd for _, nd in layout]

    def decode_args(data: BytesLike):
        cur = Cursor(data)
        values = [node.read(cur, (name,)) for name, node in zip(names, nodes)]
        if cur.pos != len(data):                              # V-9 (IX_DATA)
            raise BorshError(f"trailing bytes: consumed {cur.pos} of {len(data)}",
                             offset=cur.pos, path=())
        return dict(zip(names, values))

    return decode_args


def make_returns_decoder(node):
    """``node`` = the compiled IR for an instruction's IDL ``returns`` type -> a
    closure mapping raw return-data bytes to the decoded value. Strict, IX_DATA
    semantics (exact length, full §5.11 validation) — so a wrong-type guess on
    the low-level path fails here rather than yielding a plausible-wrong value."""

    def decode_return(data: BytesLike):
        cur = Cursor(data)
        value = node.read(cur, ("<returns>",))
        if cur.pos != len(data):
            raise BorshError(
                f"trailing bytes in return data: consumed {cur.pos} of {len(data)}",
                offset=cur.pos, path=())
        return value

    return decode_return


_POSITIONAL_OR_KEYWORD = inspect.Parameter.POSITIONAL_OR_KEYWORD
_KEYWORD_ONLY = inspect.Parameter.KEYWORD_ONLY


def _public_methods(builder_cls):
    for name in vars(builder_cls):
        obj = getattr(builder_cls, name)
        if callable(obj) and hasattr(obj, "__pytypes_ix__"):
            yield name, obj


def build_interface_from_module(mod_name, builder_cls, program_id, program_name,
                                decode_overrides=None):
    """Derive a :class:`ProgramInterface` by reflecting on the builder class.

    Positional params (before ``*``) = the Borsh arg layout; keyword-only params
    (excluding ``remaining_accounts``) = account role names. Built-ins pass
    hand-written closures via ``decode_overrides``; non-borsh instructions with
    no override register a refusing stub.
    """
    decode_overrides = decode_overrides or {}
    iface = ProgramInterface(str(program_id), program_name)
    for _attr, method in _public_methods(builder_cls):
        meta = method.__pytypes_ix__
        override = decode_overrides.get(meta.name)
        if meta.serialization is not Serialization.BORSH and override is None:
            iface.add_refusing(meta.name, meta.discriminator,
                               reason=f"serialization={meta.serialization.value}")
            continue
        sig = inspect.signature(method, eval_str=True)        # resolve PEP 563 hints
        data_params = [p for p in sig.parameters.values()
                       if p.kind is _POSITIONAL_OR_KEYWORD and p.name != "self"]
        account_params = [p for p in sig.parameters.values()
                          if p.kind is _KEYWORD_ONLY and p.name != "remaining_accounts"]
        layout = [(p.name, compile_field(p.annotation, f"{meta.name}.{p.name}"))
                  for p in data_params]
        account_names = [mangle_back(p.name) for p in account_params]
        decode_args = override or make_borsh_decoder(layout)
        # Return-data decoder from the IDL `returns` type, if the ix declares one.
        decode_return = None
        if meta.returns_type is not None:
            ret_node = compile_field(meta.returns_type, f"{meta.name}.<returns>")
            decode_return = make_returns_decoder(ret_node)
        iface.add(meta.name, meta.discriminator, account_names, decode_args,
                  decode_return)

    # Register events: module-level Kind.EVENT BorshStructs, keyed by their
    # discriminator, so `Program data:` / `emit_cpi!` payloads decode.
    import sys

    module = sys.modules.get(mod_name)
    if module is not None:
        for obj in vars(module).values():
            m = getattr(obj, "__borsh_meta__", None)
            if isinstance(obj, type) and m is not None and m.kind is Kind.EVENT:
                iface.add_event(m.discriminator, obj)
    return iface
