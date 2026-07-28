"""The deterministic Borsh codec core: cursor, typed error, modes, entry points.

v1 is pure Python (the Rust pyo3 port is deferred — §12.3). The per-type
read/write logic lives on the IR nodes (``ir.py``); this module owns the cursor
(the single byte chokepoint), the typed ``BorshError``, the shared depth guard,
the mode-aware trailing-byte rule, and the encode/decode entry points.
"""

from __future__ import annotations

import enum
from contextlib import contextmanager

MAX_DECODE_DEPTH = 64


class BorshError(ValueError):
    """Raised on any unverifiable decode/encode. NEVER swallowed by the codec.

    Carries ``offset`` (byte position) and ``path`` (the field/index
    breadcrumb) for actionable diagnostics and for the negative-test gate to
    assert on.
    """

    def __init__(self, msg, *, offset, path=()):
        self.offset = offset
        self.path = tuple(path)
        loc = "".join(f".{p}" if isinstance(p, str) else f"[{p}]" for p in self.path)
        super().__init__(f"{msg} (at byte {offset}{', path ' + loc if loc else ''})")


class Mode(enum.Enum):
    IX_DATA = "ix_data"            # instruction data / events / return-data
    ACCOUNT_DATA = "account_data"  # account state (trailing realloc slack ok)


class Cursor:
    """The single bounds-checked byte source. Nothing else indexes the buffer."""

    __slots__ = ("buf", "pos", "depth")

    def __init__(self, buf):
        self.buf = buf
        self.pos = 0
        self.depth = 0

    @property
    def remaining(self):
        return len(self.buf) - self.pos

    def take(self, n, path):
        if n < 0 or n > self.remaining:
            raise BorshError(f"need {n} bytes, {self.remaining} remain",
                             offset=self.pos, path=path)
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    @contextmanager
    def descend(self, path):
        """Enter a composite node, enforcing the shared depth guard (V-6)."""
        self.depth += 1
        if self.depth > MAX_DECODE_DEPTH:
            raise BorshError(f"decode depth exceeded {MAX_DECODE_DEPTH}",
                             offset=self.pos, path=path)
        try:
            yield
        finally:
            self.depth -= 1


def decode(buf, node, mode):
    """Decode ``buf`` (a struct body — discriminator already stripped) against
    ``node``, applying the mode-aware trailing-byte rule."""
    cur = Cursor(buf)
    value = node.read(cur, ())
    if mode is Mode.IX_DATA and cur.pos != len(buf):          # V-9
        raise BorshError(
            f"trailing bytes: consumed {cur.pos} of {len(buf)} (layout mismatch)",
            offset=cur.pos, path=())
    # Mode.ACCOUNT_DATA: trailing realloc slack is ignored (V-11) — the one
    # deliberate non-refusal in the codec.
    return value


def encode(value, node):
    """Encode ``value`` against ``node`` into Borsh bytes (no discriminator)."""
    out = bytearray()
    node.write(value, out, ())
    return bytes(out)
