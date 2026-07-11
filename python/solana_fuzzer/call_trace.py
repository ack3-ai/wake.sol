"""Render a transaction's call trace as a colored ``rich`` tree.

Consumes the structured trace (`TransactionResult.call_trace`) plus the decode
registry (`decode_instruction`) and label resolver (`resolve_label`). Each node
renders (wake-style) as ``program::instruction(arg=value, …) ✓ [N CU]`` followed
by its account slots, its own ``msg!`` log lines, and a ``➞`` line for a decoded
return value or a decoded error. CPIs are nested tree branches.

The error explanation (Anchor/panic) is shown **once**, decoded, on the ``➞``
line of the failing node — the raw error-explanation log lines are suppressed so
nothing is duplicated. Genuine ``msg!`` debug output is kept.
"""

from __future__ import annotations

import dataclasses
import io
import re
from typing import Optional

from rich.console import Console, Group, RenderableType
from rich.text import Text
from rich.tree import Tree

from ._interface import UnknownEvent, decode_instruction
from ._labels import resolve_label
from ._native import Pubkey

# Palette (kept close to Wake's call-trace colors).
_PROGRAM = "bold magenta"
_INSTRUCTION = "bold cyan"
_UNKNOWN = "dim red"
_ROLE = "cyan"
_REPR = "blue"
_NUMBER = "green"
_FLAG = "yellow"
_PUNCT = "dim"
_LOG = "white"
_ERROR = "bold red"
_CU = "cyan"
_RETURN = "green"
_EVENT = "bright_yellow"

# Per-node status glyph (wake-style ✓ / ✗), plus a dim ? for frames whose outcome
# the log stream didn't reveal (truncated, or never reached after an earlier fail).
_STATUS = {
    "success": ("✓", "bold green"),
    "failed": ("✗", "bold red"),
    "unknown": ("?", _PUNCT),
}

# Runtime error-explanation log lines (Anchor / panic / assert) — suppressed on a
# failed node because the decoded error is shown on its `➞` line instead.
_CUSTOM_ERR_RE = re.compile(r"custom program error: 0x([0-9a-fA-F]+)")


def _fmt_value(value) -> Text:
    """Render one decoded value with a type-appropriate style."""
    if isinstance(value, Pubkey):
        return Text(resolve_label(value), style=_REPR)
    if value is None:
        return Text("None", style=_PUNCT)
    if isinstance(value, bool):
        return Text(str(value), style=_NUMBER)
    if isinstance(value, int):
        return Text(format(value, ","), style=_NUMBER)
    if isinstance(value, (bytes, bytearray)):
        return Text("0x" + bytes(value).hex(), style=_NUMBER)
    return Text(repr(value))


def _header(node, dec) -> Text:
    program = dec.program_name or resolve_label(node.program_id)
    text = Text()
    text.append(program, style=_PROGRAM)
    text.append("::", style=_PUNCT)
    if dec.name:
        text.append(dec.name, style=_INSTRUCTION)
    else:
        text.append(f"<{len(node.data)} bytes>", style=_UNKNOWN)
    text.append("(", style=_PUNCT)
    for i, (key, value) in enumerate(dec.args.items()):
        if i:
            text.append(", ", style=_PUNCT)
        text.append(key, style=_PUNCT)
        text.append("=", style=_PUNCT)
        text.append_text(_fmt_value(value))
    text.append(")", style=_PUNCT)
    return text


def _account_line(index: int, role: Optional[str], account) -> Text:
    flags = ("S" if account.is_signer else "") + ("W" if account.is_writable else "")
    line = Text("  ")
    line.append(role if role is not None else f"#{index}", style=_ROLE)
    line.append(": ", style=_PUNCT)
    line.append(resolve_label(account.pubkey), style=_REPR)
    if flags:
        line.append(f" [{flags}]", style=_FLAG)
    return line


def _log_line(text: str) -> Text:
    """One raw program-log line emitted directly by this node (text only)."""
    line = Text("  ")
    line.append("▸ ", style=_LOG)
    line.append(text, style=_LOG)
    return line


def _is_error_log(s: str) -> bool:
    """A runtime error-explanation line (Anchor / panic / assert), which the
    decoded `➞` error already conveys — so we don't show it twice."""
    t = s.strip()
    return (
        t.startswith("AnchorError")
        or t.startswith("panicked at")
        or t.startswith("Error:")
        or "Error Message:" in t
        or "Error Code:" in t
        or "Error Number:" in t
    )


def _decode_return(node) -> Optional[Text]:
    """Decoded return value for this node (best-effort): decode the frame's
    return-data via its instruction's IDL `returns` type; fall back to raw hex."""
    raw = node.raw_return_value
    if not raw:
        return None
    from ._interface import decode_return_value

    try:
        return _fmt_value(decode_return_value(node.program_id, node.data, bytes(raw)))
    except Exception:
        return Text("0x" + bytes(raw).hex(), style=_NUMBER)


def _custom_error_code(raw: Optional[str]) -> Optional[int]:
    """The `custom program error: 0x…` code in a `failed:` message, else None."""
    if not raw:
        return None
    m = _CUSTOM_ERR_RE.search(raw)
    return int(m.group(1), 16) if m else None


