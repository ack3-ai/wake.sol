//! Mainnet forking: hydrate on-chain accounts into litesvm on first touch.
//!
//! litesvm has no account-load hook (its store is a plain `HashMap` and a miss
//! is silently an empty account), so forking is built at *our* wrapper boundary:
//! `PyLiteSVM::ensure_present` (in `lib.rs`) resolves which keys are missing and
//! calls into this module to fetch them, then injects them via `set_account`.
//! This module owns the JSON-RPC client (blocking `ureq`, no tokio), the disk
//! snapshot store (offline deterministic replay), and the per-session `Fork`
//! state (exclude sets, `seen`/negative cache, reference slot). User-facing
//! behavior is documented in `docs/13-forking.md`.

use std::collections::HashSet;
use std::fs;
use std::path::PathBuf;
use std::str::FromStr;

use base64::Engine;
use serde_json::{json, Value};
use solana_account::Account as SolAccount;
use solana_address::Address;

/// The BPF upgradeable loader — owner of upgradeable *program* accounts, which
/// point at a separate *programdata* account.
fn loader_v3() -> Address {
    Address::from_str("BPFLoaderUpgradeab1e11111111111111111111111")
        .expect("valid bpf_loader_upgradeable id")
}

pub(crate) fn is_loader_v3(owner: &Address) -> bool {
    *owner == loader_v3()
}

/// The programdata account address an upgradeable program at `program` points at
/// (canonical derivation), used to code-skip an excluded program's programdata.
fn programdata_address_of(program: &Address) -> Address {
    let seed: &[u8] = program.as_array();
    Address::find_program_address(&[seed], &loader_v3()).0
}

/// If `data` is an `UpgradeableLoaderState::Program`, return its programdata
/// address. bincode lays the enum out as a 4-byte little-endian discriminant
/// (`Program` == 2) followed by the 32-byte programdata pubkey.
pub(crate) fn programdata_address(data: &[u8]) -> Option<Address> {
    if data.len() >= 36 && data[0..4] == [2, 0, 0, 0] {
        let arr: [u8; 32] = data[4..36].try_into().ok()?;
        Some(Address::new_from_array(arr))
    } else {
        None
    }
}

/// An account fetched from RPC or the snapshot store.
#[derive(Clone)]
pub(crate) struct FetchedAccount {
    pub lamports: u64,
    pub data: Vec<u8>,
    pub owner: Address,
    pub executable: bool,
    pub rent_epoch: u64,
}

impl FetchedAccount {
    pub(crate) fn into_account(self) -> SolAccount {
        SolAccount {
            lamports: self.lamports,
            data: self.data,
            owner: self.owner,
            executable: self.executable,
            rent_epoch: self.rent_epoch,
        }
    }
}

// --- JSON-RPC client (blocking ureq) --------------------------------------- //

pub(crate) struct RpcClient {
    agent: ureq::Agent,
    url: String,
}

impl RpcClient {
    pub(crate) fn new(url: String) -> Self {
        Self { agent: ureq::agent(), url }
    }

    fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        let req = json!({"jsonrpc": "2.0", "id": 1, "method": method, "params": params});
        let body = serde_json::to_string(&req).map_err(|e| e.to_string())?;
        // Parse from the body reader, not `into_string()` — the latter caps at
        // 10 MB, which a large program's base64 programdata response exceeds.
        let reader = self
            .agent
            .post(&self.url)
            .set("Content-Type", "application/json")
            .send_string(&body)
            .map_err(|e| format!("rpc {method} request failed: {e}"))?
            .into_reader();
        let resp: Value =
            serde_json::from_reader(reader).map_err(|e| format!("rpc {method} bad json: {e}"))?;
        if let Some(err) = resp.get("error") {
            return Err(format!("rpc {method} error: {err}"));
        }
        resp.get("result")
            .cloned()
            .ok_or_else(|| format!("rpc {method}: no result"))
    }

    pub(crate) fn get_slot(&self) -> Result<u64, String> {
        self.call("getSlot", json!([{"commitment": "finalized"}]))?
            .as_u64()
            .ok_or_else(|| "getSlot: not a number".to_string())
    }

    /// Best-effort block time for `slot`; `None` if the node can't serve it.
    pub(crate) fn get_block_time(&self, slot: u64) -> Option<i64> {
        self.call("getBlockTime", json!([slot])).ok().and_then(|v| v.as_i64())
    }

    /// The current tip blockhash (base58), used to seed `set_latest_blockhash`
    /// at fork enable.
    pub(crate) fn get_latest_blockhash(&self) -> Result<String, String> {
        let r = self.call("getLatestBlockhash", json!([{"commitment": "finalized"}]))?;
        r.get("value")
            .and_then(|v| v.get("blockhash"))
            .and_then(|v| v.as_str())
            .map(str::to_string)
            .ok_or_else(|| "getLatestBlockhash: no blockhash".to_string())
    }

    /// One batched fetch (≤100 keys) at a `minContextSlot` floor, `finalized`.
    /// A `None` entry means the account is confirmed-absent on chain.
    pub(crate) fn get_multiple_accounts(
        &self,
        keys: &[Address],
        min_context_slot: u64,
    ) -> Result<Vec<Option<FetchedAccount>>, String> {
        let key_strs: Vec<String> = keys.iter().map(|k| k.to_string()).collect();
        let cfg = json!({
            "encoding": "base64",
            "commitment": "finalized",
            "minContextSlot": min_context_slot,
        });
        let r = self.call("getMultipleAccounts", json!([key_strs, cfg]))?;
        let arr = r
            .get("value")
            .and_then(|v| v.as_array())
            .ok_or_else(|| "getMultipleAccounts: no value array".to_string())?;
        arr.iter().map(parse_rpc_account).collect()
    }
}

fn parse_rpc_account(v: &Value) -> Result<Option<FetchedAccount>, String> {
    if v.is_null() {
        return Ok(None);
    }
    let lamports = v.get("lamports").and_then(Value::as_u64).ok_or("account: no lamports")?;
    let owner = Address::from_str(
        v.get("owner").and_then(Value::as_str).ok_or("account: no owner")?,
    )
    .map_err(|e| format!("account: bad owner: {e}"))?;
    let executable = v.get("executable").and_then(Value::as_bool).unwrap_or(false);
    let rent_epoch = v.get("rentEpoch").and_then(Value::as_u64).unwrap_or(0);
    let data = match v.get("data") {
        Some(Value::Array(a)) => {
            let b64 = a.first().and_then(Value::as_str).ok_or("account: no data[0]")?;
            base64::engine::general_purpose::STANDARD
                .decode(b64)
                .map_err(|e| format!("account: bad base64 data: {e}"))?
        }
        _ => return Err("account: unexpected data encoding (need base64)".to_string()),
    };
    Ok(Some(FetchedAccount { lamports, data, owner, executable, rent_epoch }))
}

// --- Snapshot store (disk; offline replay) --------------------------------- //

pub(crate) struct SnapshotStore {
    dir: PathBuf,
}

impl SnapshotStore {
    fn new(dir: PathBuf) -> Self {
        Self { dir }
    }

    fn path(&self, addr: &Address) -> PathBuf {
        self.dir.join(format!("{addr}.json"))
    }

    /// `Some(Some(acc))` = cached present; `Some(None)` = cached confirmed-absent;
    /// `None` = not cached.
    fn get(&self, addr: &Address) -> Option<Option<FetchedAccount>> {
        let bytes = fs::read(self.path(addr)).ok()?;
        let v: Value = serde_json::from_slice(&bytes).ok()?;
        if v.get("absent").and_then(Value::as_bool) == Some(true) {
            return Some(None);
        }
        parse_stored(&v).ok().map(Some)
    }

    fn put(&self, addr: &Address, acc: &Option<FetchedAccount>) {
        if fs::create_dir_all(&self.dir).is_err() {
            return;
        }
        let v = match acc {
            None => json!({ "absent": true }),
            Some(a) => json!({
                "lamports": a.lamports,
                "owner": a.owner.to_string(),
                "executable": a.executable,
                "rent_epoch": a.rent_epoch,
                "data": base64::engine::general_purpose::STANDARD.encode(&a.data),
            }),
        };
        if let Ok(bytes) = serde_json::to_vec(&v) {
            // Atomic write: several forked workers (`wake-sol test -P N`)
            // can fetch and cache the same `<pubkey>.json` at once. Write a
            // process-unique temp in the same dir, then rename over the final
            // path — rename is atomic within a filesystem, so a concurrent
            // reader sees either the old file or the complete new one, never a
            // half-written one. Best-effort throughout, like the plain write.
            let tmp = self.dir.join(format!(".{addr}.{}.tmp", std::process::id()));
            if fs::write(&tmp, bytes).is_ok() {
                if fs::rename(&tmp, self.path(addr)).is_err() {
                    let _ = fs::remove_file(&tmp);
                }
            } else {
                let _ = fs::remove_file(&tmp);
            }
        }
    }
}

