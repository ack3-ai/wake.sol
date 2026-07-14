use std::collections::HashSet;
use std::path::PathBuf;
use std::str::FromStr;

use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyInt};
use pyo3::{PyTraverseError, PyVisit};

use agave_feature_set::FeatureSet;
use litesvm::LiteSVM as InnerLiteSVM;
use solana_account::Account as InnerAccount;
use solana_address::Address;
use solana_hash::Hash;
use solana_message::{AddressLookupTableAccount, VersionedMessage};
use solana_keypair::Keypair;
use solana_instruction_error::InstructionError;
use solana_signature::Signature;
use solana_signer::Signer;
use solana_transaction::versioned::VersionedTransaction;
use solana_transaction_error::TransactionError;

mod account;
mod alt;
mod fork;
mod instruction;
mod perf;
mod signing;
mod sysvars;
mod trace;

use solana_clock::Clock;
use solana_epoch_rewards::EpochRewards;
use solana_epoch_schedule::EpochSchedule;
use solana_last_restart_slot::LastRestartSlot;
use solana_rent::Rent;
use solana_slot_hashes::SlotHashes;

use account::PyAccount;
use sysvars::{PyClock, PyEpochRewards, PyEpochSchedule, PyRent};

pub(crate) fn to_py_err<E: std::fmt::Debug>(e: E) -> PyErr {
    PyRuntimeError::new_err(format!("{:?}", e))
}

#[pyclass(name = "Pubkey", module = "solana_fuzzer._native", frozen, from_py_object)]
#[derive(Clone)]
pub(crate) struct PyPubkey {
    pub(crate) inner: Address,
}

#[pymethods]
impl PyPubkey {
    /// Construct from a base58 `str`, 32 raw `bytes`, an `int` (big-endian,
    /// 32 bytes), or another `Pubkey`.
    #[new]
    pub(crate) fn new(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        if let Ok(pk) = value.extract::<PyPubkey>() {
            return Ok(pk);
        }
        if let Ok(account) = value.extract::<PyRef<PyAccount>>() {
            return Ok(account.address.clone());
        }
        if let Ok(s) = value.extract::<String>() {
            return Address::from_str(&s)
                .map(|inner| Self { inner })
                .map_err(|e| PyValueError::new_err(format!("invalid base58 pubkey: {e}")));
        }
        if value.is_instance_of::<PyInt>() {
            let bytes: Vec<u8> = value
                .call_method1("to_bytes", (32usize, "big"))?
                .extract()?;
            let arr: [u8; 32] = bytes
                .try_into()
                .map_err(|_| PyValueError::new_err("integer does not fit in 32 bytes"))?;
            return Ok(Self { inner: Address::new_from_array(arr) });
        }
        if let Ok(bytes) = value.extract::<Vec<u8>>() {
            let arr: [u8; 32] = bytes
                .try_into()
                .map_err(|_| PyValueError::new_err("pubkey bytes must be exactly 32 bytes"))?;
            return Ok(Self { inner: Address::new_from_array(arr) });
        }
        Err(PyTypeError::new_err(
            "Pubkey expects str, bytes, int, or Pubkey",
        ))
    }

    fn to_bytes<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, self.inner.as_array())
    }

    fn __str__(&self) -> String {
        self.inner.to_string()
    }

    fn __repr__(&self) -> String {
        format!("Pubkey('{}')", self.inner)
    }

    fn __eq__(&self, other: &Self) -> bool {
        self.inner == other.inner
    }

    fn __hash__(&self) -> u64 {
        let bytes = self.inner.as_array();
        u64::from_le_bytes(bytes[..8].try_into().unwrap())
    }

    /// Derive a program address from `seeds` and `program_id`, searching for
    /// the canonical bump seed. Returns `(address, bump)`.
    #[staticmethod]
    pub(crate) fn find_program_address(
        seeds: Vec<Vec<u8>>,
        program_id: &Bound<'_, PyAny>,
    ) -> PyResult<(PyPubkey, u8)> {
        let pid = PyPubkey::new(program_id)?;
        let refs: Vec<&[u8]> = seeds.iter().map(Vec::as_slice).collect();
        let (addr, bump) = Address::find_program_address(&refs, &pid.inner);
        Ok((PyPubkey { inner: addr }, bump))
    }

    /// Derive a program address from explicit `seeds` (no bump search); errors
    /// if the result lands on the ed25519 curve.
    #[staticmethod]
    pub(crate) fn create_program_address(
        seeds: Vec<Vec<u8>>,
        program_id: &Bound<'_, PyAny>,
    ) -> PyResult<PyPubkey> {
        let pid = PyPubkey::new(program_id)?;
        let refs: Vec<&[u8]> = seeds.iter().map(Vec::as_slice).collect();
        Address::create_program_address(&refs, &pid.inner)
            .map(|inner| PyPubkey { inner })
            .map_err(|e| PyValueError::new_err(format!("{e}")))
    }
}

/// Raw structured error extracted from a failed `TransactionError`, before it is
/// resolved to a specific `TransactionFailed` subclass in Python. `code` xor
/// `native` carries the identity; the two indices are context.
#[derive(Clone)]
pub(crate) struct StructuredErr {
    code: Option<u32>,          // InstructionError::Custom(code) — program/Anchor/builtin
    native: Option<String>,     // any other variant's name (runtime native error)
    instruction_index: Option<u8>,
    account_index: Option<u8>,
    program_id: Option<String>, // origin of a Custom code (base58) — for builtin attribution
}

impl StructuredErr {
    /// A short display headline for the call-trace renderer (no Python round-trip).
    fn headline(&self) -> String {
        let mut s = match (self.code, &self.native) {
            (Some(c), _) => format!("Custom({c})"),
            (None, Some(n)) => n.clone(),
            _ => "error".to_string(),
        };
        if let Some(ix) = self.instruction_index {
            s.push_str(&format!(" (ix {ix})"));
        }
        s
    }
}

