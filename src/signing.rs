//! Pure-Rust signing primitives for the secp256k1 / secp256r1 precompiles,
//! exposed to Python as stateless free functions. ed25519 is handled by
//! `Account.sign` (solana-keypair); this module covers the two non-native
//! curves using RustCrypto (`k256` / `p256`) + `sha3` keccak — deliberately no
//! openssl, so the build stays self-contained.
//!
//! Keys are derived deterministically from a 32-byte seed (drawn from the
//! harness's global RNG on the Python side) so a fuzz run stays reproducible
//! from `--seed`, mirroring `Account.new()`.

use k256::ecdsa::SigningKey as K1SigningKey;
use p256::ecdsa::{signature::Signer, Signature as R1Signature, SigningKey as R1SigningKey};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use sha3::{Digest, Keccak256};

fn as_seed(seed: &[u8]) -> PyResult<[u8; 32]> {
    seed.try_into()
        .map_err(|_| PyValueError::new_err("seed must be exactly 32 bytes"))
}

/// keccak256(data) as a 32-byte array.
fn keccak(data: &[u8]) -> [u8; 32] {
    let mut out = [0u8; 32];
    out.copy_from_slice(&Keccak256::digest(data));
    out
}

/// The 20-byte Ethereum address for a secp256k1 signing key: the low 20 bytes of
/// keccak256(uncompressed_pubkey[1..]) (drop the 0x04 tag).
fn k1_eth_address(sk: &K1SigningKey) -> [u8; 20] {
    let point = sk.verifying_key().to_encoded_point(false);
    let hash = keccak(&point.as_bytes()[1..]);
    let mut addr = [0u8; 20];
    addr.copy_from_slice(&hash[12..]);
    addr
}

// --- secp256k1 (Ethereum-style) --------------------------------------------- //

/// Derive a valid secp256k1 secret scalar from `seed` deterministically
/// (rehashing on the astronomically-rare invalid-scalar case).
#[pyfunction]
fn secp256k1_secret_from_seed<'py>(py: Python<'py>, seed: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
    let mut cur = as_seed(seed)?;
    loop {
        if K1SigningKey::from_slice(&cur).is_ok() {
            return Ok(PyBytes::new(py, &cur));
        }
        cur = keccak(&cur);
    }
}

/// The 20-byte Ethereum address for a 32-byte secp256k1 secret.
#[pyfunction]
fn secp256k1_eth_address<'py>(py: Python<'py>, secret: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
    let sk = K1SigningKey::from_slice(secret)
        .map_err(|e| PyValueError::new_err(format!("invalid secp256k1 secret: {e}")))?;
    Ok(PyBytes::new(py, &k1_eth_address(&sk)))
}

/// Sign `message` with a secp256k1 secret, Ethereum-style: keccak256 the message,
/// then produce a recoverable ECDSA signature. Returns `(signature[64],
/// recovery_id)` — the pair the secp256k1 precompile recovers against.
#[pyfunction]
fn secp256k1_sign<'py>(
    py: Python<'py>,
    secret: &[u8],
    message: &[u8],
) -> PyResult<(Bound<'py, PyBytes>, u8)> {
    let sk = K1SigningKey::from_slice(secret)
        .map_err(|e| PyValueError::new_err(format!("invalid secp256k1 secret: {e}")))?;
    let (sig, recid) = sk
        .sign_prehash_recoverable(&keccak(message))
        .map_err(|e| PyValueError::new_err(format!("secp256k1 sign failed: {e}")))?;
    Ok((PyBytes::new(py, &sig.to_bytes()), recid.to_byte()))
}

// --- secp256r1 (NIST P-256) ------------------------------------------------- //

/// Derive a valid secp256r1 secret scalar from `seed` deterministically.
#[pyfunction]
fn secp256r1_secret_from_seed<'py>(py: Python<'py>, seed: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
    let mut cur = as_seed(seed)?;
    loop {
        if R1SigningKey::from_slice(&cur).is_ok() {
            return Ok(PyBytes::new(py, &cur));
        }
        cur = keccak(&cur);
    }
}

/// The 33-byte compressed public key for a 32-byte secp256r1 secret.
#[pyfunction]
fn secp256r1_public_key<'py>(py: Python<'py>, secret: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
    let sk = R1SigningKey::from_slice(secret)
        .map_err(|e| PyValueError::new_err(format!("invalid secp256r1 secret: {e}")))?;
    let point = sk.verifying_key().to_encoded_point(true);
    Ok(PyBytes::new(py, point.as_bytes()))
}

/// Sign `message` with a secp256r1 secret (ECDSA/P-256 over SHA-256), returning a
/// 64-byte compact signature with **low-S** normalization — the precompile
/// rejects high-S signatures (malleability guard).
#[pyfunction]
fn secp256r1_sign<'py>(
    py: Python<'py>,
    secret: &[u8],
    message: &[u8],
) -> PyResult<Bound<'py, PyBytes>> {
    let sk = R1SigningKey::from_slice(secret)
        .map_err(|e| PyValueError::new_err(format!("invalid secp256r1 secret: {e}")))?;
    let sig: R1Signature = sk.sign(message);
    let sig = sig.normalize_s().unwrap_or(sig);
    Ok(PyBytes::new(py, &sig.to_bytes()))
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(secp256k1_secret_from_seed, m)?)?;
    m.add_function(wrap_pyfunction!(secp256k1_eth_address, m)?)?;
    m.add_function(wrap_pyfunction!(secp256k1_sign, m)?)?;
    m.add_function(wrap_pyfunction!(secp256r1_secret_from_seed, m)?)?;
    m.add_function(wrap_pyfunction!(secp256r1_public_key, m)?)?;
    m.add_function(wrap_pyfunction!(secp256r1_sign, m)?)?;
    Ok(())
}
