use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

use pyo3::exceptions::{PyLookupError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use solana_address::Address;
use solana_keypair::Keypair;
use solana_message::{v0, Message, VersionedMessage};
use solana_signature::Signature;
use solana_signer::Signer;
use solana_transaction::versioned::VersionedTransaction;

use crate::instruction::PyInstruction;
use crate::{default_svm, deliver, to_py_err, tx_result_from, PyLiteSVM, PyPubkey, PyTxResult};

/// Process-global map of every keypair the harness has created, by address, so
/// `tx` can sign for required signers without the caller re-supplying them.
/// Stores raw 64-byte secrets (`Keypair` is not `Clone`); reconstructed on use.
fn keystore() -> &'static Mutex<HashMap<Address, [u8; 64]>> {
    static KS: OnceLock<Mutex<HashMap<Address, [u8; 64]>>> = OnceLock::new();
    KS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn remember_keypair(kp: &Keypair) {
    keystore().lock().unwrap().insert(kp.pubkey(), kp.to_bytes());
}

/// Draw 32 seed bytes from the harness's single global RNG
/// (`solana_fuzzer.random`). Keeping that one Python `random` as the sole
/// entropy source — rather than Rust `OsRng` (`Keypair::new`) — is what makes a
/// generated keypair, and therefore a whole fuzz run, reproducible from the base
/// `--seed`. The pytest plugin reseeds `solana_fuzzer.random` before every test,
/// so keys are stable given the seed and the flow order.
fn draw_seed(py: Python<'_>) -> PyResult<[u8; 32]> {
    let bytes: Vec<u8> = py
        .import("solana_fuzzer")?
        .getattr("random")?
        .call_method1("randbytes", (32usize,))?
        .extract()?;
    bytes.as_slice().try_into().map_err(|_| {
        PyValueError::new_err("solana_fuzzer.random.randbytes(32) returned wrong length")
    })
}

impl PyAccount {
    /// Build and sign a `VersionedTransaction` with `self` as fee payer — the
    /// shared core of `tx` and `simulate`. Required signers are inferred from
    /// `ixs`; each key is resolved as fee payer -> `signers` -> the global
    /// keystore (see `tx`'s signing-model docs). Does not touch the SVM beyond
    /// reading the latest blockhash.
    ///
    /// When `sigverify` is false (the bound SVM has signature verification off),
    /// a required signer whose key the harness doesn't hold — including the fee
    /// payer — is not an error: its slot gets a placeholder (all-zero) signature,
    /// which the runtime accepts since it isn't verifying signatures. This lets a
    /// transaction be sent "signed" by an account you don't have the key for.
    fn build_signed_tx(
        &self,
        py: Python<'_>,
        ixs: &[Py<PyInstruction>],
        signers: &[Py<PyAccount>],
        lookup_tables: &[Address],
        sigverify: bool,
    ) -> PyResult<VersionedTransaction> {
        // The fee payer must hold a key to sign — unless sigverify is off, in which
        // case its signature isn't checked and it gets a placeholder below.
        if sigverify && self.keypair.is_none() {
            return Err(PyValueError::new_err(
                "fee payer account has no keypair and cannot sign",
            ));
        }

        let sdk_ixs: Vec<_> = ixs.iter().map(|ix| ix.borrow(py).to_sdk()).collect();

        // Secrets explicitly supplied for this transaction, keyed by address.
        let mut explicit: HashMap<Address, [u8; 64]> = HashMap::new();
        for s in signers {
            let acc = s.borrow(py);
            if let Some(kp) = acc.keypair.as_ref() {
                explicit.insert(acc.address.inner, kp.to_bytes());
            }
        }

        // Legacy message when no lookup tables; otherwise a v0 message that
        // sources accounts from the given ALTs (read from the SVM, hydrated
        // first under a fork).
        let message = if lookup_tables.is_empty() {
            let svm = self.svm.borrow(py);
            let blockhash = svm.inner.latest_blockhash();
            VersionedMessage::Legacy(Message::new_with_blockhash(
                &sdk_ixs,
                Some(&self.address.inner),
                &blockhash,
            ))
        } else {
            let mut svm = self.svm.borrow_mut(py);
            let blockhash = svm.inner.latest_blockhash();
            svm.ensure_present(lookup_tables)?;
            let alt_accounts = svm.resolve_alt_accounts(lookup_tables)?;
            let v0_msg =
                v0::Message::try_compile(&self.address.inner, &sdk_ixs, &alt_accounts, blockhash)
                    .map_err(to_py_err)?;
            VersionedMessage::V0(v0_msg)
        };

        // Sign each required signer slot (always among the static keys), resolving
        // its key as fee payer -> explicit `signers=` -> the keystore of created
        // accounts. A slot with no known key raises when `sigverify` is on; when
        // it's off, the slot keeps its placeholder (all-zero) signature.
        let n_sig = message.header().num_required_signatures as usize;
        let static_keys = message.static_account_keys();
        let message_data = message.serialize();
        let mut signatures = vec![Signature::default(); n_sig];
        {
            let ks = keystore().lock().unwrap();
            for (i, addr) in static_keys[..n_sig].iter().enumerate() {
                let secret: Option<[u8; 64]> = if *addr == self.address.inner {
                    self.keypair.as_ref().map(|kp| kp.to_bytes())
                } else if let Some(s) = explicit.get(addr) {
                    Some(*s)
                } else {
                    ks.get(addr).copied()
                };
                match secret {
                    Some(secret) => {
                        let kp = Keypair::try_from(&secret[..]).map_err(to_py_err)?;
                        signatures[i] = kp.sign_message(&message_data);
                    }
                    None if !sigverify => {} // placeholder signature; sigs not verified
                    None => {
                        return Err(PyValueError::new_err(format!(
                            "no keypair known for required signer {addr}; pass it via signers="
                        )));
                    }
                }
            }
        }

        Ok(VersionedTransaction { signatures, message })
    }
}

/// A handle to the account **at an address** in a `LiteSVM` — addressed by its
/// `pubkey`, and the lens through which you read that account's state and
/// (when it can sign) act as fee payer / signer.
///
/// Two things to keep straight, because the name "Account" elides them:
///
/// * **It need not exist on-chain.** On Solana every address has an account
///   *slot*; "doesn't exist" just means empty / zero-lamport / not-yet-created.
///   So an `Account` is a handle to that slot, present or not — check
///   `exists`, and reads like `lamports` / `data` raise until it's funded
///   (e.g. via `svm.airdrop` or `svm.set_account`).
/// * **It may or may not hold a private key.** It carries a keypair (so it can
///   sign / pay — see `can_sign`, `tx`, `simulate`) only when created via
///   `Account.new()` / `Account.from_secret()`. A bare-address view
///   (`Account(pubkey)`) or a derived PDA has no keypair and is read/inspect
///   only.
///
/// Holds no account data of its own — every read delegates to the bound SVM,
/// so the view is always current. The SVM is bound eagerly at construction; if
/// `svm` is omitted the process-global `default_svm` is used.
#[pyclass(name = "Account", module = "solana_fuzzer._native")]
pub struct PyAccount {
    pub(crate) address: PyPubkey,
    svm: Py<PyLiteSVM>,
    keypair: Option<Keypair>,
}

impl PyAccount {
    fn missing(&self) -> PyErr {
        PyLookupError::new_err(format!("account {} does not exist", self.address.inner))
    }

    /// A read-only view of `address` bound to `svm` (no keypair, cannot sign) —
    /// the Rust-side constructor behind `Account(pubkey)`, used to hand back
    /// account handles from other modules (e.g. `svm.forked_accounts()`).
    pub(crate) fn view(address: PyPubkey, svm: Py<PyLiteSVM>) -> Self {
        Self { address, svm, keypair: None }
    }
}

/// Resolve an optional SVM argument, falling back to the process-global one.
fn resolve_svm(py: Python<'_>, svm: Option<Py<PyLiteSVM>>) -> PyResult<Py<PyLiteSVM>> {
    match svm {
        Some(svm) => Ok(svm),
        None => default_svm(py),
    }
}

/// Coerce a list of address-like values into `Address`es (for `lookup_tables`).
fn to_addrs(values: &[Bound<'_, PyAny>]) -> PyResult<Vec<Address>> {
    values.iter().map(|v| Ok(PyPubkey::new(v)?.inner)).collect()
}

#[pymethods]
impl PyAccount {
    /// View the account at `address` — no keypair, cannot sign.
    #[new]
    #[pyo3(signature = (address, svm = None))]
    fn new(
        py: Python<'_>,
        address: &Bound<'_, PyAny>,
        svm: Option<Py<PyLiteSVM>>,
    ) -> PyResult<Self> {
        Ok(Self {
            address: PyPubkey::new(address)?,
            svm: resolve_svm(py, svm)?,
            keypair: None,
        })
    }

    /// Create an account backed by a freshly generated keypair — can sign. The
    /// keypair is derived from the harness's global RNG (`solana_fuzzer.random`),
    /// not OS entropy, so it is reproducible from the base `--seed`.
    #[staticmethod]
    #[pyo3(name = "new", signature = (svm = None))]
    fn generate(py: Python<'_>, svm: Option<Py<PyLiteSVM>>) -> PyResult<Self> {
        let keypair = Keypair::new_from_array(draw_seed(py)?);
        remember_keypair(&keypair);
        Ok(Self {
            address: PyPubkey { inner: keypair.pubkey() },
            svm: resolve_svm(py, svm)?,
            keypair: Some(keypair),
        })
    }

    /// Create an account from a known 64-byte secret key — can sign.
    #[staticmethod]
    #[pyo3(signature = (secret, svm = None))]
    fn from_secret(
        py: Python<'_>,
        secret: &[u8],
        svm: Option<Py<PyLiteSVM>>,
    ) -> PyResult<Self> {
        let keypair = Keypair::try_from(secret)
            .map_err(|e| PyValueError::new_err(format!("invalid secret key: {e}")))?;
        remember_keypair(&keypair);
        Ok(Self {
            address: PyPubkey { inner: keypair.pubkey() },
            svm: resolve_svm(py, svm)?,
            keypair: Some(keypair),
        })
    }

    /// Derive a PDA from `seeds` and `program_id` and return a view of it
    /// bound to `svm` (or the global default), along with the canonical bump.
    #[staticmethod]
    #[pyo3(signature = (seeds, program_id, svm = None))]
    fn find_program_address(
        py: Python<'_>,
        seeds: Vec<Vec<u8>>,
        program_id: &Bound<'_, PyAny>,
        svm: Option<Py<PyLiteSVM>>,
    ) -> PyResult<(PyAccount, u8)> {
        let (address, bump) = PyPubkey::find_program_address(seeds, program_id)?;
        Ok((
            PyAccount {
                address,
                svm: resolve_svm(py, svm)?,
                keypair: None,
            },
            bump,
        ))
    }

    /// Derive a PDA from explicit `seeds` (no bump search) and return a view
    /// of it; errors if the result lands on the ed25519 curve.
    #[staticmethod]
    #[pyo3(signature = (seeds, program_id, svm = None))]
    fn create_program_address(
        py: Python<'_>,
        seeds: Vec<Vec<u8>>,
        program_id: &Bound<'_, PyAny>,
        svm: Option<Py<PyLiteSVM>>,
    ) -> PyResult<PyAccount> {
        Ok(PyAccount {
            address: PyPubkey::create_program_address(seeds, program_id)?,
            svm: resolve_svm(py, svm)?,
            keypair: None,
        })
    }

    #[getter]
    fn pubkey(&self) -> PyPubkey {
        self.address.clone()
    }

    /// The identity label explicitly assigned to this account, or `None`. The
    /// always-a-string display name (which also resolves well-known programs
    /// and falls back to a truncated address) is `str(account)`.
    #[getter]
    fn label(&self, py: Python<'_>) -> PyResult<Option<String>> {
        py.import("solana_fuzzer._labels")?
            .call_method1("get_label", (self.address.inner.to_string(),))?
            .extract()
    }

    /// Assign an identity label to this account's address.
    #[setter]
    fn set_label(&self, py: Python<'_>, name: String) -> PyResult<()> {
        py.import("solana_fuzzer._labels")?
            .call_method1("set_label", (self.address.inner.to_string(), name))?;
        Ok(())
    }

    #[getter]
    fn svm(&self, py: Python<'_>) -> Py<PyLiteSVM> {
        self.svm.clone_ref(py)
    }

    /// Whether this account holds a keypair and can therefore sign.
    #[getter]
    fn can_sign(&self) -> bool {
        self.keypair.is_some()
    }

    /// The 64-byte secret key; raises if the account has no keypair.
    #[getter]
    fn secret<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        match &self.keypair {
            Some(kp) => Ok(PyBytes::new(py, &kp.to_bytes())),
            None => Err(PyValueError::new_err("account has no keypair")),
        }
    }

    #[getter]
    fn exists(&self, py: Python<'_>) -> PyResult<bool> {
        let mut svm = self.svm.borrow_mut(py);
        svm.ensure_present(std::slice::from_ref(&self.address.inner))?;
        Ok(svm.inner.get_account(&self.address.inner).is_some())
    }

    #[getter]
    fn lamports(&self, py: Python<'_>) -> PyResult<u64> {
        let mut svm = self.svm.borrow_mut(py);
        svm.ensure_present(std::slice::from_ref(&self.address.inner))?;
        svm.inner
            .get_account(&self.address.inner)
            .map(|a| a.lamports)
            .ok_or_else(|| self.missing())
    }

    #[getter]
    fn data<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let mut svm = self.svm.borrow_mut(py);
        svm.ensure_present(std::slice::from_ref(&self.address.inner))?;
        match svm.inner.get_account(&self.address.inner) {
            Some(a) => Ok(PyBytes::new(py, &a.data)),
            None => Err(self.missing()),
        }
    }

    #[getter]
    fn owner(&self, py: Python<'_>) -> PyResult<PyPubkey> {
        let mut svm = self.svm.borrow_mut(py);
        svm.ensure_present(std::slice::from_ref(&self.address.inner))?;
        svm.inner
            .get_account(&self.address.inner)
            .map(|a| PyPubkey { inner: a.owner })
            .ok_or_else(|| self.missing())
    }

    #[getter]
    fn executable(&self, py: Python<'_>) -> PyResult<bool> {
        let mut svm = self.svm.borrow_mut(py);
        svm.ensure_present(std::slice::from_ref(&self.address.inner))?;
        svm.inner
            .get_account(&self.address.inner)
            .map(|a| a.executable)
            .ok_or_else(|| self.missing())
    }

    #[getter]
    fn rent_epoch(&self, py: Python<'_>) -> PyResult<u64> {
        let mut svm = self.svm.borrow_mut(py);
        svm.ensure_present(std::slice::from_ref(&self.address.inner))?;
        svm.inner
            .get_account(&self.address.inner)
            .map(|a| a.rent_epoch)
            .ok_or_else(|| self.missing())
    }

    /// Build, sign, and send a transaction with `self` as fee payer.
    ///
    /// The instructions `ixs` execute in order. The recent blockhash is taken
    /// from the bound SVM; the returned result's `call_trace` reflects the
    /// invocations.
    ///
    /// Signing model — two independent things:
    ///
    /// * **Who must sign** is fixed by the instructions (every account flagged
    ///   as a signer). `signers` does NOT change this set: it neither adds
    ///   signers nor replaces the inferred ones. An account in `signers` that
    ///   the instructions don't require to sign has no effect.
    /// * **The private key** for each required signer is resolved
    ///   automatically, trying in order: (1) this fee-payer account, (2) the
    ///   accounts in `signers`, (3) the process-global keystore of every
    ///   account created via `Account.new()` / `Account.from_secret()` (their
    ///   keys register on creation). If none yields a key, `tx` raises.
    ///
    /// So `signers` is purely a *fallback supply of keys* for a required signer
    /// the harness can't otherwise resolve — neither the fee payer nor a
    /// harness-created account. Because created signing accounts auto-register,
    /// you usually don't need it; pass it only when a required signer's keypair
    /// isn't already known. (A keypair-less view in `signers` contributes
    /// nothing.)
    ///
    /// **When the bound SVM has `sigverify` off**, a required signer whose key
    /// the harness can't resolve (including the fee payer) is no longer an error:
    /// its signature slot is left as a placeholder and the runtime accepts it. So
    /// with `svm.sigverify = False` you can send a transaction as an account you
    /// hold no key for — useful for exercising a program's own signer checks.
    #[pyo3(signature = (*ixs, signers = vec![], lookup_tables = vec![]))]
    fn tx(
        &self,
        py: Python<'_>,
        ixs: Vec<Py<PyInstruction>>,
        signers: Vec<Py<PyAccount>>,
        lookup_tables: Vec<Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyTxResult>> {
        let alts = to_addrs(&lookup_tables)?;
        let sigverify = self.svm.borrow(py).sigverify;
        let vtx = self.build_signed_tx(py, &ixs, &signers, &alts, sigverify)?;
        let traced_message = vtx.message.clone();
        let res = {
            let mut svm = self.svm.borrow_mut(py);
            svm.hydrate_for_message(&traced_message)?;
            svm.inner.send_transaction(vtx)
        };
        deliver(py, tx_result_from(res, Some(&traced_message)))
    }

    /// Build, sign, and **simulate** a transaction with `self` as fee payer —
    /// the same shape as `tx`, but it commits **nothing** (state is unchanged).
    ///
    /// The transaction is built and signed identically to `tx` (see its signing
    /// model — `signers` is the same fallback supply of keys), so the fee payer
    /// still matters: it is the message's fee-payer slot, the default signer,
    /// and must have a keypair. Only the final step differs — litesvm's
    /// `simulate_transaction` runs the tx against current state and discards the
    /// result, so balances/accounts are not mutated and there is no `signature`.
    /// The returned `TransactionResult` still carries `logs`, `return_data`,
    /// `compute_units_consumed`, `error`, and the `call_trace`.
    #[pyo3(signature = (*ixs, signers = vec![], lookup_tables = vec![]))]
    fn simulate(
        &self,
        py: Python<'_>,
        ixs: Vec<Py<PyInstruction>>,
        signers: Vec<Py<PyAccount>>,
        lookup_tables: Vec<Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyTxResult>> {
        let alts = to_addrs(&lookup_tables)?;
        let sigverify = self.svm.borrow(py).sigverify;
        let vtx = self.build_signed_tx(py, &ixs, &signers, &alts, sigverify)?;
        let msg = vtx.message.clone();
        let res = {
            let mut svm = self.svm.borrow_mut(py);
            svm.hydrate_for_message(&msg)?;
            svm.simulate_vtx(vtx)
        };
        deliver(py, res)
    }

    fn __eq__(&self, other: &Self) -> bool {
        self.address.inner == other.address.inner
    }

    fn __hash__(&self) -> u64 {
        let bytes = self.address.inner.as_array();
        u64::from_le_bytes(bytes[..8].try_into().unwrap())
    }

    /// The resolved display name: a well-known program/sysvar name, else an
    /// assigned label, else a truncated base58 of the address.
    fn __str__(&self, py: Python<'_>) -> PyResult<String> {
        py.import("solana_fuzzer._labels")?
            .call_method1("resolve_label", (self.address.inner.to_string(),))?
            .extract()
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        let label: Option<String> = py
            .import("solana_fuzzer._labels")?
            .call_method1("get_label", (self.address.inner.to_string(),))?
            .extract()?;
        let mut parts = vec![self.address.inner.to_string()];
        if let Some(l) = label {
            parts.push(format!("{l:?}"));
        }
        if self.keypair.is_some() {
            parts.push("signer".to_string());
        }
        Ok(format!("Account({})", parts.join(", ")))
    }
}