/// Destructure litesvm's `TransactionError` into the fields Python needs. Only
/// `Custom(u32)` carries a code; every other variant is identified by its name
/// (all are unit variants — verified against the pinned crate versions). For a
/// `Custom` code the `tree` is walked to attribute the failure to its originating
/// program, so the Python resolver can name builtin (System/Token/…) errors whose
/// small codes are otherwise ambiguous.
fn extract_error(err: &TransactionError, tree: &[trace::Traced]) -> StructuredErr {
    match err {
        TransactionError::InstructionError(idx, ie) => match ie {
            InstructionError::Custom(code) => StructuredErr {
                code: Some(*code),
                native: None,
                instruction_index: Some(*idx),
                account_index: None,
                program_id: trace::failing_program(tree, *code).map(|a| a.to_string()),
            },
            other => StructuredErr {
                code: None,
                native: Some(format!("{other:?}")), // unit variant Debug == its name
                instruction_index: Some(*idx),
                account_index: None,
                program_id: None,
            },
        },
        TransactionError::DuplicateInstruction(i) => StructuredErr {
            code: None,
            native: Some("DuplicateInstruction".to_string()),
            instruction_index: Some(*i),
            account_index: None,
            program_id: None,
        },
        TransactionError::InsufficientFundsForRent { account_index } => StructuredErr {
            code: None,
            native: Some("InsufficientFundsForRent".to_string()),
            instruction_index: None,
            account_index: Some(*account_index),
            program_id: None,
        },
        TransactionError::ProgramExecutionTemporarilyRestricted { account_index } => StructuredErr {
            code: None,
            native: Some("ProgramExecutionTemporarilyRestricted".to_string()),
            instruction_index: None,
            account_index: Some(*account_index),
            program_id: None,
        },
        other => StructuredErr {
            code: None,
            native: Some(format!("{other:?}")), // tx-level unit variant name
            instruction_index: None,
            account_index: None,
            program_id: None,
        },
    }
}

/// Build the specific `TransactionFailed` subclass instance from raw fields by
/// calling `solana_fuzzer._errors.build(...)`. The `.tx` link is set by the caller.
fn build_exception<'py>(py: Python<'py>, e: &StructuredErr) -> PyResult<Bound<'py, PyAny>> {
    py.import("solana_fuzzer._errors")?.getattr("build")?.call1((
        e.code,
        e.native.clone(),
        e.instruction_index,
        e.account_index,
        e.program_id.clone(),
    ))
}

/// Deliver a transaction outcome: return the receipt on success, or **raise** the
/// resolved `TransactionFailed` (with `.tx` linked to the receipt) on failure —
/// the always-revert contract. The result↔exception cycle is GC-collectable via
/// `PyTxResult`'s `__traverse__`/`__clear__`.
pub(crate) fn deliver(py: Python<'_>, res: PyTxResult) -> PyResult<Py<PyTxResult>> {
    let failed = res.err_data.is_some();
    let py_res = Py::new(py, res)?;
    if failed {
        // The `error` getter builds the exception, sets `exc.tx = py_res`, and caches it.
        let exc = py_res.bind(py).getattr("error")?;
        return Err(PyErr::from_value(exc));
    }
    Ok(py_res)
}

#[pyclass(name = "TransactionResult", module = "solana_fuzzer._native")]
pub(crate) struct PyTxResult {
    success: bool,
    signature: Option<Signature>,
    logs: Vec<String>,
    compute_units_consumed: u64,
    err_data: Option<StructuredErr>,   // raw failure info (None on success)
    error_cache: Option<Py<PyAny>>,    // lazily-built TransactionFailed exception (the .error surface)
    raw_return_value: Option<Vec<u8>>, // tx-wide return-data bytes (None if empty)
    return_program_id: Option<Address>,// the program that set the return data
    trace: Vec<trace::Traced>,
}

#[pymethods]
impl PyTxResult {
    /// Support `TransactionResult[T]` subscription at runtime — the type stubs
    /// make it generic over the decoded `return_value` type. Returns a
    /// `types.GenericAlias`.
    #[classmethod]
    fn __class_getitem__<'py>(
        cls: &Bound<'py, pyo3::types::PyType>,
        item: Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        cls.py()
            .import("types")?
            .getattr("GenericAlias")?
            .call1((cls, item))
    }

    #[getter]
    fn success(&self) -> bool {
        self.success
    }

    #[getter]
    fn signature<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        self.signature.map(|s| PyBytes::new(py, s.as_ref()))
    }

    #[getter]
    fn logs(&self) -> Vec<String> {
        self.logs.clone()
    }

    #[getter]
    fn compute_units_consumed(&self) -> u64 {
        self.compute_units_consumed
    }

    /// The `TransactionFailed` exception for this outcome, or `None` on success.
    /// Built once and cached, so `result.error is result.error` and (for a failed
    /// result reached via `exc.tx`) `exc.tx.error is exc`. The exception links back
    /// here as its `.tx`, forming a cycle the GC hooks below reclaim.
    #[getter]
    fn error(slf: &Bound<'_, PyTxResult>, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        {
            let this = slf.borrow();
            if this.err_data.is_none() {
                return Ok(None);
            }
            if let Some(cached) = &this.error_cache {
                return Ok(Some(cached.clone_ref(py)));
            }
        }
        let data = slf.borrow().err_data.clone().expect("err_data present");
        let exc = build_exception(py, &data)?;
        exc.setattr("tx", slf)?;
        slf.borrow_mut().error_cache = Some(exc.clone().unbind());
        Ok(Some(exc.unbind()))
    }

    /// The raw return-data bytes (tx-wide, last-writer-wins), or `None` if the
    /// transaction set no return data. Always available regardless of decoding.
    #[getter]
    fn raw_return_value<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        self.raw_return_value.as_ref().map(|d| PyBytes::new(py, d))
    }

    /// The program that set the return data, or `None` if there was none.
    #[getter]
    fn return_program_id(&self) -> Option<PyPubkey> {
        self.return_program_id.map(|inner| PyPubkey { inner })
    }

    /// The **decoded** return value, per the setting instruction's IDL `returns`
    /// type. `None` if the transaction set no return data. Best-effort on this
    /// low-level path: the return program is matched via the call trace and the
    /// bytes are strictly decoded; if the program has no generated interface, the
    /// return data can't be attributed to an instruction, the instruction
    /// declares no return type, or the bytes don't validate, this **raises**
    /// `ReturnDataError` (use `raw_return_value` for the bytes, or
    /// `decode_return(T)` to decode against a type you name).
    #[getter]
    fn return_value<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let raw = match &self.raw_return_value {
            Some(d) => d,
            None => return Ok(None),
        };
        let pid = self
            .return_program_id
            .expect("return_program_id present when raw_return_value is");
        let ix_arg = match trace::return_candidate(&self.trace, &pid) {
            Some(d) => PyBytes::new(py, &d).into_any(),
            None => py.None().into_bound(py),
        };
        let val = py
            .import("solana_fuzzer._interface")?
            .getattr("decode_return_value")?
            .call1((pid.to_string(), ix_arg, PyBytes::new(py, raw)))?;
        Ok(Some(val))
    }

    /// Decode the return data against an explicitly-named type — no attribution
    /// heuristic. Raises `ReturnDataError` if there is no return data or the
    /// bytes don't decode as `ty`.
    fn decode_return<'py>(
        &self,
        py: Python<'py>,
        ty: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let raw = match &self.raw_return_value {
            Some(d) => PyBytes::new(py, d).into_any(),
            None => py.None().into_bound(py),
        };
        py.import("solana_fuzzer._interface")?
            .getattr("decode_return_as")?
            .call1((ty, raw))
    }

    /// The transaction's execution as a `CallTrace`: a sequence of top-level
    /// `TracedInstruction`s (each carrying its CPIs) that also renders itself.
    /// Empty for results without a known message (e.g. `airdrop`).
    #[getter]
    fn call_trace(&self) -> trace::PyCallTrace {
        trace::PyCallTrace::new(
            self.trace.clone(),
            self.success,
            self.err_data.as_ref().map(StructuredErr::headline),
            self.compute_units_consumed,
        )
    }

    /// All decoded events emitted during the transaction (`emit!` + `emit_cpi!`),
    /// flattened pre-order across the call tree — the assertion surface
    /// (`assert Trade(...) in result.events`). `UnknownEvent` for events whose
    /// program/discriminator isn't generated.
    #[getter]
    fn events<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, pyo3::types::PyList>> {
        let out = pyo3::types::PyList::empty(py);
        trace::collect_events(py, &self.trace, &out)?;
        Ok(out)
    }

    fn __repr__(&self) -> String {
        match &self.err_data {
            None => format!("TransactionResult(success, cu={})", self.compute_units_consumed),
            Some(e) => format!("TransactionResult(failed, {})", e.headline()),
        }
    }

    /// GC support: the cached `TransactionFailed` (which holds `.tx = self`) forms
    /// a reference cycle; expose it so Python's collector can reclaim it.
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(exc) = &self.error_cache {
            visit.call(exc)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.error_cache = None;
    }
}

