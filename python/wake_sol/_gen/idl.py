"""IDL reader: parse Anchor 0.30 / 0.31 (and legacy pre-0.30) JSON into a
normalized internal model, resolve the program address, and pin ``idl_sha256``.

The normalized model is intentionally thin — type *nodes* are kept as their
(lightly normalized) JSON values and lowered to Python annotations by
:mod:`._gen.types`. The reader's job is structural: address keying,
discriminator resolution (verbatim when present, computed fallback otherwise),
account-flag spelling reconciliation, and pairing account declarations with
their struct layouts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional

from .._codec import GenError, anchor_discriminator


# --------------------------------------------------------------------------- #
# normalized model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Field:
    """One struct/variant field. ``name is None`` marks a tuple field."""

    name: Optional[str]
    type: object            # normalized IDL type node (str | dict | list)


@dataclass(frozen=True)
class Variant:
    name: str
    fields: tuple           # tuple[Field, ...]; empty => unit variant


@dataclass(frozen=True)
class TypeDef:
    name: str
    kind: str               # "struct" | "enum"
    serialization: str      # "borsh" (default) | "bytemuck" | ...
    fields: tuple = ()      # tuple[Field, ...]      (struct)
    variants: tuple = ()    # tuple[Variant, ...]    (enum)


@dataclass(frozen=True)
class AccountDecl:
    """An account root: its name (indexes into the type table for its layout)
    plus its verbatim/computed discriminator."""

    name: str
    discriminator: bytes


@dataclass(frozen=True)
class EventDecl:
    """An `emit!`/`emit_cpi!` event: its name (indexes into the type table for
    its layout) plus its verbatim/computed 8-byte discriminator."""

    name: str
    discriminator: bytes


@dataclass(frozen=True)
class IxArg:
    name: str
    type: object


@dataclass(frozen=True)
class IxAccount:
    name: str
    is_signer: bool
    is_writable: bool
    is_optional: bool
    address: Optional[str] = None     # IDL-pinned fixed address (0.30+), if any


@dataclass(frozen=True)
class Instruction:
    name: str
    discriminator: bytes
    args: tuple             # tuple[IxArg, ...]
    accounts: tuple         # tuple[IxAccount, ...]
    returns: object = None  # normalized IDL `returns` type node, or None


@dataclass
class ErrorDef:
    """One IDL ``errors[]`` entry. ``code`` is copied VERBATIM (never recomputed
    as ``6000 + index`` — non-contiguous custom offsets exist)."""
    code: int
    name: str
    msg: Optional[str]


@dataclass
class Idl:
    address: str
    name: str
    version: str
    anchor_version: Optional[str]
    spec: Optional[str]
    raw_bytes: bytes
    idl_sha256: str
    types: list = field(default_factory=list)            # list[TypeDef], decl order
    accounts: list = field(default_factory=list)         # list[AccountDecl], decl order
    instructions: list = field(default_factory=list)     # list[Instruction], decl order
    errors: list = field(default_factory=list)           # list[ErrorDef], decl order
    events: list = field(default_factory=list)           # list[EventDecl], decl order

    @property
    def type_layouts(self):
        """name -> TypeDef for every struct/enum layout (types[] + inline)."""
        return {t.name: t for t in self.types}

    @property
    def account_names(self):
        return {a.name for a in self.accounts}

    @property
    def event_names(self):
        return {e.name for e in self.events}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _disc(raw, kind: str, name: str) -> bytes:
    """Verbatim discriminator array when present (0.30+ / 0.31 custom), else the
    pre-0.30 computed fallback ``sha256(prefix:name)[:8]``."""
    arr = raw.get("discriminator")
    if arr is not None:
        return bytes(arr)
    return anchor_discriminator(kind, name)


def _norm_type_node(node):
    """Lightly normalize a type node so the mapper sees one shape:

    - legacy ``{"defined": "Foo"}`` -> ``{"defined": {"name": "Foo"}}``
    - legacy ``"publicKey"`` -> ``"pubkey"``
    Containers are normalized recursively; unknown shapes pass through verbatim
    (the mapper raises on anything it cannot lower)."""
    if isinstance(node, str):
        return "pubkey" if node == "publicKey" else node
    if isinstance(node, dict):
        if "defined" in node:
            d = node["defined"]
            name = d if isinstance(d, str) else d.get("name")
            return {"defined": {"name": name}}
        if "option" in node:
            return {"option": _norm_type_node(node["option"])}
        if "coption" in node:
            return {"coption": _norm_type_node(node["coption"])}
        if "vec" in node:
            return {"vec": _norm_type_node(node["vec"])}
        if "array" in node:
            elem, n = node["array"]
            return {"array": [_norm_type_node(elem), n]}
        # generic / other -> pass through for the mapper to refuse
        return node
    return node


def _fields(raw_fields) -> tuple:
    """Normalize a struct/variant ``fields`` array into ``tuple[Field]``.
    Named when each entry is a dict carrying ``name``; tuple otherwise."""
    out = []
    for entry in raw_fields or ():
        if isinstance(entry, dict) and "name" in entry:
            out.append(Field(entry["name"], _norm_type_node(entry["type"])))
        elif isinstance(entry, dict) and "type" in entry:   # tuple field, dict-wrapped
            out.append(Field(None, _norm_type_node(entry["type"])))
        else:                                                # bare type node => tuple field
            out.append(Field(None, _norm_type_node(entry)))
    return tuple(out)


def _typedef(raw) -> TypeDef:
    name = raw["name"]
    ty = raw.get("type", {})
    kind = ty.get("kind", "struct")
    ser = (ty.get("serialization") or raw.get("serialization") or "borsh")
    if isinstance(ser, dict):                # 0.31 sometimes wraps as {"borsh": {}}
        ser = next(iter(ser), "borsh")
    if kind == "enum":
        variants = tuple(
            Variant(v["name"], _fields(v.get("fields")))
            for v in ty.get("variants", ())
        )
        return TypeDef(name, "enum", ser, variants=variants)
    return TypeDef(name, "struct", ser, fields=_fields(ty.get("fields")))


def _flatten_accounts(raw_accounts) -> list:
    """Flatten Anchor nested account groups into positional slots, reconciling
    the 0.30 (``isSigner``/``isMut``/``isOptional``) and 0.31
    (``signer``/``writable``/``optional``) spellings."""
    out = []
    for a in raw_accounts or ():
        if "accounts" in a:                  # nested composite group -> flatten
            out.extend(_flatten_accounts(a["accounts"]))
            continue
        addr = a.get("address")
        out.append(IxAccount(
            name=a["name"],
            is_signer=bool(a.get("signer", a.get("isSigner", False))),
            is_writable=bool(a.get("writable", a.get("isMut", False))),
            is_optional=bool(a.get("optional", a.get("isOptional", False))),
            address=str(addr) if addr else None,
        ))
    return out


def _resolve_address(doc, path) -> str:
    """Address precedence: top-level ``address`` -> ``metadata.address``
    -> base58 file stem -> GenError."""
    addr = doc.get("address") or (doc.get("metadata") or {}).get("address")
    if addr:
        return str(addr)
    stem = path.stem
    if _is_base58_address(stem):
        return stem
    raise GenError(f"cannot key IDL {path} by address (no address field, "
                   f"file stem {stem!r} is not a base58 program address)")


_B58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def _is_base58_address(s: str) -> bool:
    return 32 <= len(s) <= 44 and bool(s) and all(c in _B58_ALPHABET for c in s)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def load_idl(path) -> Idl:
    """Parse one IDL file at ``path`` into the normalized model. ``idl_sha256``
    is over the exact bytes consumed (computed before parsing) so it is stable
    against JSON re-formatting."""
    raw_bytes = path.read_bytes()
    idl_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    doc = json.loads(raw_bytes)

    address = _resolve_address(doc, path)
    metadata = doc.get("metadata") or {}
    name = metadata.get("name") or doc.get("name") or path.stem
    version = metadata.get("version") or doc.get("version") or "0.0.0"
    anchor_version = metadata.get("spec") or doc.get("version")

    idl = Idl(
        address=address, name=name, version=version,
        anchor_version=anchor_version, spec=metadata.get("spec"),
        raw_bytes=raw_bytes, idl_sha256=idl_sha256,
    )

    idl.types = [_typedef(t) for t in doc.get("types", ())]

    # Legacy IDLs carry the account layout inline under accounts[].type; 0.30+
    # keep it in types[] and leave accounts[] holding name + discriminator.
    known = {t.name for t in idl.types}
    for a in doc.get("accounts", ()):
        nm = a["name"]
        idl.accounts.append(AccountDecl(nm, _disc(a, "account", nm)))
        if nm not in known and "type" in a:           # inline legacy layout
            idl.types.append(_typedef(a))
            known.add(nm)

    for ix in doc.get("instructions", ()):
        ret = ix.get("returns")
        idl.instructions.append(Instruction(
            name=ix["name"],
            discriminator=_disc(ix, "instruction", ix["name"]),
            args=tuple(IxArg(a["name"], _norm_type_node(a["type"]))
                       for a in ix.get("args", ())),
            accounts=tuple(_flatten_accounts(ix.get("accounts"))),
            returns=_norm_type_node(ret) if ret is not None else None,
        ))

    # errors[]: code copied verbatim. Present in both legacy and 0.30+ IDLs.
    for e in doc.get("errors", ()):
        idl.errors.append(ErrorDef(int(e["code"]), e["name"], e.get("msg")))

    # events[]: discriminator verbatim (0.30+) or computed sha256("event:Name")
    # (legacy). 0.30+ keeps the layout in types[]; legacy carries `fields` inline.
    for e in doc.get("events", ()):
        nm = e["name"]
        idl.events.append(EventDecl(nm, _disc(e, "event", nm)))
        if nm not in known and "fields" in e:          # legacy inline layout
            idl.types.append(TypeDef(nm, "struct", "borsh", fields=_fields(e["fields"])))
            known.add(nm)

    return idl
