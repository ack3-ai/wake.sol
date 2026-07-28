//! Address Lookup Table (ALT) support.
//!
//! Two ways to get a table, per the design discussion:
//!
//! * **Official builders** (`svm.address_lookup_table.*`) — construct the real
//!   on-chain ALT-program instructions (`CreateLookupTable`, `ExtendLookupTable`,
//!   …). These go through the actual program, so they carry its real-world
//!   constraints: `create` derives the table address from a *recent* slot (must
//!   be present in the `SlotHashes` sysvar) and the authority must sign; and
//!   addresses added via `extend` are only usable from the *next* slot on. Not a
//!   cheatcode — this is the faithful path.
//! * **God-mode cheatcode** (`svm.create_lookup_table(...)`, defined in `lib.rs`)
//!   — injects a ready-to-use, immediately-active table via `set_account`,
//!   sidestepping the recent-slot / one-slot-warmup / authority dance. See its
//!   docstring; this module provides the state serialization it uses.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use solana_address::Address;
use solana_address_lookup_table_interface::instruction::{
    close_lookup_table, create_lookup_table, deactivate_lookup_table, extend_lookup_table,
    freeze_lookup_table,
};
use solana_address_lookup_table_interface::program::id as alt_program_id;
use solana_address_lookup_table_interface::state::{AddressLookupTable, LookupTableMeta};
use solana_clock::Clock;

use crate::instruction::PyInstruction;
use crate::{PyLiteSVM, PyPubkey};

/// The Address Lookup Table program id.
pub(crate) fn program_id() -> Address {
    alt_program_id()
}

/// Serialize a ready-to-use ALT account for the god-mode cheatcode: a table that
/// is immediately active — `deactivation_slot = MAX` and `last_extended_slot = 0`
/// (so its addresses are live at any current slot `>= 1`; under a mainnet fork
/// the clock is a large slot, so this is automatic).
pub(crate) fn cheat_table_data(authority: Address, addresses: Vec<Address>) -> PyResult<Vec<u8>> {
    let meta = LookupTableMeta { authority: Some(authority), ..LookupTableMeta::default() };
    let table = AddressLookupTable { meta, addresses: addresses.into() };
    table
        .serialize_for_tests()
        .map_err(|e| PyValueError::new_err(format!("failed to serialize lookup table: {e:?}")))
}

/// Read the addresses stored in a serialized lookup-table account (for
/// compiling v0 transactions and for the fork two-wave resolution).
pub(crate) fn read_table_addresses(data: &[u8]) -> PyResult<Vec<Address>> {
    AddressLookupTable::deserialize(data)
        .map(|t| t.addresses.to_vec())
        .map_err(|e| PyValueError::new_err(format!("invalid address lookup table: {e:?}")))
}

/// Builder namespace for the official ALT-program instructions, reached via
/// `svm.address_lookup_table`.
#[pyclass(name = "AddressLookupTable", module = "wake_sol._native")]
pub struct PyAltBuilder {
    svm: Py<PyLiteSVM>,
}

impl PyAltBuilder {
    pub(crate) fn new(svm: Py<PyLiteSVM>) -> Self {
        Self { svm }
    }
}

#[pymethods]
impl PyAltBuilder {
    /// `CreateLookupTable`: returns `(instruction, table_address)`. `recent_slot`
    /// must be a slot present in the `SlotHashes` sysvar (defaults to the SVM's
    /// current clock slot); the table address is derived from `authority` +
    /// `recent_slot`. `authority` and `payer` must sign the transaction. The
    /// table isn't usable for lookups until the slot *after* it is extended —
    /// prefer the `svm.create_lookup_table` cheatcode when you just want a table.
    #[pyo3(signature = (authority, payer, recent_slot = None))]
    fn create(
        &self,
        py: Python<'_>,
        authority: &Bound<'_, PyAny>,
        payer: &Bound<'_, PyAny>,
        recent_slot: Option<u64>,
    ) -> PyResult<(PyInstruction, PyPubkey)> {
        let authority = PyPubkey::new(authority)?.inner;
        let payer = PyPubkey::new(payer)?.inner;
        let slot = match recent_slot {
            Some(s) => s,
            None => self.svm.borrow(py).inner.get_sysvar::<Clock>().slot,
        };
        let (ix, table) = create_lookup_table(authority, payer, slot);
        Ok((PyInstruction::from_sdk(ix), PyPubkey { inner: table }))
    }

    /// `ExtendLookupTable`: append `addresses` to `table`. `authority` must sign;
    /// `payer` (defaults to none) funds any rent increase and must sign.
    #[pyo3(signature = (table, authority, addresses, payer = None))]
    fn extend(
        &self,
        table: &Bound<'_, PyAny>,
        authority: &Bound<'_, PyAny>,
        addresses: Vec<Bound<'_, PyAny>>,
        payer: Option<Bound<'_, PyAny>>,
    ) -> PyResult<PyInstruction> {
        let table = PyPubkey::new(table)?.inner;
        let authority = PyPubkey::new(authority)?.inner;
        let payer = match payer {
            Some(p) => Some(PyPubkey::new(&p)?.inner),
            None => None,
        };
        let addrs: Vec<Address> =
            addresses.iter().map(|a| Ok(PyPubkey::new(a)?.inner)).collect::<PyResult<_>>()?;
        Ok(PyInstruction::from_sdk(extend_lookup_table(table, authority, payer, addrs)))
    }

    /// `DeactivateLookupTable`: begin deactivating `table` (signed by `authority`).
    fn deactivate(
        &self,
        table: &Bound<'_, PyAny>,
        authority: &Bound<'_, PyAny>,
    ) -> PyResult<PyInstruction> {
        let table = PyPubkey::new(table)?.inner;
        let authority = PyPubkey::new(authority)?.inner;
        Ok(PyInstruction::from_sdk(deactivate_lookup_table(table, authority)))
    }

    /// `CloseLookupTable`: close a deactivated `table`, draining its lamports to
    /// `recipient` (signed by `authority`).
    fn close(
        &self,
        table: &Bound<'_, PyAny>,
        authority: &Bound<'_, PyAny>,
        recipient: &Bound<'_, PyAny>,
    ) -> PyResult<PyInstruction> {
        let table = PyPubkey::new(table)?.inner;
        let authority = PyPubkey::new(authority)?.inner;
        let recipient = PyPubkey::new(recipient)?.inner;
        Ok(PyInstruction::from_sdk(close_lookup_table(table, authority, recipient)))
    }

    /// `FreezeLookupTable`: permanently freeze `table` so it can never be
    /// extended or closed again (signed by `authority`).
    fn freeze(
        &self,
        table: &Bound<'_, PyAny>,
        authority: &Bound<'_, PyAny>,
    ) -> PyResult<PyInstruction> {
        let table = PyPubkey::new(table)?.inner;
        let authority = PyPubkey::new(authority)?.inner;
        Ok(PyInstruction::from_sdk(freeze_lookup_table(table, authority)))
    }
}