/// A fresh SVM. `LiteSVM::new()` already starts from **mainnet-beta's feature
/// set** (builtins + default programs are compiled under it), so mainnet parity
/// is the default — we only set the sig/blockhash flags here, no recompile.
/// litesvm's default transaction-history window (`TransactionHistory::new()` =
/// `IndexMap::with_capacity(32)`), used to re-enable dedup after it's turned off.
const DEFAULT_TX_HISTORY_CAP: usize = 32;

fn base_svm(sigverify: bool, blockhash_check: bool, transaction_history: bool) -> InnerLiteSVM {
    let svm = InnerLiteSVM::new()
        .with_sigverify(sigverify)
        .with_blockhash_check(blockhash_check);
    // Capacity 0 disables the signature dedup, so duplicate txs are allowed
    // (see `set_transaction_history`). Non-zero keeps litesvm's default window.
    if transaction_history {
        svm
    } else {
        svm.with_transaction_history(0)
    }
}

/// Apply a non-default `feature_set` to an existing SVM, **preserving account
/// state** (rebuilds the runtime + recompiles deployed programs under the new
/// set). Only needed when deltas move the set off the mainnet default — so the
/// common path never pays this recompile.
///
/// `with_precompiles()` is re-run so precompile registration tracks the feature
/// set on every rebuild (e.g. activating `enable_secp256r1_precompile` mid-life
/// registers secp256r1). It only *adds* accounts, so a precompile whose feature
/// was deactivated keeps its (now inert) account — an accepted edge; the
/// verifier keys off the live feature set regardless.
fn apply_feature_set(svm: InnerLiteSVM, feature_set: FeatureSet) -> InnerLiteSVM {
    let mut svm = svm
        .with_feature_set(feature_set)
        .with_builtins()
        .with_precompiles();
    let _ = svm.rebuild_caches();
    svm
}

#[pyclass(name = "LiteSVM", module = "solana_fuzzer._native", unsendable)]
pub(crate) struct PyLiteSVM {
    pub(crate) inner: InnerLiteSVM,
    pub(crate) sigverify: bool,
    blockhash_check: bool,
    pub(crate) transaction_history: bool,
    fork: Option<fork::Fork>,
    /// The construction-time feature set when it differs from the mainnet
    /// default (i.e. constructed with `activate`/`deactivate`); `None` for the
    /// plain mainnet default, so `reset` can rebuild without recompiling.
    construction_features: Option<FeatureSet>,
}

#[pymethods]
impl PyLiteSVM {
    #[new]
    #[pyo3(signature = (*, sigverify = true, blockhash_check = true, transaction_history = true, activate = vec![], deactivate = vec![]))]
    fn new(
        sigverify: bool,
        blockhash_check: bool,
        transaction_history: bool,
        activate: Vec<Bound<'_, PyAny>>,
        deactivate: Vec<Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        // `base_svm` is already mainnet parity; only touch (and recompile) the
        // feature set when there are actual deltas. Remember the delta set so
        // `reset` restores it without recompiling in the common (no-delta) case.
        let base = base_svm(sigverify, blockhash_check, transaction_history);
        let (inner, construction_features) = if activate.is_empty() && deactivate.is_empty() {
            (base, None)
        } else {
            let mut fs = base.get_feature_set_ref().clone(); // = mainnet
            for f in &activate {
                fs.activate(&PyPubkey::new(f)?.inner, 0);
            }
            for f in &deactivate {
                fs.deactivate(&PyPubkey::new(f)?.inner);
            }
            (apply_feature_set(base, fs.clone()), Some(fs))
        };
        Ok(Self {
            inner,
            sigverify,
            blockhash_check,
            transaction_history,
            fork: None,
            construction_features,
        })
    }

    /// **Cheatcode.** Wipe all accounts back to genesis, restoring the
    /// construction-time config (sigverify, blockhash checks, and feature set;
    /// mid-life `activate_features`/`deactivate_features` changes are reverted).
    fn reset(&mut self) {
        // No recompile for the plain mainnet default (the common case, incl. the
        // per-test global reset); re-apply deltas only if constructed with them.
        let base = base_svm(self.sigverify, self.blockhash_check, self.transaction_history);
        self.inner = match &self.construction_features {
            Some(fs) => apply_feature_set(base, fs.clone()),
            None => base,
        };
        // Accounts are gone, so previously-hydrated addresses may re-hydrate.
        if let Some(fork) = self.fork.as_mut() {
            fork.seen.clear();
        }
    }

    /// Builder namespace for System Program instructions (Python builder in
    /// `solana_fuzzer._programs.system`).
    #[getter]
    fn system(slf: Bound<'_, PyLiteSVM>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let module = py.import("solana_fuzzer._programs.system")?;
        Ok(module.getattr("System")?.call1((slf,))?.unbind())
    }

