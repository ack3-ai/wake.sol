"""Non-collapsing Option carriers and the data-carrying-enum metaclass."""

from __future__ import annotations

from typing import Generic, TypeVar

_T = TypeVar("_T")


class Opt(Generic[_T]):
    """Non-collapsing Borsh ``Option<T>`` carrier, used only where Python's
    ``Optional`` / ``| None`` would collapse (i.e. ``Option<Option<...>>``).

    A *present* level is ``Opt(value)``; an *absent* level is bare ``None`` — so
    ``Some(None)`` decodes to ``Opt(None)`` and ``None`` to ``None``, keeping
    the two distinct (the whole reason this carrier exists). The wrapped value
    lives on ``.value`` so it is actually recoverable.
    """

    __slots__ = ("value",)

    def __init__(self, value=None):
        self.value = value

    def __eq__(self, other):
        return type(other) is Opt and other.value == self.value

    def __hash__(self):
        return hash((Opt, self.value))

    def __repr__(self):
        return f"Opt({self.value!r})"


class COption(Generic[_T]):
    """SPL Pack ``COption``: 4-byte LE tag (0=None, 1=Some), payload ALWAYS
    present. Decoded only by the hand-written SPL Pack built-in (tier 2), never
    by the Borsh codec (the v1 engine ships no COption branch)."""

    __slots__ = ("value",)

    def __init__(self, value=None):
        self.value = value

    def __eq__(self, other):
        return type(other) is COption and other.value == self.value

    def __hash__(self):
        return hash((COption, self.value))

    def __repr__(self):
        return f"COption({self.value!r})"


def variant(tag=None):
    """Mark a nested dataclass as a ``BorshEnum`` variant carrying wire ``u8``
    selector ``tag`` (defaulting to the contiguous declaration index)."""

    def deco(cls):
        cls.__variant_tag__ = tag
        return cls

    return deco


class BorshEnumMeta(type):
    """Metaclass for data-carrying enums: collects ``@variant`` dataclasses into
    an ordered ``__variants__`` tuple + a ``__variants_by_tag__`` map, and makes
    ``isinstance(x, E)`` true iff ``type(x)`` is one of ``E.__variants__``."""

    def __new__(mcs, name, bases, ns):
        cls = super().__new__(mcs, name, bases, ns)
        vs = [v for v in ns.values()
              if isinstance(v, type) and hasattr(v, "__variant_tag__")]
        for i, v in enumerate(vs):
            if v.__variant_tag__ is None:
                v.__variant_tag__ = i
        tags = [v.__variant_tag__ for v in vs]
        if len(vs) > 256:
            raise TypeError(f"{name}: >256 variants is not Borsh-encodable")
        if not all(isinstance(t, int) and 0 <= t <= 255 for t in tags):
            raise TypeError(f"{name}: variant tags must be u8 (0..255)")
        if len(set(tags)) != len(tags):
            raise TypeError(f"{name}: duplicate variant tags")
        cls.__variants__ = tuple(vs)
        cls.__variants_by_tag__ = {v.__variant_tag__: v for v in vs}
        return cls

    def __instancecheck__(cls, obj):
        return type(obj) in cls.__variants__


class BorshEnum(metaclass=BorshEnumMeta):
    """Convenience base so user enums can write ``class Foo(BorshEnum): ...``
    instead of spelling ``metaclass=BorshEnumMeta``."""
