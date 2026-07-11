"""Type stubs for the native (Rust/pyo3) extension `solana_fuzzer._native`."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Generic, TypeAlias, overload

from typing_extensions import TypeVar, deprecated

from solana_fuzzer._codec import u64
from solana_fuzzer._errors import TransactionFailed
from solana_fuzzer._precompiles import PrecompileInstruction, SignedMessage

#: The decoded-return-value type carried by `Instruction` / `TransactionResult`.
#: Covariant (so `Instruction[u64]` is-an `Instruction[object]` — required for the
#: variadic `tx`/`simulate`) and defaults to `object`, so a bare `Instruction` /
#: `TransactionResult` (built-ins, multi-instruction / raw-bytes paths) behaves
#: exactly as before.
_R_co = TypeVar("_R_co", covariant=True, default=object)
#: Invariant inference var for the single-instruction `tx`/`simulate` overloads.
_R = TypeVar("_R")
_T = TypeVar("_T")

AddressLike: TypeAlias = "str | bytes | int | Pubkey | Account"
"""Any value accepted where an address is expected."""

MetaLike: TypeAlias = "AccountMeta | AddressLike"
"""An `AccountMeta`, or an address-like value (coerced to a read-only meta)."""

class Pubkey:
    """A 32-byte Solana address."""

    def __init__(self, value: AddressLike) -> None:
        """Construct from a base58 `str`, 32 raw `bytes`, an `int` (big-endian,
        32 bytes), another `Pubkey`, or an `Account` (its address)."""
        ...

    def to_bytes(self) -> bytes:
        """Return the address as its 32 raw bytes."""
        ...

    def __str__(self) -> str:
        """Return the base58 encoding of the address."""
        ...

    def __repr__(self) -> str:
        """Return a `Pubkey('<base58>')` representation."""
        ...

    def __eq__(self, other: object) -> bool:
        """Two pubkeys are equal when their 32 bytes match."""
        ...

    def __hash__(self) -> int:
        """Hash of the address bytes, so pubkeys work as dict keys / set members."""
        ...

    @staticmethod
    def find_program_address(
        seeds: Sequence[bytes], program_id: AddressLike
    ) -> tuple[Pubkey, int]:
        """Derive a PDA from `seeds` and `program_id`, searching for the
        canonical bump seed. Returns `(address, bump)`."""
        ...

    @staticmethod
    def create_program_address(
        seeds: Sequence[bytes], program_id: AddressLike
    ) -> Pubkey:
        """Derive a PDA from explicit `seeds` (no bump search). Raises if the
        result lands on the ed25519 curve."""
        ...

class AccountMeta:
    """Per-account metadata in an instruction: address + signer/writable flags."""

    is_signer: bool
    """Whether this account must sign the transaction."""
    is_writable: bool
    """Whether the instruction may write to this account."""

    def __init__(
        self,
        pubkey: AddressLike,
        is_signer: bool = ...,
        is_writable: bool = ...,
    ) -> None:
        """Build a meta for `pubkey`; defaults to a non-signer, read-only account."""
        ...

    @property
    def pubkey(self) -> Pubkey:
        """The account's address."""
        ...

    @pubkey.setter
    def pubkey(self, value: AddressLike) -> None: ...
    def __eq__(self, other: object) -> bool:
        """Equal when address, signer flag, and writable flag all match."""
        ...

    def __repr__(self) -> str:
        """Return an `AccountMeta(<pubkey>, signer=..., writable=...)` representation."""
        ...

def signer(account: MetaLike) -> AccountMeta:
    """Mark an account as a signer (preserving its writable flag)."""
    ...

def writable(account: MetaLike) -> AccountMeta:
    """Mark an account as writable (preserving its signer flag)."""
    ...

def readonly(account: MetaLike) -> AccountMeta:
    """Mark an account as read-only (preserving its signer flag)."""
    ...

def writable_signer(account: MetaLike) -> AccountMeta:
    """Mark an account as a writable signer."""
    ...

class Instruction(Generic[_R_co]):
    """A program invocation: program id, account metas, and a data payload.

    Generic in the decoded return type of the instruction that produced it: a
    generated builder for an instruction with an IDL `returns` type returns
    `Instruction[T]`, and running exactly one such instruction through
    `Account.tx` / `Account.simulate` yields `TransactionResult[T]`. A bare
    `Instruction` (built-ins, or any instruction without a return type) is
    `Instruction[object]`."""

    def __init__(
        self,
        program_id: AddressLike,
        accounts: Sequence[MetaLike] = ...,
        data: bytes = ...,
    ) -> None:
        """Build an instruction for `program_id`. Each account may be an
        `AccountMeta` or a bare address (coerced to a non-signer, read-only
        meta); `data` is the opaque instruction payload."""
        ...

    @property
    def program_id(self) -> Pubkey:
        """The program this instruction invokes."""
        ...

    @program_id.setter
    def program_id(self, value: AddressLike) -> None: ...
    @property
    def accounts(self) -> list[AccountMeta]:
        """The account metas the instruction references, in order."""
        ...

    @accounts.setter
    def accounts(self, value: Sequence[MetaLike]) -> None: ...
    @property
    def data(self) -> bytes:
        """The opaque instruction data payload."""
        ...

    @data.setter
    def data(self, value: bytes) -> None: ...
    def __repr__(self) -> str:
        """Summarize the program id, account count, and data length."""
        ...

