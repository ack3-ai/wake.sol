use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyType};

use crate::PyPubkey;

/// Per-account metadata within an instruction: the address, plus whether the
/// account must sign the transaction and whether it may be written to.
#[pyclass(name = "AccountMeta", module = "solana_fuzzer._native", from_py_object)]
#[derive(Clone)]
pub struct PyAccountMeta {
    pub(crate) pubkey: PyPubkey,
    #[pyo3(get, set)]
    pub(crate) is_signer: bool,
    #[pyo3(get, set)]
    pub(crate) is_writable: bool,
}

#[pymethods]
impl PyAccountMeta {
    #[new]
    #[pyo3(signature = (pubkey, is_signer = false, is_writable = false))]
    fn new(
        pubkey: &Bound<'_, PyAny>,
        is_signer: bool,
        is_writable: bool,
    ) -> PyResult<Self> {
        Ok(Self {
            pubkey: PyPubkey::new(pubkey)?,
            is_signer,
            is_writable,
        })
    }

    #[getter]
    fn pubkey(&self) -> PyPubkey {
        self.pubkey.clone()
    }

    #[setter]
    fn set_pubkey(&mut self, pubkey: &Bound<'_, PyAny>) -> PyResult<()> {
        self.pubkey = PyPubkey::new(pubkey)?;
        Ok(())
    }

    fn __eq__(&self, other: &Self) -> bool {
        self.pubkey.inner == other.pubkey.inner
            && self.is_signer == other.is_signer
            && self.is_writable == other.is_writable
    }

    fn __repr__(&self) -> String {
        format!(
            "AccountMeta({}, signer={}, writable={})",
            self.pubkey.inner, self.is_signer, self.is_writable
        )
    }
}

/// Coerce an `AccountMeta` or an address-like value into an `AccountMeta`.
/// An existing `AccountMeta` is returned verbatim; a bare address takes the
/// supplied default signer/writable flags.
pub(crate) fn meta_or(
    value: &Bound<'_, PyAny>,
    is_signer: bool,
    is_writable: bool,
) -> PyResult<PyAccountMeta> {
    if let Ok(meta) = value.extract::<PyAccountMeta>() {
        return Ok(meta);
    }
    Ok(PyAccountMeta {
        pubkey: PyPubkey::new(value)?,
        is_signer,
        is_writable,
    })
}

fn to_metas(values: Vec<Bound<'_, PyAny>>) -> PyResult<Vec<PyAccountMeta>> {
    values.iter().map(|v| meta_or(v, false, false)).collect()
}

/// Mark an account as a signer (preserving its writable flag).
#[pyfunction]
pub fn signer(account: &Bound<'_, PyAny>) -> PyResult<PyAccountMeta> {
    let mut meta = meta_or(account, false, false)?;
    meta.is_signer = true;
    Ok(meta)
}

/// Mark an account as writable (preserving its signer flag).
#[pyfunction]
pub fn writable(account: &Bound<'_, PyAny>) -> PyResult<PyAccountMeta> {
    let mut meta = meta_or(account, false, false)?;
    meta.is_writable = true;
    Ok(meta)
}

/// Mark an account as read-only (preserving its signer flag).
#[pyfunction]
pub fn readonly(account: &Bound<'_, PyAny>) -> PyResult<PyAccountMeta> {
    let mut meta = meta_or(account, false, false)?;
    meta.is_writable = false;
    Ok(meta)
}

/// Mark an account as a writable signer.
#[pyfunction]
pub fn writable_signer(account: &Bound<'_, PyAny>) -> PyResult<PyAccountMeta> {
    let mut meta = meta_or(account, false, false)?;
    meta.is_signer = true;
    meta.is_writable = true;
    Ok(meta)
}

/// A single instruction — a program to invoke, the account metas it
/// references, and an opaque data payload.
#[pyclass(name = "Instruction", module = "solana_fuzzer._native")]
pub struct PyInstruction {
    program_id: PyPubkey,
    accounts: Vec<PyAccountMeta>,
    data: Vec<u8>,
}

impl PyInstruction {
    /// Lower into an SDK `Instruction` for transaction compilation.
    pub(crate) fn to_sdk(&self) -> solana_instruction::Instruction {
        use solana_instruction::{AccountMeta, Instruction};
        let metas: Vec<AccountMeta> = self
            .accounts
            .iter()
            .map(|m| {
                if m.is_writable {
                    AccountMeta::new(m.pubkey.inner, m.is_signer)
                } else {
                    AccountMeta::new_readonly(m.pubkey.inner, m.is_signer)
                }
            })
            .collect();
        Instruction::new_with_bytes(self.program_id.inner, &self.data, metas)
    }

    /// Build a `PyInstruction` from an SDK `Instruction` (inverse of `to_sdk`) —
    /// used to surface builders that produce SDK instructions (e.g. the ALT
    /// program builders) as harness `Instruction`s.
    pub(crate) fn from_sdk(ix: solana_instruction::Instruction) -> Self {
        let accounts = ix
            .accounts
            .into_iter()
            .map(|m| PyAccountMeta {
                pubkey: PyPubkey { inner: m.pubkey },
                is_signer: m.is_signer,
                is_writable: m.is_writable,
            })
            .collect();
        Self {
            program_id: PyPubkey { inner: ix.program_id },
            accounts,
            data: ix.data,
        }
    }
}

#[pymethods]
impl PyInstruction {
    /// Support `Instruction[T]` subscription at runtime — the type stubs make
    /// `Instruction` generic (the return type flows to `TransactionResult[T]`),
    /// and builder registration evaluates the generated `-> Instruction[<ret>]`
    /// annotations, so the class must be subscriptable. Returns a
    /// `types.GenericAlias`.
    #[classmethod]
    fn __class_getitem__<'py>(
        cls: &Bound<'py, PyType>,
        item: Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        cls.py()
            .import("types")?
            .getattr("GenericAlias")?
            .call1((cls, item))
    }

    #[new]
    #[pyo3(signature = (program_id, accounts = vec![], data = vec![]))]
    fn new(
        program_id: &Bound<'_, PyAny>,
        accounts: Vec<Bound<'_, PyAny>>,
        data: Vec<u8>,
    ) -> PyResult<Self> {
        Ok(Self {
            program_id: PyPubkey::new(program_id)?,
            accounts: to_metas(accounts)?,
            data,
        })
    }

    #[getter]
    fn program_id(&self) -> PyPubkey {
        self.program_id.clone()
    }

    #[setter]
    fn set_program_id(&mut self, program_id: &Bound<'_, PyAny>) -> PyResult<()> {
        self.program_id = PyPubkey::new(program_id)?;
        Ok(())
    }

    #[getter]
    fn accounts(&self) -> Vec<PyAccountMeta> {
        self.accounts.clone()
    }

    #[setter]
    fn set_accounts(&mut self, accounts: Vec<Bound<'_, PyAny>>) -> PyResult<()> {
        self.accounts = to_metas(accounts)?;
        Ok(())
    }

    #[getter]
    fn data<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.data)
    }

    #[setter]
    fn set_data(&mut self, data: Vec<u8>) {
        self.data = data;
    }

    fn __repr__(&self) -> String {
        format!(
            "Instruction(program_id={}, accounts={}, data={} bytes)",
            self.program_id.inner,
            self.accounts.len(),
            self.data.len(),
        )
    }
}