    /// Builder namespace for SPL Token instructions (Python builder in
    /// `solana_fuzzer._programs.token`).
    #[getter]
    fn token(slf: Bound<'_, PyLiteSVM>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let module = py.import("solana_fuzzer._programs.token")?;
        Ok(module.getattr("Token")?.call1((slf,))?.unbind())
    }

    /// Builder namespace for the official Address Lookup Table program
    /// instructions (`create` / `extend` / `deactivate` / `close` / `freeze`).
    /// These go through the real ALT program (not a cheatcode); to just get a
    /// ready-to-use table, prefer the `create_lookup_table` cheatcode.
    #[getter]
    fn address_lookup_table(slf: Bound<'_, PyLiteSVM>) -> alt::PyAltBuilder {
        alt::PyAltBuilder::new(slf.unbind())
    }

    #[getter]
    fn sigverify(&self) -> bool {
        self.sigverify
    }

    /// Toggle signature verification in place, preserving all account state.
    #[setter]
    fn set_sigverify(&mut self, value: bool) {
        let svm = std::mem::take(&mut self.inner);
        self.inner = svm.with_sigverify(value);
        self.sigverify = value;
    }

    #[getter]
    fn transaction_history(&self) -> bool {
        self.transaction_history
    }

    /// Toggle the transaction-history dedup in place, preserving all account
    /// state. When **off**, litesvm no longer rejects a repeated transaction
    /// signature with `AlreadyProcessed`, so byte-identical txs execute again —
    /// what the fuzz engine wants for repeated actions. Also disables
    /// `get_transaction(sig)` lookups. When re-enabled, restores litesvm's
    /// default window.
    #[setter]
    fn set_transaction_history(&mut self, value: bool) {
        let svm = std::mem::take(&mut self.inner);
        let cap = if value { DEFAULT_TX_HISTORY_CAP } else { 0 };
        self.inner = svm.with_transaction_history(cap);
        self.transaction_history = value;
    }

    #[getter]
    fn blockhash_check(&self) -> bool {
        self.blockhash_check
    }

    /// Toggle blockhash checking in place, preserving all account state.
    #[setter]
    fn set_blockhash_check(&mut self, value: bool) {
        let svm = std::mem::take(&mut self.inner);
        self.inner = svm.with_blockhash_check(value);
        self.blockhash_check = value;
    }

    /// Whether the given feature-gate pubkey is active in this SVM's feature set.
    fn is_feature_active(&self, feature: &Bound<'_, PyAny>) -> PyResult<bool> {
        Ok(self.inner.get_feature_set_ref().is_active(&PyPubkey::new(feature)?.inner))
    }

    /// **Cheatcode.** Activate the given feature-gate pubkeys on the live SVM,
    /// **preserving account state** (rebuilds the runtime under the new feature
    /// set and recompiles deployed programs). Mirrors a real mainnet feature
    /// activation at an epoch boundary — not a normal runtime operation.
    #[pyo3(signature = (*features))]
    fn activate_features(&mut self, features: Vec<Bound<'_, PyAny>>) -> PyResult<()> {
        let mut fs = (*self.inner.get_feature_set_ref()).clone();
        for f in &features {
            fs.activate(&PyPubkey::new(f)?.inner, 0);
        }
        self.rebuild_with_feature_set(fs);
        Ok(())
    }

    /// **Cheatcode.** Deactivate the given feature-gate pubkeys on the live SVM,
    /// **preserving account state** (as `activate_features`, in reverse).
    #[pyo3(signature = (*features))]
    fn deactivate_features(&mut self, features: Vec<Bound<'_, PyAny>>) -> PyResult<()> {
        let mut fs = (*self.inner.get_feature_set_ref()).clone();
        for f in &features {
            fs.deactivate(&PyPubkey::new(f)?.inner);
        }
        self.rebuild_with_feature_set(fs);
        Ok(())
    }

    /// **Cheatcode.** Credit `lamports` to `address` out of thin air — mints
    /// lamports, which cannot happen on a real chain.
    fn airdrop(
        &mut self,
        py: Python<'_>,
        address: &Bound<'_, PyAny>,
        lamports: u64,
    ) -> PyResult<Py<PyTxResult>> {
        let address = PyPubkey::new(address)?;
        deliver(py, tx_result_from(self.inner.airdrop(&address.inner, lamports), None))
    }

    /// **Cheatcode.** Overwrite the account at `address` with the given fields —
    /// a god-mode state write, not possible on a real chain. Omitted fields take
    /// their default (0 lamports, empty data, System owner, …).
    #[pyo3(signature = (address, *, lamports = 0, data = vec![], owner = None, executable = false, rent_epoch = 0))]
    fn set_account(
        &mut self,
        address: &Bound<'_, PyAny>,
        lamports: u64,
        data: Vec<u8>,
        owner: Option<Bound<'_, PyAny>>,
        executable: bool,
        rent_epoch: u64,
    ) -> PyResult<()> {
        let address = PyPubkey::new(address)?;
        let owner = match owner {
            Some(owner) => PyPubkey::new(&owner)?.inner,
            None => Address::default(),
        };
        let acc = InnerAccount { lamports, data, owner, executable, rent_epoch };
        self.inner.set_account(address.inner, acc).map_err(to_py_err)
    }

