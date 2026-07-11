"""Runtime contract for decoding instruction data into named instructions/args.

Every program — built-in (hand-written, in ``_programs``) or generated from an
IDL (Phase 1b) — registers a :class:`ProgramInterface` into the global
``REGISTRY`` keyed by base58 program id. The call-trace renderer calls
:func:`decode_instruction` with a node's program id, data, and account count to
get a :class:`DecodedInstruction` (instruction name, named args, per-slot
account role names).

Instruction dispatch is a longest-match over each instruction's declared
discriminator bytes, which covers Anchor (8-byte), System (4-byte LE u32),
SPL Token (1-byte tag, plus Token-2022's tag+subtag), and Borsh enums (1-byte)
uniformly. A program with a non-prefix dispatch can override :meth:`decode`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass(frozen=True)
class DecodedInstruction:
    """The decoded view of one instruction in a call trace."""

    program_name: Optional[str]
    """Human name of the program, if known (e.g. ``"System Program"``)."""
    name: Optional[str]
    """Instruction name (e.g. ``"transfer"``), or ``None`` if undecodable."""
    args: dict
    """Decoded named arguments, in declaration order."""
    account_names: list
    """Role name per account slot (``list[str | None]``), aligned to accounts."""


@dataclass
class InstructionDef:
    """One instruction's discriminator, account roles, and decoders."""

    name: str
    discriminator: bytes
    account_names: list
    #: ``bytes after the discriminator -> {arg_name: value}``.
    decode_args: Callable[[bytes], dict] = field(default=lambda _data: {})
    #: ``raw return-data bytes -> decoded value`` for the IDL ``returns`` type;
    #: ``None`` when the instruction declares no return type. Strict (raises on a
    #: length/validation mismatch — see :func:`decode_return_value`).
    decode_return: Optional[Callable[[bytes], object]] = None


class ProgramInterface:
    """A decodable program: its id, display name, and instruction set."""

    def __init__(self, program_id: str, name: str) -> None:
        self.program_id = program_id
        self.name = name
        self._defs: list[InstructionDef] = []
        self._events: dict[bytes, type] = {}   # 8-byte disc -> event class

    def add(
        self,
        name: str,
        discriminator: bytes,
        account_names: list,
        decode_args: Optional[Callable[[bytes], dict]] = None,
        decode_return: Optional[Callable[[bytes], object]] = None,
    ) -> "ProgramInterface":
        """Register one instruction; returns self for chaining."""
        self._defs.append(
            InstructionDef(
                name,
                discriminator,
                account_names,
                decode_args or (lambda _data: {}),
                decode_return,
            )
        )
        return self

    def _match(self, data: bytes) -> Optional[InstructionDef]:
        """The instruction whose discriminator is the longest prefix of `data`."""
        best: Optional[InstructionDef] = None
        for d in self._defs:
            disc = d.discriminator
            if data[: len(disc)] == disc:
                if best is None or len(disc) > len(best.discriminator):
                    best = d
        return best

    def add_refusing(
        self, name: str, discriminator: bytes, reason: str
    ) -> "ProgramInterface":
        """Register an instruction that dispatches but **refuses** to decode
        (e.g. non-borsh serialization with no built-in override). The refusal is
        observable — decoding it raises rather than yielding a plausible empty
        result."""

        def _refuse(_data: bytes) -> dict:
            from ._codec import RefuseToDecode

            raise RefuseToDecode(reason)

        self._defs.append(InstructionDef(name, discriminator, [], _refuse))
        return self

    def add_event(self, discriminator: bytes, cls: type) -> "ProgramInterface":
        """Register an event class by its 8-byte discriminator."""
        self._events[bytes(discriminator)] = cls
        return self

    def decode_event(self, data: bytes):
        """Decode one `Program data:` / `emit_cpi!` payload (`disc ‖ Borsh`) to a
        registered event instance, or ``None`` if the discriminator is unknown or
        the body doesn't decode (caller surfaces raw)."""
        data = bytes(data)
        cls = self._events.get(data[:8]) if len(data) >= 8 else None
        if cls is None:
            return None
        try:
            return cls.decode(data)   # BorshStruct.decode: verify disc + strict body
        except Exception:
            return None

    def decode(self, data: bytes, n_accounts: int) -> DecodedInstruction:
        d = self._match(data)
        if d is None:
            return DecodedInstruction(self.name, None, {}, [None] * n_accounts)
        # Refusal is observable: a matched-but-undecodable instruction raises
        # (BorshError / RefuseToDecode). The blanket ``except: args = {}`` that
        # hid refusals as a successful empty decode is intentionally gone.
        args = d.decode_args(data[len(d.discriminator) :])
        names = list(d.account_names[:n_accounts])
        names += [None] * (n_accounts - len(names))
        return DecodedInstruction(self.name, d.name, args, names)