def _propagates_child_error(node, code: int) -> bool:
    """True if a descendant frame failed with the same custom `code` — i.e. this
    frame merely propagated a child's error rather than originating it (a failed
    CPI's code bubbles up unchanged through every enclosing frame)."""
    return any(
        _custom_error_code(child.error) == code or _propagates_child_error(child, code)
        for child in node.inner
    )


def _decode_error(node) -> Optional[Text]:
    """Decoded error for a failed node (best-effort). A `custom program error: 0x…`
    is resolved through the error registry to its specific class, attributed to this
    node's program — so builtin System/Token codes resolve to their named errors,
    not just Anchor/user codes. The decoded line is shown on the frame that
    *originated* the code; a parent that only propagated a child's error keeps its
    `✗` glyph, so the error appears once (on the deepest frame) rather than as a
    misleading bare code on each enclosing frame. A native error's `failed:` text is
    already human, so surface it as-is."""
    raw = node.error
    if not raw:
        return None
    code = _custom_error_code(raw)
    if code is not None:
        if _propagates_child_error(node, code):
            return None  # shown on the originating (deeper) frame instead
        try:
            from ._errors import build

            return Text(str(build(code=code, program_id=str(node.program_id))), style=_ERROR)
        except Exception:
            pass
    return Text(raw, style=_ERROR)


def _event_line(ev) -> Text:
    """One emitted event, wake-style: `⚡ Name(field=value, …)`, or a dim raw
    marker when it couldn't be decoded (unknown program/discriminator)."""
    line = Text("  ")
    line.append("⚡ ", style=_EVENT)
    if isinstance(ev, UnknownEvent):
        line.append(f"<unknown event 0x{ev.discriminator.hex()}: {len(ev.data)} bytes>",
                    style=_PUNCT)
        return line
    line.append(type(ev).__name__, style=_EVENT)
    line.append("(", style=_PUNCT)
    for i, f in enumerate(dataclasses.fields(ev)):
        if i:
            line.append(", ", style=_PUNCT)
        line.append(f.name, style=_PUNCT)
        line.append("=", style=_PUNCT)
        line.append_text(_fmt_value(getattr(ev, f.name)))
    line.append(")", style=_PUNCT)
    return line


def _undecodable_header(node, exc: Exception) -> Text:
    """Header for a matched-but-undecodable node: refusal made visible (never a
    silently-empty decode), while the rest of the tree keeps rendering."""
    text = Text()
    text.append(resolve_label(node.program_id), style=_PROGRAM)
    text.append("::", style=_PUNCT)
    text.append(f"<undecodable: {exc}>", style=_UNKNOWN)
    return text


def _node_renderable(node) -> RenderableType:
    try:
        dec = decode_instruction(node.program_id, node.data, len(node.accounts))
        header = _header(node, dec)
        names = dec.account_names
    except Exception as exc:  # observable refusal at the node boundary
        header = _undecodable_header(node, exc)
        names = []

    # status glyph + cumulative compute units
    glyph, glyph_style = _STATUS.get(node.status, ("?", _PUNCT))
    header.append(" ")
    header.append(glyph, style=glyph_style)
    if node.compute_units is not None:
        header.append(f"  [{node.compute_units:,} CU]", style=_CU)

    lines = [
        _account_line(i, names[i] if i < len(names) else None, acc)
        for i, acc in enumerate(node.accounts)
    ]

    # msg! debug output — kept; error-explanation lines dropped on a failed node
    # (shown decoded on the ➞ line below, so they aren't duplicated).
    failed = node.status == "failed"
    logs = [
        _log_line(sub)
        for line in node.logs
        for sub in line.split("\n")
        if not (failed and _is_error_log(sub))
    ]

    # emitted events (emit! + hoisted emit_cpi!), wake-style `⚡` lines
    events = [_event_line(ev) for ev in node.events]

    # wake-style `➞` lines: decoded return value, then decoded error.
    tail = []
    ret = _decode_return(node)
    if ret is not None:
        t = Text("  ➞ ", style=_RETURN)
        t.append_text(ret)
        tail.append(t)
    err = _decode_error(node)
    if err is not None:
        t = Text("  ➞ ", style=_ERROR)
        t.append_text(err)
        tail.append(t)

    return Group(header, *lines, *logs, *events, *tail)


def _add_node(parent: Tree, node) -> None:
    branch = parent.add(_node_renderable(node))
    for child in node.inner:
        _add_node(branch, child)


def _any_failed(nodes) -> bool:
    return any(n.status == "failed" or _any_failed(n.inner) for n in nodes)


def _rich(trace) -> Tree:
    """Build the `rich.Tree` for a native `CallTrace` (its `__rich__` target)."""
    status = Text()
    status.append(
        "✓ " if trace.success else "✗ ",
        style="bold green" if trace.success else "bold red",
    )
    status.append("Transaction", style="bold")
    status.append(f"  {trace.compute_units_consumed:,} CU", style=_PUNCT)

    # The decoded error lives on the failing node's `➞` line. Repeat the
    # structured tx error at the root ONLY as a fallback — when no node captured
    # the failure (a tx-level error, or a truncated stream) — so it's never lost.
    if not trace.success and trace.error and not _any_failed(trace.instructions):
        status.append(f"  {trace.error}", style=_ERROR)

    tree = Tree(status)
    for node in trace.instructions:
        _add_node(tree, node)
    return tree


def _to_str(trace) -> str:
    """Plain-text render of a native `CallTrace` (its `__str__` target)."""
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=100).print(_rich(trace))
    return buf.getvalue()
