"""Lower a normalized IDL type node to a Python annotation **source string** —
the emit side of what ``ir.compile_field`` consumes. The mapper records every
symbol it references (aliases, ``typing`` names, engine carriers, and
user-defined type names) on a shared :class:`Usage` accumulator so the emitter
can produce a minimal, deterministic import block.

Anything that cannot be represented in the supported Borsh surface raises
:class:`GenError` here, at generation time — never a guess.
"""

from __future__ import annotations

from .._codec import GenError

# IDL scalar name -> Python annotation source (the lowercase width aliases).
_SCALARS = {
    "u8": "u8", "u16": "u16", "u32": "u32", "u64": "u64", "u128": "u128",
    "i8": "i8", "i16": "i16", "i32": "i32", "i64": "i64", "i128": "i128",
    "f32": "f32", "f64": "f64",
}
_ALIAS_NAMES = set(_SCALARS.values()) | {"pubkey"}

# Engine-extension widths with no Anchor IDL spelling: reaching them from an IDL
# node is a hard error.
_REJECTED_SCALARS = {"u256", "i256", "char"}


class Usage:
    """Accumulates the symbols an emitted annotation references."""

    def __init__(self):
        self.aliases = set()        # "u64", "pubkey", ...
        self.typing = set()         # "Optional", "Annotated"
        self.carriers = set()       # "Opt"
        self.defined = set()        # user PascalCase type names

    def alias(self, name):
        self.aliases.add(name)
        return name


def _is_option(node) -> bool:
    return isinstance(node, dict) and "option" in node


def map_type(node, usage: Usage) -> str:
    """Return the Python annotation source for ``node``, recording symbols on
    ``usage``."""
    # scalars / builtins
    if isinstance(node, str):
        if node in _SCALARS:
            return usage.alias(_SCALARS[node])
        if node == "pubkey":
            return usage.alias("pubkey")
        if node == "bool":
            return "bool"
        if node == "string":
            return "str"
        if node == "bytes":
            return "bytes"
        if node in _REJECTED_SCALARS:
            raise GenError(f"IDL scalar {node!r} is an engine-extension type, "
                           "not Anchor-emittable")
        raise GenError(f"unsupported IDL scalar {node!r}")

    if isinstance(node, dict):
        if "option" in node:
            return _map_option(node, usage)
        if "vec" in node:
            return f"list[{map_type(node['vec'], usage)}]"
        if "array" in node:
            return _map_array(node, usage)
        if "defined" in node:
            name = node["defined"]["name"]
            if not name or not name.isidentifier():
                raise GenError(f"defined type name {name!r} is not a usable "
                               "Python identifier")
            usage.defined.add(name)
            return name
        if "coption" in node:
            raise GenError("COption is SPL-Pack-only; not decodable by the v1 "
                           "Borsh engine")
        if "generic" in node:
            raise GenError(f"generic type parameter {node['generic']!r} is not "
                           "monomorphizable from the IDL alone")

    raise GenError(f"unsupported IDL type node {node!r}")


def _map_option(node, usage: Usage) -> str:
    """``Option<T>`` -> ``Optional[T]``; ``Option<Option<...>>`` -> non-collapsing
    ``Opt[...]`` at every level."""
    inner = node["option"]
    if _is_option(inner):
        usage.carriers.add("Opt")
        return f"Opt[{_map_opt_chain(inner, usage)}]"
    usage.typing.add("Optional")
    return f"Optional[{map_type(inner, usage)}]"


def _map_opt_chain(node, usage: Usage) -> str:
    """``node`` is an option node inside an ``Opt[...]`` chain -> render it (and
    any further nested options) as ``Opt[...]``."""
    usage.carriers.add("Opt")
    inner = node["option"]
    if _is_option(inner):
        return f"Opt[{_map_opt_chain(inner, usage)}]"
    return f"Opt[{map_type(inner, usage)}]"


def _map_array(node, usage: Usage) -> str:
    elem, n = node["array"]
    if not isinstance(n, int):
        raise GenError(f"array length {n!r} must be an integer")
    usage.typing.add("Annotated")
    if elem == "u8":                              # [u8;N] -> fixed-length bytes
        return f"Annotated[bytes, {n}]"
    return f"Annotated[list[{map_type(elem, usage)}], {n}]"
