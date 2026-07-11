//! Lightweight, process-global transaction-timing counters for benchmarking.
//!
//! `Account::tx` splits each submission into fine phases and accumulates the
//! wall time of each into an atomic counter here, plus a total transaction
//! count. This isolates pure litesvm engine time (`send`) from the pyo3 /
//! message-assembly work around it, giving a deterministic per-phase breakdown
//! that a sampling profiler can't produce on this platform (no `--native`).
//!
//! Phases (all in `Account::tx` / `build_signed_tx`):
//!   materialize : Python ix objects -> Rust `Instruction` (pyo3 crossing)
//!   compile     : assemble the VersionedMessage (dedup/order accounts, blockhash)
//!   sign        : serialize the message + sign required slots (placeholders off)
//!   hydrate     : fork account hydration (~0 when hermetic)
//!   send        : litesvm engine — the actual Solana VM execution
//!   resultbuild : build PyTxResult (call-trace, logs, split return data)
//!   deliver     : wrap into Py + resolve/raise the failure exception on revert
//!
//! Overhead is a handful of `Instant::now()` reads per tx (~tens of ns each) —
//! negligible next to a tx that costs microseconds-plus. Exposed to Python as
//! `_native.perf_snapshot()` / `_native.perf_reset()`.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use pyo3::prelude::*;

macro_rules! counters {
    ($($name:ident),+ $(,)?) => {
        $( static $name: AtomicU64 = AtomicU64::new(0); )+
    };
}

counters!(TX_COUNT, MATERIALIZE_NS, COMPILE_NS, SIGN_NS, HYDRATE_NS, SEND_NS, RESULTBUILD_NS, DELIVER_NS);

#[inline]
fn add(counter: &AtomicU64, d: Duration) {
    counter.fetch_add(d.as_nanos() as u64, Ordering::Relaxed);
}

#[inline]
pub(crate) fn add_materialize(d: Duration) { add(&MATERIALIZE_NS, d); }
#[inline]
pub(crate) fn add_compile(d: Duration) { add(&COMPILE_NS, d); }
#[inline]
pub(crate) fn add_sign(d: Duration) { add(&SIGN_NS, d); }
#[inline]
pub(crate) fn add_hydrate(d: Duration) { add(&HYDRATE_NS, d); }

/// Records one transaction's engine time and bumps the transaction count.
#[inline]
pub(crate) fn add_send(d: Duration) {
    TX_COUNT.fetch_add(1, Ordering::Relaxed);
    add(&SEND_NS, d);
}

#[inline]
pub(crate) fn add_resultbuild(d: Duration) { add(&RESULTBUILD_NS, d); }
#[inline]
pub(crate) fn add_deliver(d: Duration) { add(&DELIVER_NS, d); }

/// Snapshot of the counters as a plain dict of nanosecond totals + tx count.
#[pyfunction]
fn perf_snapshot() -> HashMap<String, u64> {
    HashMap::from([
        ("tx_count".to_string(), TX_COUNT.load(Ordering::Relaxed)),
        ("materialize_ns".to_string(), MATERIALIZE_NS.load(Ordering::Relaxed)),
        ("compile_ns".to_string(), COMPILE_NS.load(Ordering::Relaxed)),
        ("sign_ns".to_string(), SIGN_NS.load(Ordering::Relaxed)),
        ("hydrate_ns".to_string(), HYDRATE_NS.load(Ordering::Relaxed)),
        ("send_ns".to_string(), SEND_NS.load(Ordering::Relaxed)),
        ("resultbuild_ns".to_string(), RESULTBUILD_NS.load(Ordering::Relaxed)),
        ("deliver_ns".to_string(), DELIVER_NS.load(Ordering::Relaxed)),
    ])
}

/// Zero every counter (call before a measured run).
#[pyfunction]
fn perf_reset() {
    for c in [&TX_COUNT, &MATERIALIZE_NS, &COMPILE_NS, &SIGN_NS, &HYDRATE_NS,
              &SEND_NS, &RESULTBUILD_NS, &DELIVER_NS] {
        c.store(0, Ordering::Relaxed);
    }
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(perf_snapshot, m)?)?;
    m.add_function(wrap_pyfunction!(perf_reset, m)?)?;
    Ok(())
}