class TransactionResult(Generic[_R_co]):
    """Outcome of a transaction. Returned only on **success** — a failed send /
    simulate raises the resolved `TransactionFailed` instead (its `.tx` links back
    to the failed receipt). See `solana_fuzzer._errors`.

    Generic in the decoded `return_value` type. You get a precise `_R` only from
    running exactly one instruction that declares an IDL `returns` type (via the
    single-instruction `Account.tx` / `Account.simulate` overloads); every other
    path — `send_transaction(bytes)`, multi-instruction txs, `airdrop` — is
    `TransactionResult[object]`."""

    @property
    def success(self) -> bool:
        """Whether the transaction executed without error (`True` for a returned
        result; `False` on the receipt reached via `TransactionFailed.tx`)."""
        ...

    @property
    def signature(self) -> bytes | None:
        """The transaction signature, or `None` (e.g. for simulations).

        When the sending SVM has both `sigverify` and `transaction_history` off,
        signatures are cosmetic (never verified, never used as a dedup key) and are
        left unsigned to save the ed25519 work, so this is the all-zero placeholder
        rather than a real signature."""
        ...

    @property
    def logs(self) -> list[str]:
        """Program log messages emitted during execution."""
        ...

    @property
    def compute_units_consumed(self) -> int:
        """Total compute units the transaction consumed."""
        ...

    @property
    def error(self) -> TransactionFailed | None:
        """The `TransactionFailed` exception for this outcome, or `None` on
        success. Built once and cached: for a failed receipt reached via
        `exc.tx`, `exc.tx.error is exc`."""
        ...

    @property
    def raw_return_value(self) -> bytes | None:
        """The raw return-data bytes (tx-wide, last-writer-wins), or `None` if the
        transaction set no return data. Always available regardless of decoding."""
        ...

    @property
    def return_program_id(self) -> Pubkey | None:
        """The program that set the return data, or `None` if there was none."""
        ...

    @property
    def return_value(self) -> _R_co:
        """The **decoded** return value, per the setting instruction's IDL
        `returns` type; `None` if the transaction set no return data. Statically
        typed (`_R`) when this result came from a single-instruction
        `tx`/`simulate` over an instruction that declares a return type; otherwise
        `object`.

        Best-effort on the low-level path: the setting program is matched via the
        call trace and the bytes are strictly decoded. Raises `ReturnDataError`
        (from `solana_fuzzer`) if the program has no generated interface, the
        return data can't be attributed to an instruction, the instruction
        declares no return type, or the bytes don't validate — use
        `raw_return_value` for the bytes, or `decode_return(T)` to name the type."""
        ...

    @overload
    def decode_return(self, ty: type[_T]) -> _T: ...
    @overload
    def decode_return(self, ty: object) -> object: ...
    def decode_return(self, ty: object) -> object:
        """Decode the return data against an explicitly-named type `ty` (any
        annotation the codec accepts — a width alias, generated struct/enum,
        `Optional`/`list`/…). No attribution heuristic; strict. Raises
        `ReturnDataError` if there is no return data or it does not decode as `ty`."""
        ...

    @property
    def events(self) -> list:
        """All decoded events emitted during the transaction (`emit!` +
        `emit_cpi!`), flattened pre-order across the call tree — the assertion
        surface (`assert Trade(...) in result.events`). `UnknownEvent` for events
        whose program/discriminator isn't generated."""
        ...

    @property
    def call_trace(self) -> CallTrace:
        """The execution as a `CallTrace`: a sequence of top-level
        `TracedInstruction`s (each carrying its CPIs) that also renders itself.
        Empty for results without a known message (e.g. `airdrop`)."""
        ...

    def __repr__(self) -> str:
        """Summarize success (with compute units) or failure (with the error)."""
        ...

class CallTrace:
    """A transaction's call trace: a sequence over the top-level
    `TracedInstruction`s that is also a `rich` renderable (and `str()`-able)."""

    @property
    def success(self) -> bool:
        """Whether the transaction executed without error."""
        ...

    @property
    def error(self) -> str | None:
        """The failure description, or `None` on success."""
        ...

    @property
    def compute_units_consumed(self) -> int:
        """Total compute units the transaction consumed."""
        ...

    @property
    def instructions(self) -> list[TracedInstruction]:
        """The top-level instructions, in order."""
        ...

    def __len__(self) -> int: ...
    def __getitem__(self, index: int) -> TracedInstruction: ...
    def __str__(self) -> str: ...
    def __repr__(self) -> str: ...