    fn latest_blockhash<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        let h: Hash = self.inner.latest_blockhash();
        PyBytes::new(py, h.as_ref())
    }

    /// **Cheatcode.** Advance past the current blockhash so it is no longer
    /// accepted (test control over blockhash expiry).
    fn expire_blockhash(&mut self) {
        self.inner.expire_blockhash();
    }

    /// **Cheatcode.** Jump the clock forward to `slot` — arbitrary time travel,
    /// not possible on a real chain.
    fn warp_to_slot(&mut self, slot: u64) {
        self.inner.warp_to_slot(slot);
    }

    fn minimum_balance_for_rent_exemption(&self, data_len: usize) -> u64 {
        self.inner.minimum_balance_for_rent_exemption(data_len)
    }

    // --- sysvars ------------------------------------------------------------ //
    // Reads via litesvm `get_sysvar`; writes via `set_sysvar` (updates the
    // cached sysvar the runtime uses, not just the backing account). Setters
    // are partial — only the given kwargs change; the rest keep their value.

    #[getter]
    fn clock(&self) -> PyClock {
        self.inner.get_sysvar::<Clock>().into()
    }

    /// **Cheatcode.** Override the given `Clock` fields (others unchanged) —
    /// direct sysvar manipulation, not possible on a real chain.
    #[pyo3(signature = (*, slot = None, epoch = None, epoch_start_timestamp = None,
                        leader_schedule_epoch = None, unix_timestamp = None))]
    fn set_clock(&mut self, slot: Option<u64>, epoch: Option<u64>,
                 epoch_start_timestamp: Option<i64>, leader_schedule_epoch: Option<u64>,
                 unix_timestamp: Option<i64>) {
        let mut c: Clock = self.inner.get_sysvar();
        if let Some(v) = slot { c.slot = v; }
        if let Some(v) = epoch { c.epoch = v; }
        if let Some(v) = epoch_start_timestamp { c.epoch_start_timestamp = v; }
        if let Some(v) = leader_schedule_epoch { c.leader_schedule_epoch = v; }
        if let Some(v) = unix_timestamp { c.unix_timestamp = v; }
        self.inner.set_sysvar(&c);
    }

    /// **Cheatcode.** Set only the clock's `unix_timestamp` (block time) —
    /// direct sysvar manipulation, not possible on a real chain.
    fn warp_to_timestamp(&mut self, unix_timestamp: i64) {
        let mut c: Clock = self.inner.get_sysvar();
        c.unix_timestamp = unix_timestamp;
        self.inner.set_sysvar(&c);
    }

    #[getter]
    fn rent(&self) -> PyRent {
        self.inner.get_sysvar::<Rent>().into()
    }

    /// **Cheatcode.** Override the given `Rent` fields (others unchanged) —
    /// direct sysvar manipulation, not possible on a real chain.
    #[pyo3(signature = (*, lamports_per_byte_year = None, exemption_threshold = None,
                        burn_percent = None))]
    #[allow(deprecated)]  // rent fields are deprecated upstream but still the wire layout
    fn set_rent(&mut self, lamports_per_byte_year: Option<u64>,
                exemption_threshold: Option<f64>, burn_percent: Option<u8>) {
        let mut r: Rent = self.inner.get_sysvar();
        if let Some(v) = lamports_per_byte_year { r.lamports_per_byte_year = v; }
        if let Some(v) = exemption_threshold { r.exemption_threshold = v; }
        if let Some(v) = burn_percent { r.burn_percent = v; }
        self.inner.set_sysvar(&r);
    }

    #[getter]
    fn epoch_schedule(&self) -> PyEpochSchedule {
        self.inner.get_sysvar::<EpochSchedule>().into()
    }

    /// **Cheatcode.** Override the given `EpochSchedule` fields (others
    /// unchanged) — direct sysvar manipulation, not possible on a real chain.
    #[pyo3(signature = (*, slots_per_epoch = None, leader_schedule_slot_offset = None,
                        warmup = None, first_normal_epoch = None, first_normal_slot = None))]
    fn set_epoch_schedule(&mut self, slots_per_epoch: Option<u64>,
                          leader_schedule_slot_offset: Option<u64>, warmup: Option<bool>,
                          first_normal_epoch: Option<u64>, first_normal_slot: Option<u64>) {
        let mut e: EpochSchedule = self.inner.get_sysvar();
        if let Some(v) = slots_per_epoch { e.slots_per_epoch = v; }
        if let Some(v) = leader_schedule_slot_offset { e.leader_schedule_slot_offset = v; }
        if let Some(v) = warmup { e.warmup = v; }
        if let Some(v) = first_normal_epoch { e.first_normal_epoch = v; }
        if let Some(v) = first_normal_slot { e.first_normal_slot = v; }
        self.inner.set_sysvar(&e);
    }

    #[getter]
    fn last_restart_slot(&self) -> u64 {
        self.inner.get_sysvar::<LastRestartSlot>().last_restart_slot
    }

    /// **Cheatcode.** Set the `LastRestartSlot` sysvar — direct sysvar
    /// manipulation, not possible on a real chain.
    fn set_last_restart_slot(&mut self, slot: u64) {
        self.inner.set_sysvar(&LastRestartSlot { last_restart_slot: slot });
    }

    #[getter]
    fn epoch_rewards(&self) -> PyEpochRewards {
        self.inner.get_sysvar::<EpochRewards>().into()
    }

    /// Read-only: `[(slot, hash_bytes), …]`, most-recent first.
    #[getter]
    fn slot_hashes<'py>(&self, py: Python<'py>) -> Vec<(u64, Bound<'py, PyBytes>)> {
        let sh: SlotHashes = self.inner.get_sysvar();
        sh.iter()
            .map(|e| (e.0, PyBytes::new(py, e.1.as_ref())))
            .collect()
    }

    /// **Cheatcode.** Deploy a BPF program from the `.so` file at `path` directly
    /// at `program_id`, bypassing the loader and upgrade-authority flow — not
    /// possible on a real chain without going through the BPF loader.
    fn add_program_from_file(
        &mut self,
        program_id: &Bound<'_, PyAny>,
        path: &str,
    ) -> PyResult<()> {
        let program_id = PyPubkey::new(program_id)?;
        self.inner
            .add_program_from_file(program_id.inner, path)
            .map_err(to_py_err)
    }

    /// **Cheatcode.** Deploy a BPF program from raw ELF `bytes` directly at
    /// `program_id`, bypassing the loader and upgrade-authority flow — not
    /// possible on a real chain without going through the BPF loader.
    fn add_program(&mut self, program_id: &Bound<'_, PyAny>, bytes: &[u8]) -> PyResult<()> {
        let program_id = PyPubkey::new(program_id)?;
        self.inner.add_program(program_id.inner, bytes).map_err(to_py_err)
    }

    /// **Cheatcode.** Inject a ready-to-use Address Lookup Table directly (via
    /// `set_account`), bypassing the ALT program's create/extend flow and its
    /// recent-slot + one-slot-warmup + authority requirements. The returned table
    /// is immediately active and can be referenced by v0 transactions. This
    /// cannot happen on a real chain — a real ALT must be created and extended
    /// through the loader program and warmed up over a slot. For the faithful
    /// path use `svm.address_lookup_table.create` / `.extend`.
    ///
    /// `addresses` are the entries the table resolves; `address` optionally fixes
    /// the table's own address (default: a fresh one); `authority` sets the
    /// modification authority (default: the System program — irrelevant for pure
    /// lookups). Returns the table address.
    #[pyo3(signature = (addresses, *, address = None, authority = None))]
    fn create_lookup_table(
        &mut self,
        addresses: Vec<Bound<'_, PyAny>>,
        address: Option<Bound<'_, PyAny>>,
        authority: Option<Bound<'_, PyAny>>,
    ) -> PyResult<PyPubkey> {
        let addrs: Vec<Address> = addresses
            .iter()
            .map(|a| Ok(PyPubkey::new(a)?.inner))
            .collect::<PyResult<Vec<_>>>()?;
        let auth = match authority {
            Some(a) => PyPubkey::new(&a)?.inner,
            None => Address::default(),
        };
        let table_addr = match address {
            Some(a) => PyPubkey::new(&a)?.inner,
            None => Keypair::new().pubkey(),
        };
        let data = alt::cheat_table_data(auth, addrs)?;
        let lamports = self.inner.minimum_balance_for_rent_exemption(data.len());
        let acc = InnerAccount {
            lamports,
            data,
            owner: alt::program_id(),
            executable: false,
            rent_epoch: 0,
        };
        self.inner.set_account(table_addr, acc).map_err(to_py_err)?;
        Ok(PyPubkey { inner: table_addr })
    }

    /// **Cheatcode.** Enable **mainnet forking**: dependency accounts are hydrated from `url`
    /// (an RPC endpoint) on first touch and cached under `cache`. `exclude` lists
    /// program IDs to treat as not-on-mainnet — their code is not forked (deploy
    /// your own build) and any account they own starts blank. With `offline=True`
    /// no URL is needed and nothing hits the network: state is replayed from the
    /// snapshot and a cache miss is a hard error. Forking uses the SVM's own
    /// feature set (mainnet parity by default; diverge via the constructor's
    /// `activate`/`deactivate`) — it is *not* fetched from the forked chain, so
    /// it can lag the fork slot's actual active features. See design/forking-spec/.
    #[pyo3(signature = (url = None, *, exclude = vec![], cache = None, offline = false))]
    fn fork(
        &mut self,
        url: Option<String>,
        exclude: Vec<Bound<'_, PyAny>>,
        cache: Option<String>,
        offline: bool,
    ) -> PyResult<()> {
        let mut exclude_programs = HashSet::new();
        for e in &exclude {
            exclude_programs.insert(PyPubkey::new(e)?.inner);
        }
        let dir = PathBuf::from(cache.unwrap_or_else(|| "fork-cache".to_string()));
        let rpc = if offline {
            None
        } else {
            let u = url.ok_or_else(|| {
                PyValueError::new_err("fork(url=...) is required unless offline=True")
            })?;
            Some(fork::RpcClient::new(u))
        };
        let mut f = fork::Fork::new(rpc, dir, exclude_programs);
        // Capture the reference slot once and seed the clock + tip blockhash, so
        // forked oracles aren't stale and durable-nonce paths see a real hash.
        // Capture the reference slot; seed the clock and the tip blockhash so
        // forked oracles aren't stale and durable-nonce paths see a real hash.
        let seed = match f.rpc() {
            Some(rpc) => {
                let s = rpc.get_slot().map_err(PyRuntimeError::new_err)?;
                Some((s, rpc.get_block_time(s), rpc.get_latest_blockhash().ok()))
            }
            None => None,
        };
        if let Some((s, block_time, blockhash)) = seed {
            f.min_context_slot = s;
            let mut c: Clock = self.inner.get_sysvar();
            c.slot = s;
            if let Some(t) = block_time {
                c.unix_timestamp = t;
            }
            self.inner.set_sysvar(&c);
            if let Some(bh) = blockhash {
                if let Ok(h) = Hash::from_str(&bh) {
                    self.inner.set_latest_blockhash(h);
                }
            }
        }
        self.fork = Some(f);
        Ok(())
    }

    /// Disable mainnet forking: drop the fork configuration so nothing further
    /// hydrates from RPC/snapshot. It **stops** forking, it does not **undo** it —
    /// already-hydrated accounts are **not** wiped and the seeded clock/blockhash
    /// are **not** reverted (both are `reset()`'s job); the on-disk snapshot cache
    /// is untouched. So `unfork()` alone *freezes* the current forked state (handy
    /// to hydrate what you need, then run RPC-free); `unfork()` then `reset()`
    /// gives a clean, unforked SVM. (`reset()` alone keeps forking on.)
    fn unfork(&mut self) {
        self.fork = None;
    }

    /// **Cheatcode.** Pre-fetch on-chain **programs** into the fork by id, without
    /// running a transaction: each id's program account and (for upgradeable
    /// programs) its programdata are hydrated from the snapshot cache → RPC,
    /// exactly as lazy hydration would on first touch. Use it to *pin* the program
    /// set a test needs so a later `offline=True` replay has no network dependency:
    /// run the scenario once online, read `forked_accounts()` to see which programs
    /// it touched, then freeze that list here. Errors if an id is not an executable
    /// program on chain (absent, or a non-program account), so a wrong id fails
    /// loudly rather than silently under-forking. Requires forking to be enabled.
    /// Returns the number of programs pinned. See design/forking-spec/.
    #[pyo3(signature = (*programs))]
    fn fork_programs(&mut self, programs: Vec<Bound<'_, PyAny>>) -> PyResult<usize> {
        if self.fork.is_none() {
            return Err(PyRuntimeError::new_err(
                "fork_programs requires forking to be enabled; call svm.fork(url=...) \
                 or svm.fork(offline=True) first",
            ));
        }
        let mut ids: Vec<Address> = Vec::with_capacity(programs.len());
        for p in &programs {
            ids.push(PyPubkey::new(p)?.inner);
        }
        // ensure_present fetches each id and, for upgradeable loaders, expands to
        // its programdata — the same path lazy hydration uses.
        self.ensure_present(&ids)?;
        for id in &ids {
            match self.inner.get_account(id) {
                Some(acc) if acc.executable => {}
                _ => {
                    return Err(PyRuntimeError::new_err(format!(
                        "fork_programs: {id} is not an executable program on chain \
                         (absent, or a non-program account) — check the id"
                    )));
                }
            }
        }
        Ok(ids.len())
    }

    /// The on-chain accounts hydrated into this fork so far, as read-only
    /// `Account` views — every address the fork has resolved to a *present*
    /// account this session, ordered by address for stable output. Excludes
    /// confirmed-absent and owner-blanked addresses, and your own locally-set
    /// accounts (which aren't fork-sourced). Empty when forking is off. Pair with
    /// `fork_programs` to freeze a discovered program set for offline replay:
    /// `svm.fork_programs(*(a.pubkey for a in svm.forked_accounts() if a.executable))`.
    fn forked_accounts(slf: Bound<'_, PyLiteSVM>) -> PyResult<Vec<PyAccount>> {
        let this = slf.borrow();
        let Some(fork) = this.fork.as_ref() else { return Ok(vec![]) };
        let mut addrs: Vec<Address> = fork
            .seen
            .iter()
            .copied()
            .filter(|a| this.inner.get_account(a).is_some())
            .collect();
        addrs.sort_by_key(Address::to_string);
        drop(this);
        Ok(addrs
            .into_iter()
            .map(|a| PyAccount::view(PyPubkey { inner: a }, slf.clone().unbind()))
            .collect())
    }

    /// Send a serialized `VersionedTransaction` (bincode bytes). Raises the
    /// resolved `TransactionFailed` on failure (see design/pytypes §10).
    fn send_transaction(&mut self, py: Python<'_>, tx_bytes: &[u8]) -> PyResult<Py<PyTxResult>> {
        let tx: VersionedTransaction = bincode::deserialize(tx_bytes)
            .map_err(|e| PyValueError::new_err(format!("invalid tx bytes: {e}")))?;
        let message = tx.message.clone();
        self.hydrate_for_message(&message)?;
        let res = self.inner.send_transaction(tx);
        deliver(py, tx_result_from(res, Some(&message)))
    }

    fn simulate_transaction(&mut self, py: Python<'_>, tx_bytes: &[u8]) -> PyResult<Py<PyTxResult>> {
        let tx: VersionedTransaction = bincode::deserialize(tx_bytes)
            .map_err(|e| PyValueError::new_err(format!("invalid tx bytes: {e}")))?;
        self.hydrate_for_message(&tx.message)?;
        deliver(py, self.simulate_vtx(tx))
    }
}

