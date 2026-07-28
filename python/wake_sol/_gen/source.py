"""Best-effort mapping from an instruction to the ``file://`` location of its Rust
handler, for the optional ``gen --source-links`` builder docstrings.

The IDL carries no source location, so we recover it heuristically and per program:

1. **Identify the crate** by the ``declare_id!("<address>")`` whose address matches
   the IDL's program address — an authoritative link (the address is unique).
2. **Locate each handler** as ``pub fn <name>`` within that crate's sources,
   searching the declaring file first (the ``#[program]`` entry points live there)
   before the rest of the crate.

Anything not found is simply omitted — links are a convenience, never required, and
never fail generation. Because the emitted URIs are absolute, output produced with
this on is machine-specific (see ``run_gen(..., source_links=...)``).
"""

from __future__ import annotations

import re
from pathlib import Path

# The base58 program id inside `declare_id!("…")`.
_DECLARE_ID_RE = re.compile(r'declare_id!\s*\(\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
# A public handler declaration: `pub fn <name>` (args/generics may follow).
_PUB_FN_RE = re.compile(r"\bpub\s+fn\s+([A-Za-z_][A-Za-z0-9_]*)")


def _read(path: Path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def index_crates(source_roots) -> dict:
    """Scan ``*.rs`` under each root for ``declare_id!(addr)``; return
    ``{program_address: declaring_file}`` (usually the crate's ``lib.rs``, which
    also holds the ``#[program]`` module). First declaration wins per address."""
    crates: dict = {}
    for root in source_roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for rs in sorted(root.rglob("*.rs")):
            text = _read(rs)
            if text is None:
                continue
            m = _DECLARE_ID_RE.search(text)
            if m:
                crates.setdefault(m.group(1), rs)
    return crates


def handler_uris(lib_rs: Path, names) -> dict:
    """Locate each instruction ``name``'s ``pub fn <name>``, searching the crate's
    declaring file (its ``#[program]`` entry points) first, then the rest of the
    crate. Return ``{name: "file:///abs/path#L<line>"}`` for those found."""
    wanted = set(names)
    found: dict = {}
    siblings = [p for p in sorted(lib_rs.parent.rglob("*.rs")) if p != lib_rs]
    for rs in [lib_rs, *siblings]:
        if wanted <= set(found):
            break
        text = _read(rs)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            m = _PUB_FN_RE.search(line)
            if m and m.group(1) in wanted and m.group(1) not in found:
                found[m.group(1)] = (rs.resolve(), lineno)
    return {name: f"{path.as_uri()}#L{line}" for name, (path, line) in found.items()}