class TracedInstruction:
    """One node of a transaction's call tree: a program invocation with its
    resolved accounts, data, depth, and nested CPIs."""

    @property
    def program_id(self) -> Pubkey:
        """The program this instruction invokes."""
        ...

    @property
    def accounts(self) -> list[AccountMeta]:
        """The account slots, in instruction order, as `AccountMeta`s (pubkey
        plus the signer/writable privileges this instruction saw)."""
        ...

    @property
    def data(self) -> bytes:
        """The opaque instruction data payload."""
        ...

    @property
    def stack_height(self) -> int:
        """Invocation depth: 1 for a top-level instruction, >= 2 for a CPI."""
        ...

    @property
    def logs(self) -> list[str]:
        """Raw program-log lines emitted **directly** by this invocation, in
        order (its own frame only — children carry their own; the runtime's
        `invoke`/`consumed`/`success`/`failed`/`return`/`data` markers are
        excluded — those surface as status/compute_units/return/events)."""
        ...

    @property
    def status(self) -> str:
        """This invocation's outcome, best-effort from the logs: `"success"`,
        `"failed"`, or `"unknown"` (frame never closed — e.g. truncated logs)."""
        ...

    @property
    def compute_units(self) -> int | None:
        """Cumulative compute units for this frame (incl. its CPIs), or `None` if
        the `consumed` marker wasn't in the (possibly truncated) log stream."""
        ...

    @property
    def error(self) -> str | None:
        """The raw `failed: <msg>` runtime text if this frame failed, else `None`."""
        ...

    @property
    def raw_return_value(self) -> bytes | None:
        """This frame's return-data bytes (`set_return_data`), or `None`."""
        ...

    @property
    def events(self) -> list:
        """This frame's decoded events (`emit!` + hoisted `emit_cpi!`);
        `UnknownEvent` for an unregistered program/discriminator."""
        ...

    @property
    def events_raw(self) -> list[bytes]:
        """Raw event payloads (each `disc ‖ Borsh`) emitted by this frame, in order."""
        ...

    @property
    def inner(self) -> list[TracedInstruction]:
        """The CPIs this instruction made, in order."""
        ...

    def __repr__(self) -> str: ...

class System:
    """Builder namespace for System Program instructions (via `svm.system`).

    For each account slot: a bare address gets the instruction's required
    signer/writable flags; an explicit `AccountMeta` is used verbatim.
    """

    def transfer(
        self, from_: MetaLike, to: MetaLike, lamports: int
    ) -> Instruction:
        """Transfer `lamports` from `from_` to `to`."""
        ...

    def create_account(
        self,
        lamports: int,
        *,
        from_: MetaLike,
        to: MetaLike,
        owner: AddressLike,
        space: int,
    ) -> Instruction:
        """Create account `to`, funded by `from_`, owned by `owner`, with `space`
        bytes of data, seeded with `lamports` lamports (use
        `svm.minimum_balance_for_rent_exemption(space)` for the rent-exempt
        minimum)."""
        ...

    def assign(self, account: MetaLike, owner: AddressLike) -> Instruction:
        """Reassign `account` to the owning program `owner`."""
        ...

    def allocate(self, account: MetaLike, space: int) -> Instruction:
        """Allocate `space` bytes of data for `account`."""
        ...

class Token:
    """Builder namespace for SPL Token instructions (via `svm.token`).

    For each account slot: a bare address gets the instruction's required
    signer/writable flags; an explicit `AccountMeta` is used verbatim. The
    same builders serve classic Token and Token-2022 (the program ID differs).
    """

    @property
    def program_id(self) -> Pubkey:
        """The program ID this builder targets (classic Token, or Token-2022)."""
        ...

    def initialize_mint2(
        self,
        mint: MetaLike,
        mint_authority: AddressLike,
        decimals: int,
        freeze_authority: AddressLike | None = ...,
    ) -> Instruction:
        """Initialize `mint` with `decimals`. The mint authority and optional
        freeze authority are written into the mint data, not passed as
        account slots."""
        ...

    def initialize_account3(
        self, account: MetaLike, mint: MetaLike, owner: AddressLike
    ) -> Instruction:
        """Initialize the token `account` to hold `mint`, owned by `owner`."""
        ...

    def mint_to_checked(
        self,
        mint: MetaLike,
        account: MetaLike,
        authority: MetaLike,
        amount: int,
        decimals: int,
    ) -> Instruction:
        """Mint `amount` base units of `mint` into the token `account`, signed by
        `authority`; verifies `decimals` matches the mint."""
        ...

    def transfer_checked(
        self,
        source: MetaLike,
        mint: MetaLike,
        destination: MetaLike,
        authority: MetaLike,
        amount: int,
        decimals: int,
    ) -> Instruction:
        """Transfer `amount` base units from `source` to `destination`, signed
        by `authority`; verifies `mint` and `decimals`."""
        ...

    @deprecated("unchecked transfer; prefer transfer_checked")
    def transfer(
        self,
        source: MetaLike,
        destination: MetaLike,
        authority: MetaLike,
        amount: int,
    ) -> Instruction:
        """Transfer `amount` base units from `source` to `destination`, signed
        by `authority`. Unchecked — prefer `transfer_checked`."""
        ...

    def burn_checked(
        self,
        account: MetaLike,
        mint: MetaLike,
        authority: MetaLike,
        amount: int,
        decimals: int,
    ) -> Instruction:
        """Burn `amount` base units of `mint` from `account`, signed by
        `authority`; verifies `decimals`."""
        ...

    def close_account(
        self, account: MetaLike, destination: MetaLike, owner: MetaLike
    ) -> Instruction:
        """Close `account`, sending its rent lamports to `destination`; signed
        by `owner`."""
        ...

    def approve(
        self, source: MetaLike, delegate: MetaLike, authority: MetaLike, amount: int
    ) -> Instruction:
        """Delegate up to `amount` base units of `source` to `delegate`, signed
        by `authority` (the source account's owner)."""
        ...

    def revoke(self, source: MetaLike, authority: MetaLike) -> Instruction:
        """Revoke any existing delegation on `source`, signed by `authority`
        (the source account's owner)."""
        ...

    def create_ata(
        self, funder: AddressLike, owner: AddressLike, mint: AddressLike
    ) -> Instruction:
        """Create the associated token account for `owner` + `mint`, funded by
        `funder` (targets the Associated Token Account program)."""
        ...

    def ata_address(self, owner: AddressLike, mint: AddressLike) -> Pubkey:
        """Derive the associated token account address for `owner` + `mint`."""
        ...