impl PyLiteSVM {
    /// Rebuild the runtime under `feature_set`, **preserving all account state**:
    /// `mem::take` keeps the accounts; `with_feature_set`/`with_builtins` mutate
    /// in place (no genesis reset); `rebuild_caches` recompiles deployed programs
    /// under the new feature set.
    fn rebuild_with_feature_set(&mut self, feature_set: FeatureSet) {
        let svm = std::mem::take(&mut self.inner);
        self.inner = apply_feature_set(svm, feature_set);
    }

    /// Ensure every key in `keys` is resident, hydrating any that are missing
    /// from the fork (snapshot cache → RPC) and injecting them. No-op when
    /// forking is off. See design/forking-spec/ §3.
    pub(crate) fn ensure_present(&mut self, keys: &[Address]) -> PyResult<()> {
        // Disjoint mutable borrows of `inner` and `fork` (distinct fields), so
        // the account-store read and the fetch coexist.
        let PyLiteSVM { inner, fork, .. } = self;
        let Some(fork) = fork.as_mut() else { return Ok(()) };

        let mut candidates: Vec<Address> = Vec::new();
        for &k in keys {
            if fork.seen.contains(&k) {
                continue;
            }
            // Excluded program code (program id + its programdata): leave empty.
            if fork.exclude_programs.contains(&k) || fork.exclude_addrs.contains(&k) {
                fork.seen.insert(k);
                continue;
            }
            // Local-wins: an account already present is never overwritten.
            if inner.get_account(&k).is_some() {
                continue;
            }
            candidates.push(k);
        }
        if candidates.is_empty() {
            return Ok(());
        }

        let fetched = fork.fetch(&candidates).map_err(PyRuntimeError::new_err)?;
        let mut inject: Vec<(Address, fork::FetchedAccount)> = Vec::new();
        let mut followups: Vec<Address> = Vec::new();
        for (k, acc) in candidates.iter().zip(fetched.into_iter()) {
            fork.seen.insert(*k);
            let Some(acc) = acc else { continue };
            // Owner-scoped exclude: an account owned by an excluded program is
            // blanked (the audited program's state starts from scratch).
            if fork.exclude_programs.contains(&acc.owner) {
                continue;
            }
            // Upgradeable program → also hydrate its programdata account.
            if acc.executable && fork::is_loader_v3(&acc.owner) {
                if let Some(pd) = fork::programdata_address(&acc.data) {
                    if !fork.seen.contains(&pd)
                        && !fork.exclude_addrs.contains(&pd)
                        && inner.get_account(&pd).is_none()
                    {
                        followups.push(pd);
                    }
                }
            }
            inject.push((*k, acc));
        }
        if !followups.is_empty() {
            let pd = fork.fetch(&followups).map_err(PyRuntimeError::new_err)?;
            for (k, acc) in followups.iter().zip(pd.into_iter()) {
                fork.seen.insert(*k);
                if let Some(acc) = acc {
                    inject.push((*k, acc));
                }
            }
        }
        // Non-executables first: litesvm's set_account eagerly loads an
        // executable account, reading its programdata from the db — so
        // programdata must be resident before its program.
        inject.sort_by_key(|(_, a)| a.executable);
        for (addr, acc) in inject {
            inner.set_account(addr, acc.into_account()).map_err(to_py_err)?;
        }
        Ok(())
    }