class RefusingInterface(ProgramInterface):
    """A whole-program refusal stub (§9.8): the program hit a generation-time
    punt (non-borsh serialization, an unrepresentable type, a missing layout),
    so every decode **refuses loudly** with the recorded reason rather than
    producing a plausible-but-wrong result. It still registers, so one bad
    program never blocks generation of the others."""

    def __init__(self, program_id: str, name: str, reason: str) -> None:
        super().__init__(program_id, name)
        self.reason = reason

    def decode(self, data: bytes, n_accounts: int) -> DecodedInstruction:
        from ._codec import RefuseToDecode

        raise RefuseToDecode(self.reason)


#: Global program registry, keyed by base58 program id.
REGISTRY: dict[str, ProgramInterface] = {}


def register(iface: ProgramInterface) -> ProgramInterface:
    """Register (or replace) a program interface by its program id."""
    REGISTRY[iface.program_id] = iface
    return iface


def decode_instruction(program_id, data: bytes, n_accounts: int) -> DecodedInstruction:
    """Decode one instruction's data for a known program.

    Falls back to a name-only result (program name resolved from the label
    table if it is a well-known program) when the program is not registered.
    """
    pid = str(program_id)
    iface = REGISTRY.get(pid)
    if iface is None:
        from ._labels import program_name

        return DecodedInstruction(program_name(pid), None, {}, [None] * n_accounts)
    return iface.decode(bytes(data), n_accounts)


class ReturnDataError(Exception):
    """Raised by ``TransactionResult.return_value`` when the transaction's
    return data cannot be decoded to the IDL ``returns`` type: the setting
    program has no generated interface, the return data could not be attributed
    to an instruction, the matched instruction declares no return type, or the
    bytes fail a strict decode against it. ``raw_return_value`` always still
    carries the bytes, and ``decode_return(T)`` lets you decode against a type
    you name explicitly."""


def decode_return_value(program_id, ix_data, raw: bytes) -> object:
    """Best-effort decode of a transaction's return-data bytes to the IDL
    ``returns`` type of the instruction that produced them.

    ``ix_data`` is the (discriminator-bearing) instruction data of the setting
    program's invocation, used to identify *which* instruction's return type to
    use; ``None`` when the runtime could not attribute the return data to one.
    Decoding is **strict** — a wrong guess fails validation and raises rather
    than fabricating a plausible-but-wrong value (refuse-don't-guess). Raise on
    any failure; callers fall back to ``raw_return_value``.
    """
    pid = str(program_id)
    iface = REGISTRY.get(pid)
    if iface is None:
        raise ReturnDataError(
            f"return data set by program {pid}, which has no generated "
            "interface; use raw_return_value")
    if ix_data is None:
        raise ReturnDataError(
            f"return data of {pid} could not be attributed to an instruction; "
            "use raw_return_value")
    d = iface._match(bytes(ix_data))
    if d is None:
        raise ReturnDataError(
            f"no instruction of {pid} matched the return-data setter; "
            "use raw_return_value")
    if d.decode_return is None:
        raise ReturnDataError(
            f"instruction {d.name!r} declares no return type; use raw_return_value")
    try:
        return d.decode_return(bytes(raw))
    except ReturnDataError:
        raise
    except Exception as e:  # BorshError / RefuseToDecode / anything the codec raises
        raise ReturnDataError(
            f"return data did not decode as {d.name!r}'s return type: {e}") from e


class UnknownEvent:
    """An emitted event we can't decode — the program isn't generated, the
    discriminator is unregistered, or the body didn't validate. Carries the raw
    payload so nothing is lost (refuse-don't-guess; never a fabricated event)."""

    __slots__ = ("program_id", "discriminator", "data")

    def __init__(self, program_id: str, data: bytes) -> None:
        self.program_id = program_id
        self.data = bytes(data)
        self.discriminator = self.data[:8]

    def __repr__(self) -> str:
        return (f"<unknown event 0x{self.discriminator.hex()}: "
                f"{len(self.data)} bytes>")


def decode_events(program_id, raw_payloads) -> list:
    """Decode a node's emitted event payloads (each `disc ‖ Borsh`) using the
    emitting program's event table, matched by `program_id`. Unknown program /
    discriminator / decode failure yields an :class:`UnknownEvent` (raw), so the
    result list always aligns 1:1 with the payloads and nothing is dropped."""
    pid = str(program_id)
    iface = REGISTRY.get(pid)
    out = []
    for raw in raw_payloads:
        raw = bytes(raw)
        ev = iface.decode_event(raw) if iface is not None else None
        out.append(ev if ev is not None else UnknownEvent(pid, raw))
    return out


def decode_return_as(ty, raw) -> object:
    """Decode raw return-data bytes against an explicitly-supplied type ``ty``
    (any annotation the codec accepts — a width alias, a generated struct/enum,
    ``Optional``/``list``/…). No attribution heuristic; strict decode. Raises
    :class:`ReturnDataError` if there is no return data or it does not validate."""
    if raw is None:
        raise ReturnDataError("transaction set no return data")
    from ._codec import compile_field
    from ._codec.builder import make_returns_decoder

    try:
        decode = make_returns_decoder(compile_field(ty, "<returns>"))
        return decode(bytes(raw))
    except ReturnDataError:
        raise
    except Exception as e:
        raise ReturnDataError(f"return data did not decode as {ty!r}: {e}") from e