class AddressLookupTable:
    """Builder namespace for the official Address Lookup Table program
    instructions (via `svm.address_lookup_table`). These construct the real
    on-chain instructions — **not** cheatcodes; they carry the program's real
    constraints (a recent slot present in `SlotHashes`, authority signatures, and
    a one-slot warmup before a table's addresses become usable). To skip all that
    in a test, use the `svm.create_lookup_table` cheatcode instead."""

    def create(
        self, authority: AddressLike, payer: AddressLike, recent_slot: int | None = ...
    ) -> tuple[Instruction, Pubkey]:
        """`CreateLookupTable`: returns `(instruction, table_address)`.
        `recent_slot` must be present in the `SlotHashes` sysvar (defaults to the
        SVM's current slot); the table address is derived from `authority` +
        `recent_slot`. `authority` and `payer` must sign."""
        ...

    def extend(
        self,
        table: AddressLike,
        authority: AddressLike,
        addresses: Sequence[AddressLike],
        payer: AddressLike | None = ...,
    ) -> Instruction:
        """`ExtendLookupTable`: append `addresses` to `table` (signed by
        `authority`; `payer` funds any rent increase and must sign)."""
        ...

    def deactivate(self, table: AddressLike, authority: AddressLike) -> Instruction:
        """`DeactivateLookupTable`: begin deactivating `table` (signed by `authority`)."""
        ...

    def close(
        self, table: AddressLike, authority: AddressLike, recipient: AddressLike
    ) -> Instruction:
        """`CloseLookupTable`: close a deactivated `table`, draining lamports to
        `recipient` (signed by `authority`)."""
        ...

    def freeze(self, table: AddressLike, authority: AddressLike) -> Instruction:
        """`FreezeLookupTable`: permanently freeze `table` so it can never be
        extended or closed again (signed by `authority`)."""
        ...

class Clock:
    """The `Clock` sysvar: cluster time and slot/epoch counters. Read via
    `svm.clock`, write (partially) via `svm.set_clock(...)`."""

    @property
    def slot(self) -> int: ...
    @property
    def epoch_start_timestamp(self) -> int: ...
    @property
    def epoch(self) -> int: ...
    @property
    def leader_schedule_epoch(self) -> int: ...
    @property
    def unix_timestamp(self) -> int: ...
    def __repr__(self) -> str: ...

class Rent:
    """The `Rent` sysvar parameters. Read via `svm.rent`, write via
    `svm.set_rent(...)`. (Prefer `svm.minimum_balance_for_rent_exemption` to
    compute a rent-exempt balance.)"""

    @property
    def lamports_per_byte_year(self) -> int: ...
    @property
    def exemption_threshold(self) -> float: ...
    @property
    def burn_percent(self) -> int: ...
    def __repr__(self) -> str: ...

class EpochSchedule:
    """The `EpochSchedule` sysvar. Read via `svm.epoch_schedule`, write via
    `svm.set_epoch_schedule(...)`."""

    @property
    def slots_per_epoch(self) -> int: ...
    @property
    def leader_schedule_slot_offset(self) -> int: ...
    @property
    def warmup(self) -> bool: ...
    @property
    def first_normal_epoch(self) -> int: ...
    @property
    def first_normal_slot(self) -> int: ...
    def __repr__(self) -> str: ...

class EpochRewards:
    """The `EpochRewards` sysvar (read-only; `svm.epoch_rewards`)."""

    @property
    def distribution_starting_block_height(self) -> int: ...
    @property
    def num_partitions(self) -> int: ...
    @property
    def parent_blockhash(self) -> bytes: ...
    @property
    def total_points(self) -> int: ...
    @property
    def total_rewards(self) -> int: ...
    @property
    def distributed_rewards(self) -> int: ...
    @property
    def active(self) -> bool: ...
    def __repr__(self) -> str: ...