    /// Resolve lookup-table addresses into the `AddressLookupTableAccount`s that
    /// `v0::Message::try_compile` needs, reading each table from the SVM (create
    /// or fork it first). See design/forking-spec/ §3.
    pub(crate) fn resolve_alt_accounts(
        &self,
        keys: &[Address],
    ) -> PyResult<Vec<AddressLookupTableAccount>> {
        let mut out = Vec::with_capacity(keys.len());
        for k in keys {
            let acc = self.inner.get_account(k).ok_or_else(|| {
                PyValueError::new_err(format!(
                    "lookup table {k} not found — create it (svm.create_lookup_table / \
                     svm.address_lookup_table.create) or fork it first"
                ))
            })?;
            let addresses = alt::read_table_addresses(&acc.data)?;
            out.push(AddressLookupTableAccount { key: *k, addresses });
        }
        Ok(out)
    }

    /// Hydrate every account a message references before execution (fork §3.2):
    /// its static keys, and for a v0 message the two-wave ALT resolution —
    /// wave 1 hydrates the lookup-table accounts, wave 2 hydrates the addresses
    /// they resolve to. No-op when forking is off.
    pub(crate) fn hydrate_for_message(&mut self, msg: &VersionedMessage) -> PyResult<()> {
        if self.fork.is_none() {
            return Ok(());
        }
        // wave 1: static keys + the ALT table accounts themselves
        let mut keys: Vec<Address> = msg.static_account_keys().to_vec();
        if let Some(lookups) = msg.address_table_lookups() {
            keys.extend(lookups.iter().map(|l| l.account_key));
        }
        self.ensure_present(&keys)?;
        // wave 2: resolve the ALT indexes to addresses and hydrate those
        if let Some(lookups) = msg.address_table_lookups() {
            let mut resolved: Vec<Address> = Vec::new();
            for l in lookups {
                if let Some(acc) = self.inner.get_account(&l.account_key) {
                    let addrs = alt::read_table_addresses(&acc.data)?;
                    for i in l.writable_indexes.iter().chain(l.readonly_indexes.iter()) {
                        if let Some(a) = addrs.get(*i as usize) {
                            resolved.push(*a);
                        }
                    }
                }
            }
            if !resolved.is_empty() {
                self.ensure_present(&resolved)?;
            }
        }
        Ok(())
    }