fn parse_stored(v: &Value) -> Result<FetchedAccount, String> {
    let lamports = v.get("lamports").and_then(Value::as_u64).ok_or("stored: no lamports")?;
    let owner = Address::from_str(
        v.get("owner").and_then(Value::as_str).ok_or("stored: no owner")?,
    )
    .map_err(|e| format!("stored: bad owner: {e}"))?;
    let executable = v.get("executable").and_then(Value::as_bool).unwrap_or(false);
    let rent_epoch = v.get("rent_epoch").and_then(Value::as_u64).unwrap_or(0);
    let data = base64::engine::general_purpose::STANDARD
        .decode(v.get("data").and_then(Value::as_str).ok_or("stored: no data")?)
        .map_err(|e| format!("stored: bad data: {e}"))?;
    Ok(FetchedAccount { lamports, data, owner, executable, rent_epoch })
}

// --- Per-session fork state ------------------------------------------------ //

pub(crate) struct Fork {
    rpc: Option<RpcClient>,
    store: SnapshotStore,
    /// Program IDs treated as not-on-mainnet: their code is skipped and any
    /// account they *own* is blanked (owner-scoped exclude).
    pub(crate) exclude_programs: HashSet<Address>,
    /// Addresses skipped by exact match: the excluded programs' programdata
    /// accounts (code-skip), plus future `local=[addr]` pins.
    pub(crate) exclude_addrs: HashSet<Address>,
    /// Every address resolved this session (present, absent, or blanked) — the
    /// positive+negative cache; prevents re-fetching. Never forces a write.
    pub(crate) seen: HashSet<Address>,
    pub(crate) min_context_slot: u64,
}

impl Fork {
    pub(crate) fn new(
        rpc: Option<RpcClient>,
        cache: PathBuf,
        exclude_programs: HashSet<Address>,
    ) -> Self {
        let exclude_addrs = exclude_programs.iter().map(programdata_address_of).collect();
        Self {
            rpc,
            store: SnapshotStore::new(cache),
            exclude_programs,
            exclude_addrs,
            seen: HashSet::new(),
            min_context_slot: 0,
        }
    }

    pub(crate) fn rpc(&self) -> Option<&RpcClient> {
        self.rpc.as_ref()
    }

    /// Fetch accounts: snapshot cache first, then RPC (batched, 100-chunked),
    /// writing results back to the store. Offline (`rpc` is `None`) a cache miss
    /// is a hard error — never a silent network read.
    pub(crate) fn fetch(&mut self, keys: &[Address]) -> Result<Vec<Option<FetchedAccount>>, String> {
        let mut out: Vec<Option<FetchedAccount>> = Vec::with_capacity(keys.len());
        let mut miss: Vec<Address> = Vec::new();
        let mut miss_idx: Vec<usize> = Vec::new();
        for (i, k) in keys.iter().enumerate() {
            match self.store.get(k) {
                Some(cached) => out.push(cached),
                None => {
                    out.push(None);
                    miss.push(*k);
                    miss_idx.push(i);
                }
            }
        }
        if miss.is_empty() {
            return Ok(out);
        }
        let rpc = self.rpc.as_ref().ok_or_else(|| {
            format!(
                "offline fork: {} account(s) not in snapshot cache (first: {}); \
                 run online (svm.fork(url=...)) or pre-populate with svm.fork_programs(...)",
                miss.len(),
                miss[0]
            )
        })?;
        let mut fetched: Vec<Option<FetchedAccount>> = Vec::with_capacity(miss.len());
        for chunk in miss.chunks(100) {
            fetched.extend(rpc.get_multiple_accounts(chunk, self.min_context_slot)?);
        }
        for ((k, acc), idx) in miss.iter().zip(fetched.into_iter()).zip(miss_idx.into_iter()) {
            self.store.put(k, &acc);
            out[idx] = acc;
        }
        Ok(out)
    }
}