class LiteSVM:
    """A litesvm-backed Solana virtual machine instance."""

    def __init__(
        self,
        *,
        sigverify: bool = ...,
        blockhash_check: bool = ...,
        transaction_history: bool = ...,
        activate: Sequence[AddressLike] = ...,
        deactivate: Sequence[AddressLike] = ...,
    ) -> None:
        """Create an SVM. `sigverify` enforces transaction signatures;
        `blockhash_check` enforces recent-blockhash validity (both on by default).
        `transaction_history` (on by default) keeps litesvm's per-signature dedup
        and `get_transaction` log; turn it off to allow duplicate transactions.

        The runtime starts from **mainnet-beta's feature set** (a pinned snapshot),
        so behavior matches mainnet. `activate` / `deactivate` are feature-gate
        pubkeys flipped on top of it — e.g. `deactivate=[X]` to test behavior
        before X activated, or `activate=[Y]` to test a pending feature. The
        feature set is fixed at construction (change it live via
        `activate_features` / `deactivate_features`)."""
        ...

    def reset(self) -> None:
        """**Cheatcode.** Wipe all accounts back to genesis, keeping the original config."""
        ...

    @property
    def system(self) -> System:
        """Builder namespace for System Program instructions."""
        ...

    @property
    def token(self) -> Token:
        """Builder namespace for SPL Token instructions."""
        ...

    @property
    def sigverify(self) -> bool:
        """Whether signature verification is enforced. Setting it toggles in
        place, preserving account state."""
        ...

    @sigverify.setter
    def sigverify(self, value: bool) -> None: ...
    @property
    def transaction_history(self) -> bool:
        """Whether the per-signature transaction-history dedup is on. When off,
        litesvm no longer raises `AlreadyProcessed` for a repeated transaction
        signature (byte-identical txs execute again) and `get_transaction(sig)`
        returns nothing. Setting it toggles in place, preserving account state;
        re-enabling restores the default history window. The fuzz engine disables
        it for a run so repeated identical actions aren't rejected.

        With this **and** `sigverify` both off, transaction signatures are neither
        verified nor used as a dedup key, so `tx`/`simulate` skip ed25519 signing
        entirely (a large win on the fuzz hot path) and leave every signature at
        the all-zero placeholder — see `TransactionResult.signature`."""
        ...

    @transaction_history.setter
    def transaction_history(self, value: bool) -> None: ...
    @property
    def blockhash_check(self) -> bool:
        """Whether recent-blockhash checking is enforced. Setting it toggles in
        place, preserving account state."""
        ...

    @blockhash_check.setter
    def blockhash_check(self, value: bool) -> None: ...
    def is_feature_active(self, feature: AddressLike) -> bool:
        """Whether the given feature-gate pubkey is active in this SVM's feature
        set (e.g. `if not svm.is_feature_active(feat): pytest.skip(...)`)."""
        ...

    def activate_features(self, *features: AddressLike) -> None:
        """**Cheatcode.** Activate feature-gate pubkeys on the live SVM, preserving
        account state (rebuilds the runtime under the new feature set and
        recompiles deployed programs). Mirrors a mainnet feature activation at an
        epoch boundary."""
        ...

    def deactivate_features(self, *features: AddressLike) -> None:
        """**Cheatcode.** Deactivate feature-gate pubkeys on the live SVM,
        preserving account state (as `activate_features`, in reverse)."""
        ...

    def airdrop(self, address: AddressLike, lamports: int) -> TransactionResult:
        """**Cheatcode.** Credit `lamports` to `address` (mints lamports),
        returning the resulting transaction."""
        ...

    def set_account(
        self,
        address: AddressLike,
        *,
        lamports: int = ...,
        data: bytes = ...,
        owner: AddressLike | None = ...,
        executable: bool = ...,
        rent_epoch: int = ...,
    ) -> None:
        """**Cheatcode.** Overwrite the account at `address` with the given fields. Any omitted
        field takes its default (zero lamports, empty data, System Program
        owner, etc.)."""
        ...

    def latest_blockhash(self) -> bytes:
        """Return the current blockhash (32 bytes)."""
        ...

    def expire_blockhash(self) -> None:
        """**Cheatcode.** Advance past the current blockhash so it is no longer accepted."""
        ...

    def warp_to_slot(self, slot: int) -> None:
        """**Cheatcode.** Jump the clock forward to `slot`."""
        ...

    def minimum_balance_for_rent_exemption(self, data_len: int) -> int:
        """Return the lamports an account of `data_len` bytes needs to be
        rent-exempt."""
        ...

    # --- sysvars ---------------------------------------------------------- #
    # Reads via litesvm `get_sysvar`; writes via `set_sysvar` (updates the
    # cached sysvar the runtime uses, not just the backing account). Setters are
    # partial: only the given keywords change, the rest keep their value.

    @property
    def clock(self) -> Clock:
        """The current `Clock` sysvar (slot, epoch, unix_timestamp, …)."""
        ...

    def set_clock(self, *, slot: int | None = ..., epoch: int | None = ...,
                  epoch_start_timestamp: int | None = ...,
                  leader_schedule_epoch: int | None = ...,
                  unix_timestamp: int | None = ...) -> None:
        """**Cheatcode.** Override the given `Clock` fields (others unchanged)."""
        ...

    def warp_to_timestamp(self, unix_timestamp: int) -> None:
        """**Cheatcode.** Set only the clock's `unix_timestamp` (block time)."""
        ...

    @property
    def rent(self) -> Rent:
        """The current `Rent` sysvar parameters."""
        ...

    def set_rent(self, *, lamports_per_byte_year: int | None = ...,
                 exemption_threshold: float | None = ...,
                 burn_percent: int | None = ...) -> None:
        """**Cheatcode.** Override the given `Rent` fields (others unchanged)."""
        ...

    @property
    def epoch_schedule(self) -> EpochSchedule:
        """The current `EpochSchedule` sysvar."""
        ...

    def set_epoch_schedule(self, *, slots_per_epoch: int | None = ...,
                           leader_schedule_slot_offset: int | None = ...,
                           warmup: bool | None = ...,
                           first_normal_epoch: int | None = ...,
                           first_normal_slot: int | None = ...) -> None:
        """**Cheatcode.** Override the given `EpochSchedule` fields (others unchanged)."""
        ...

    @property
    def last_restart_slot(self) -> int:
        """The `LastRestartSlot` sysvar value."""
        ...

    def set_last_restart_slot(self, slot: int) -> None:
        """**Cheatcode.** Set the `LastRestartSlot` sysvar."""
        ...

    @property
    def epoch_rewards(self) -> EpochRewards:
        """The current `EpochRewards` sysvar (read-only)."""
        ...

    @property
    def slot_hashes(self) -> list[tuple[int, bytes]]:
        """The `SlotHashes` sysvar as `[(slot, hash_bytes), …]` (read-only)."""
        ...

    def add_program_from_file(self, program_id: AddressLike, path: str) -> None:
        """**Cheatcode.** Deploy a BPF program from the `.so` file at `path` at
        `program_id`, bypassing the loader / upgrade-authority flow."""
        ...

    def add_program(self, program_id: AddressLike, bytes: bytes) -> None:
        """**Cheatcode.** Deploy a BPF program from raw ELF `bytes` at
        `program_id`, bypassing the loader / upgrade-authority flow."""
        ...

    @property
    def address_lookup_table(self) -> AddressLookupTable:
        """Builder namespace for the official Address Lookup Table program
        instructions (create / extend / deactivate / close / freeze). These go
        through the real ALT program — **not** a cheatcode; to just get a
        ready-to-use table, prefer the `create_lookup_table` cheatcode."""
        ...

    def create_lookup_table(
        self,
        addresses: Sequence[AddressLike],
        *,
        address: AddressLike | None = ...,
        authority: AddressLike | None = ...,
    ) -> Pubkey:
        """**Cheatcode.** Inject a ready-to-use Address Lookup Table directly (via
        `set_account`), bypassing the ALT program's create/extend flow and its
        recent-slot + one-slot-warmup + authority requirements. The returned table
        is immediately active and referenceable by v0 transactions — which cannot
        happen on a real chain. `addresses` are the entries the table resolves;
        `address` optionally fixes the table's own address (default: fresh);
        `authority` sets the modification authority (default: System — irrelevant
        for pure lookups). Returns the table address. For the faithful path use
        `svm.address_lookup_table`."""
        ...

    def fork(
        self,
        url: str | None = ...,
        *,
        exclude: Sequence[AddressLike] = ...,
        cache: str | None = ...,
        offline: bool = ...,
    ) -> None:
        """**Cheatcode.** Enable **mainnet forking**: dependency accounts hydrate from `url` (a
        Solana RPC endpoint) on first touch — the full account set of each
        transaction, plus any account read directly — and are cached under
        `cache` (default `./fork-cache`) for deterministic offline replay.

        `exclude` lists program IDs to treat as *not on mainnet*: their bytecode
        is not forked (deploy your own build with `add_program`) and any account
        they **own** starts blank, so an audited program reinitializes from
        scratch while its real dependencies are forked around it.

        With `offline=True` (or no `url`) nothing touches the network: state is
        replayed from the snapshot and a cache miss is a hard error. Locally-set
        accounts always win over the fork (`set_account` / `add_program` are never
        overwritten). Forking uses the SVM's own feature set (mainnet parity by
        default; diverge via the constructor's `activate`/`deactivate`) — it is
        *not* fetched from the forked chain. See `design/forking-spec/`."""
        ...

    def unfork(self) -> None:
        """Disable mainnet forking: drop the fork configuration so nothing further
        hydrates from RPC/snapshot. It **stops** forking, it does not **undo** it —
        already-hydrated accounts are **not** wiped and the seeded clock/blockhash
        are **not** reverted (both are `reset()`'s job); the on-disk snapshot cache
        is untouched. So `unfork()` alone *freezes* the current forked state (handy
        to hydrate what you need, then run RPC-free); `unfork()` then `reset()`
        gives a clean, unforked SVM. (`reset()` alone keeps forking on.)"""
        ...

    def fork_programs(self, *programs: AddressLike) -> int:
        """**Cheatcode.** Pre-fetch on-chain **programs** into the fork by id, without
        running a transaction: each id's program account and (for upgradeable
        programs) its programdata hydrate from the snapshot cache → RPC, exactly as
        lazy hydration would on first touch. Use it to *pin* the program set a test
        needs so a later `offline=True` replay has no network dependency: run the
        scenario once online, read `forked_accounts()` to see which programs it
        touched, then freeze that list here. Raises if an id is not an executable
        program on chain (absent, or a non-program account), so a wrong id fails
        loudly rather than silently under-forking. Requires forking to be enabled;
        returns the number of programs pinned."""
        ...

    def forked_accounts(self) -> list[Account]:
        """The on-chain accounts hydrated into this fork so far, as read-only
        `Account` views — every address the fork has resolved to a *present*
        account this session, ordered by address. Excludes confirmed-absent and
        owner-blanked addresses, and your own locally-set accounts (not
        fork-sourced). Empty when forking is off. Pair with `fork_programs` to
        freeze a discovered program set for offline replay:
        `svm.fork_programs(*(a.pubkey for a in svm.forked_accounts() if a.executable))`."""
        ...

    def send_transaction(self, tx_bytes: bytes) -> TransactionResult:
        """Send a serialized `VersionedTransaction` (bincode bytes), committing
        any resulting state changes."""
        ...

    def simulate_transaction(self, tx_bytes: bytes) -> TransactionResult:
        """Simulate a serialized `VersionedTransaction` (bincode bytes) without
        committing state changes."""
        ...