    /// Simulate an already-built `VersionedTransaction` (execute against current
    /// state, commit **nothing**) and convert litesvm's result into a
    /// `PyTxResult` (no `signature`, since nothing was sent). Shared by the
    /// `svm`-level `simulate_transaction` and `Account.simulate`.
    pub(crate) fn simulate_vtx(&self, tx: VersionedTransaction) -> PyTxResult {
        let message = tx.message.clone();
        match self.inner.simulate_transaction(tx) {
            Ok(info) => {
                let meta = info.meta;
                let trace =
                    trace::build_call_tree(&message, &meta.inner_instructions, &meta.logs);
                let pid = meta.return_data.program_id.to_bytes();
                let (raw_return_value, return_program_id) =
                    split_return_data(pid, meta.return_data.data);
                PyTxResult {
                    success: true,
                    signature: None,
                    trace,
                    logs: meta.logs,
                    compute_units_consumed: meta.compute_units_consumed,
                    err_data: None,
                    error_cache: None,
                    raw_return_value,
                    return_program_id,
                }
            }
            Err(failed) => {
                let meta = failed.meta;
                let trace =
                    trace::build_call_tree(&message, &meta.inner_instructions, &meta.logs);
                let pid = meta.return_data.program_id.to_bytes();
                let (raw_return_value, return_program_id) =
                    split_return_data(pid, meta.return_data.data);
                let err_data = Some(extract_error(&failed.err, &trace));
                PyTxResult {
                    success: false,
                    signature: None,
                    trace,
                    logs: meta.logs,
                    compute_units_consumed: meta.compute_units_consumed,
                    err_data,
                    error_cache: None,
                    raw_return_value,
                    return_program_id,
                }
            }
        }
    }
}

/// Split litesvm's tx-wide return data into `(bytes, setting program)`; empty
/// data (the common "no return value" case) collapses to `(None, None)`.
fn split_return_data(program_id: [u8; 32], data: Vec<u8>) -> (Option<Vec<u8>>, Option<Address>) {
    if data.is_empty() {
        (None, None)
    } else {
        (Some(data), Some(Address::new_from_array(program_id)))
    }
}

/// Convert a litesvm transaction result into the Python-facing `PyTxResult`.
///
/// When `message` is provided, the call tree is reconstructed from it plus the
/// metadata's inner instructions; without a message (e.g. `airdrop`, whose
/// transaction is built inside litesvm) the trace is left empty.
pub(crate) fn tx_result_from(
    res: litesvm::types::TransactionResult,
    message: Option<&VersionedMessage>,
) -> PyTxResult {
    match res {
        Ok(meta) => {
            let trace = message
                .map(|m| trace::build_call_tree(m, &meta.inner_instructions, &meta.logs))
                .unwrap_or_default();
            let pid = meta.return_data.program_id.to_bytes();
            let (raw_return_value, return_program_id) =
                split_return_data(pid, meta.return_data.data);
            PyTxResult {
                success: true,
                signature: Some(meta.signature),
                trace,
                logs: meta.logs,
                compute_units_consumed: meta.compute_units_consumed,
                err_data: None,
                error_cache: None,
                raw_return_value,
                return_program_id,
            }
        }
        Err(failed) => {
            let meta = failed.meta;
            let trace = message
                .map(|m| trace::build_call_tree(m, &meta.inner_instructions, &meta.logs))
                .unwrap_or_default();
            let pid = meta.return_data.program_id.to_bytes();
            let (raw_return_value, return_program_id) =
                split_return_data(pid, meta.return_data.data);
            let err_data = Some(extract_error(&failed.err, &trace));
            PyTxResult {
                success: false,
                signature: None,
                trace,
                logs: meta.logs,
                compute_units_consumed: meta.compute_units_consumed,
                err_data,
                error_cache: None,
                raw_return_value,
                return_program_id,
            }
        }
    }
}

// Fork-safety invariant (multiprocess runner, `solana-fuzzer test -P N`; see
// design/multiprocess-runner.md §3 blocker 2): the server forks N worker
// processes with this global SVM already live in memory. That is fine — account
// state is per-fork and each worker calls `svm.reset()` before its first test —
// but only because nothing in the Rust layer holds a background thread or lock
// across the fork point. litesvm is single-threaded and `fork.rs` uses blocking
// `ureq` (no tokio runtime). Keep it that way: any Rust dependency that spawns
// its own threads (e.g. a rayon/tokio pool from an agave version bump) must be
// initialized lazily *after* the fork, or the workers may deadlock on a lock
// held by a thread that did not survive the fork.
static DEFAULT_SVM: PyOnceLock<Py<PyLiteSVM>> = PyOnceLock::new();

/// The process-global `LiteSVM`, created once in Rust on first access
/// (sigverify and blockhash checks on).
pub(crate) fn default_svm(py: Python<'_>) -> PyResult<Py<PyLiteSVM>> {
    let cell = DEFAULT_SVM
        .get_or_try_init(py, || Py::new(py, PyLiteSVM::new(true, true, true, vec![], vec![])?))?;
    Ok(cell.clone_ref(py))
}

#[pyfunction(name = "default_svm")]
fn py_default_svm(py: Python<'_>) -> PyResult<Py<PyLiteSVM>> {
    default_svm(py)
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyLiteSVM>()?;
    m.add_class::<PyPubkey>()?;
    m.add_class::<PyTxResult>()?;
    m.add_class::<account::PyAccount>()?;
    m.add_class::<alt::PyAltBuilder>()?;
    m.add_class::<instruction::PyInstruction>()?;
    m.add_class::<instruction::PyAccountMeta>()?;
    m.add_class::<trace::PyTracedInstruction>()?;
    m.add_class::<trace::PyCallTrace>()?;
    m.add_class::<sysvars::PyClock>()?;
    m.add_class::<sysvars::PyRent>()?;
    m.add_class::<sysvars::PyEpochSchedule>()?;
    m.add_class::<sysvars::PyEpochRewards>()?;
    m.add_function(wrap_pyfunction!(instruction::signer, m)?)?;
    m.add_function(wrap_pyfunction!(instruction::writable, m)?)?;
    m.add_function(wrap_pyfunction!(instruction::readonly, m)?)?;
    m.add_function(wrap_pyfunction!(instruction::writable_signer, m)?)?;
    m.add_function(wrap_pyfunction!(py_default_svm, m)?)?;
    signing::register(m)?;
    perf::register(m)?;
    Ok(())
}