class Account:
    """A handle to the account **at an address** in a `LiteSVM` — addressed by
    its `pubkey`, and the lens through which you read that account's state and
    (when it can sign) act as fee payer / signer.

    Two things the name "Account" elides:

    * **It need not exist on-chain.** Every address has an account *slot*;
      "doesn't exist" just means empty / not-yet-created. An `Account` is a
      handle to that slot, present or not — check `exists`; reads like
      `lamports` / `data` raise until it's funded (`svm.airdrop` /
      `svm.set_account`).
    * **It may or may not hold a private key.** It carries a keypair (so it can
      sign / pay — `can_sign`, `tx`, `simulate`) only when made via
      `Account.new()` / `Account.from_secret()`. A bare-address view
      (`Account(pubkey)`) or a derived PDA has none and is read/inspect only.

    Holds no data of its own — every read delegates to the bound SVM, so the
    view is always current.
    """

    def __init__(self, address: AddressLike, svm: LiteSVM | None = ...) -> None:
        """View the account at `address` — no keypair, cannot sign. Binds the
        given SVM, or the process-global default if `svm` is omitted."""
        ...

    @staticmethod
    def new(svm: LiteSVM | None = ...) -> Account:
        """Create an account backed by a freshly generated keypair — can sign.
        The keypair is derived from the harness's global RNG
        (`solana_fuzzer.random`), not OS entropy, so it is reproducible from the
        base `--seed`."""
        ...

    @staticmethod
    def from_secret(secret: bytes, svm: LiteSVM | None = ...) -> Account:
        """Create an account from a known 64-byte secret key — can sign."""
        ...

    @staticmethod
    def find_program_address(
        seeds: Sequence[bytes],
        program_id: AddressLike,
        svm: LiteSVM | None = ...,
    ) -> tuple[Account, int]:
        """Derive a PDA view (no keypair) and its canonical bump seed. Returns
        `(account, bump)`."""
        ...

    @staticmethod
    def create_program_address(
        seeds: Sequence[bytes],
        program_id: AddressLike,
        svm: LiteSVM | None = ...,
    ) -> Account:
        """Derive a PDA view (no keypair) from explicit `seeds` (no bump
        search); raises if the result lands on the ed25519 curve."""
        ...

    @property
    def pubkey(self) -> Pubkey:
        """The account's address."""
        ...

    @property
    def label(self) -> str | None:
        """The identity label explicitly assigned to this account, or `None`.
        The always-a-string display name (resolving well-known programs and
        falling back to a truncated address) is `str(account)`."""
        ...

    @label.setter
    def label(self, value: str) -> None:
        """Assign an identity label to this account's address."""
        ...

    @property
    def svm(self) -> LiteSVM:
        """The `LiteSVM` instance this view is bound to."""
        ...

    @property
    def can_sign(self) -> bool:
        """Whether this account holds a keypair and can sign."""
        ...

    @property
    def secret(self) -> bytes:
        """The 64-byte secret key; raises if the account has no keypair."""
        ...

    def sign(self, message: bytes) -> SignedMessage:
        """Sign `message` with this account's ed25519 keypair, returning a
        `SignedMessage` claim (`curve="ed25519"`, `identity` = this account's
        pubkey) for `ed25519.verify(...)`. The raw 64-byte signature is
        `.signature` / `bytes(...)`. Raises if the account has no keypair."""
        ...

    @property
    def exists(self) -> bool:
        """Whether an account currently exists at this address in the SVM."""
        ...

    @property
    def lamports(self) -> u64:
        """The account's lamport balance; raises if it does not exist. Typed
        `u64` so it drops straight into builder args / struct fields of that
        width without a type-checker complaint."""
        ...

    @property
    def data(self) -> bytes:
        """The account's raw data bytes; raises if it does not exist."""
        ...

    @property
    def owner(self) -> Pubkey:
        """The program that owns the account; raises if it does not exist."""
        ...

    @property
    def executable(self) -> bool:
        """Whether the account is an executable program; raises if it does not
        exist."""
        ...

    @property
    def rent_epoch(self) -> int:
        """The account's rent epoch; raises if it does not exist."""
        ...

    @overload
    def tx(
        self,
        ix: Instruction[_R],
        /,
        *,
        signers: Sequence[Account] = ...,
        lookup_tables: Sequence[AddressLike] = ...,
    ) -> TransactionResult[_R]: ...
    @overload
    def tx(
        self,
        *ixs: Instruction | PrecompileInstruction,
        signers: Sequence[Account] = ...,
        lookup_tables: Sequence[AddressLike] = ...,
    ) -> TransactionResult[object]:
        """Build, sign, and send a transaction with this account as fee payer.

        Passing `lookup_tables` (Address Lookup Table addresses) builds a **v0**
        transaction that sources eligible accounts from those tables; omitting it
        builds a legacy transaction. Get a table from `svm.create_lookup_table`
        (cheatcode) or `svm.address_lookup_table` (official).

        The instructions run in order. The recent blockhash is taken from the
        bound SVM; the returned result's `call_trace` reflects the (possibly
        nested) invocations.

        Signing model — two independent things:

        * **Who must sign** is fixed by the instructions (every account flagged
          as a signer). `signers` does NOT change this set: it neither adds
          signers nor replaces the inferred ones. Passing an account that the
          instructions don't require to sign has no effect.
        * **The private key** for each required signer is resolved
          automatically, trying in order: (1) this fee-payer account, (2) the
          accounts in `signers`, (3) the process-global keystore of every
          account created via `Account.new()` / `Account.from_secret()` (their
          keys register on creation). If none yields a key, `tx` raises.

        So `signers` is purely a **fallback supply of keys** for a required
        signer the harness can't otherwise resolve — i.e. one that is neither
        the fee payer nor a harness-created account. Because created signing
        accounts auto-register, you usually don't need it; pass it only when a
        required signer's keypair isn't already known. (A keypair-less view in
        `signers`, `can_sign == False`, contributes nothing.)

        When the bound SVM has `sigverify` off (`svm.sigverify = False`), a
        required signer whose key the harness can't resolve — including the fee
        payer — is not an error: its signature slot is left as a placeholder and
        the runtime accepts it. This lets you send a transaction as an account
        you hold no key for, e.g. to exercise a program's own signer checks.
        """
        ...

    @overload
    def simulate(
        self,
        ix: Instruction[_R],
        /,
        *,
        signers: Sequence[Account] = ...,
        lookup_tables: Sequence[AddressLike] = ...,
    ) -> TransactionResult[_R]: ...
    @overload
    def simulate(
        self,
        *ixs: Instruction | PrecompileInstruction,
        signers: Sequence[Account] = ...,
        lookup_tables: Sequence[AddressLike] = ...,
    ) -> TransactionResult[object]:
        """Build, sign, and **simulate** a transaction — same shape as `tx` (incl.
        `lookup_tables` for a v0 transaction), but
        commits nothing (SVM state is unchanged).

        The transaction is built and signed exactly like `tx` (identical signing
        model — `signers` is the same fallback supply of keys), so the fee payer
        still matters: it is the message's fee-payer slot, the default signer,
        and must have a keypair. Only the final step differs — the tx is run
        against current state and the result discarded, so balances/accounts are
        not mutated and `signature` is `None`. The returned result still carries
        `logs`, `return_data`, `compute_units_consumed`, `error`, and
        `call_trace`. Use it to inspect logs / CUs / return data without
        spending the transaction.
        """
        ...

    def __eq__(self, other: object) -> bool:
        """Equal when two accounts share the same address (keypair ignored)."""
        ...

    def __hash__(self) -> int:
        """Hash of the address, so accounts work as dict keys / set members."""
        ...

    def __str__(self) -> str:
        """The resolved display name: a well-known program/sysvar name, else an
        assigned label, else a truncated base58 of the address."""
        ...

    def __repr__(self) -> str:
        """Return an `Account(<address>[, "label"][, signer])` representation."""
        ...

def default_svm() -> LiteSVM:
    """Return the process-global default SVM, created once on first access
    (with sigverify and blockhash checks on)."""
    ...

# --- precompile signing primitives (used by the ed25519/secp256k1/secp256r1
# modules; keys are derived deterministically from a 32-byte seed) ------------ #
def secp256k1_secret_from_seed(seed: bytes) -> bytes:
    """Derive a valid 32-byte secp256k1 secret scalar from a 32-byte seed."""
    ...

def secp256k1_eth_address(secret: bytes) -> bytes:
    """The 20-byte Ethereum address for a 32-byte secp256k1 secret."""
    ...

def secp256k1_sign(secret: bytes, message: bytes) -> tuple[bytes, int]:
    """Ethereum-style sign (keccak256 + recoverable ECDSA); returns
    `(signature[64], recovery_id)`."""
    ...

def secp256r1_secret_from_seed(seed: bytes) -> bytes:
    """Derive a valid 32-byte secp256r1 secret scalar from a 32-byte seed."""
    ...

def secp256r1_public_key(secret: bytes) -> bytes:
    """The 33-byte compressed public key for a 32-byte secp256r1 secret."""
    ...

def secp256r1_sign(secret: bytes, message: bytes) -> bytes:
    """ECDSA/P-256 sign over SHA-256, low-S normalized; returns a 64-byte
    compact signature."""
    ...
