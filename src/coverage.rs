//! Source-line coverage for the SBF programs under test.
//!
//! # How it works
//!
//! agave's SBF VM can record the register file after every executed
//! instruction. The switch is `solana_sbpf::vm::Config::enable_register_tracing`,
//! which both the interpreter and the JIT honour; the bpf-loader moves each
//! invocation's trace into the `InvokeContext`, and litesvm hands the finished
//! context to an [`InvocationInspectCallback`] after every transaction. Register
//! 11 is the program counter, so the trace is — for our purposes — a stream of
//! executed instruction indices.
//!
//! Turning that into `file:line` needs the program's own DWARF, which
//! `cargo build-sbf` emits into `target/sbpf-solana-solana/release/*.so` when the
//! program sets `[profile.release] debug = true`. That file is *not* the one you
//! deploy, and it cannot be: turning tracing on also sets
//! `enable_symbol_and_section_labels`, under which agave's ELF loader walks the
//! symbol table and rejects the program outright if any `STT_FUNC` symbol falls
//! outside `.text`'s VM range (`solana_sbpf::elf`, "Register all known function
//! names from the symbol table"). An unstripped build trips that and fails to
//! load as `InvalidAccountData` — but only when coverage is on, which makes it a
//! confusing failure to hit by accident. Strip `.symtab` and the same ELF loads
//! fine, so it is the symbol table specifically, not the debug sections.
//!
//! So the debug ELF is a *sidecar*: deploy `target/deploy/*.so` as usual, and
//! point coverage at the unstripped sibling. Both come out of one build, so
//! their `.text` is identical and the pcs line up exactly.
//! [`add_debug_elf`](coverage_add_debug_elf) registers one explicitly, and
//! `add_program_from_file` finds the sibling on its own for the standard
//! `cargo build-sbf` layout. If the deployed ELF happens to carry DWARF itself
//! (debug sections but no symbol table), that is used directly and no sidecar is
//! needed.
//!
//! Resolving against the *wrong* build would silently produce plausible, wrong
//! line numbers — the worst failure mode an auditing tool can have — so a
//! registered sidecar is accepted only if its `.text` matches the deployed one.
//!
//! An instruction's file address is `.text`'s vaddr + `pc * 8`, which the
//! DWARF line table maps to a source location.
//!
//! # Counting: why `max` and not `+= 1`
//!
//! A source line compiles to many instructions, so the obvious fold — walk the
//! trace, bump the line each instruction lands on — reports a one-line statement
//! as having run eleven times. This is not hypothetical: it is the documented
//! "Known problems" section of both DWARF-based tools in this space, and their
//! own committed fixtures show `DA:5,11` for a single execution.
//!
//! The upstream mitigation is to collapse *consecutive* trace entries sharing a
//! `(file, line)`. That helps, but being tied to execution order it reports a
//! single-line loop body as one hit however many times it spins.
//!
//! What LCOV actually asks for is the count gcov produces: a line's execution
//! count comes from the count of the *basic blocks* on it (`line->count +=
//! block->count`), never from instructions retired. So we charge a line once per
//! block entry, on the address axis rather than the time axis:
//!
//! - only `is_stmt` rows of the line table count. That flag marks the compiler's
//!   recommended breakpoint for a statement, and for LLVM output it amounts to
//!   "the first instruction attributed to this line". Every line-oriented DWARF
//!   consumer filters on it — gdb, kcov, cargo-tarpaulin — and notably
//!   `addr2line` discards it, which is why the line program is driven directly
//!   here;
//! - rows are walked in address order, and consecutive rows for the same line
//!   are one run charged once, so a statement spanning many instructions counts
//!   once per execution;
//! - across non-adjacent runs the line takes the **maximum**, not the sum. A
//!   line routinely owns several blocks — a macro expansion, a prologue and its
//!   epilogue — and a single pass through the line enters all of them, so
//!   summing multiplies the count by however many blocks the compiler chose.
//!
//! A loop therefore reports its true iteration count, and three transactions
//! through a `msg!` report 3 — where summing reports 9 and charging per
//! instruction reports 11.
//!
//! The cost is an undercount where one line is genuinely reached through
//! distinct blocks, such as a function inlined at several call sites: it reports
//! the busiest site rather than the total. That errs toward claiming *less*
//! coverage than was achieved, which is the safe direction here, and it does not
//! arise at the `opt-level = 0` this feature asks for.
//!
//! # Which file a line belongs to
//!
//! The SBF toolchain emits **no `DW_AT_comp_dir`**, so a locally compiled crate
//! records its sources as bare relative paths — `src/lib.rs`. In a minimal
//! program eighteen crates do that, `native_counter` and `borsh` and `sha2`
//! alike, and naively trusting the path merges all of their coverage into the
//! program's own file. Units are instead identified by the crate embedded in
//! `DW_AT_name` (`src/lib.rs/@/<crate>.<hash>-cgu.0`), and a relative path is
//! resolved only against a source root belonging to that same crate, and only if
//! the file is really there.
//!
//! # Known limitation: inlining
//!
//! A pc inside inlined code carries the *callee's* line, so an inlined helper is
//! credited to the library it came from rather than to the call site, and is
//! then dropped for being outside the source roots. Every tool in this space
//! behaves this way. It matters little at the `opt-level = 0` this feature asks
//! for, where nothing is inlined, and resolving it means walking the
//! `DW_TAG_inlined_subroutine` chain to find the outermost frame inside the
//! user's tree.

use std::collections::{BTreeMap, HashMap};
use std::path::Path;
use std::str::FromStr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use litesvm::{InvocationInspectCallback, LiteSVM};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use solana_address::Address;
use solana_program_runtime::invoke_context::{Executable, InvokeContext, RegisterTrace};
use solana_transaction::sanitized::SanitizedTransaction;
use solana_transaction_context::{IndexOfAccount, InstructionContext};

/// Bytes per SBF instruction. A `pc` is an index into `.text`, so the file
/// address of instruction `pc` is `text_vaddr + pc * INSN_SIZE`. `lddw` occupies
/// two slots, which costs nothing here: the second slot is simply a pc that
/// never appears in a trace.
const INSN_SIZE: u64 = 8;

// --------------------------------------------------------------------------- #
// Runtime collection
// --------------------------------------------------------------------------- #

/// Per-program execution counts, indexed densely by pc.
struct ProgramCounters {
    /// `hits[pc]` = times instruction `pc` executed. Dense rather than a map:
    /// this is bumped once per executed instruction on the VM's hot path, and a
    /// program's `.text` is bounded by the loader, so one `u64` per instruction
    /// slot is both smaller and faster than hashing.
    hits: Vec<u64>,
    /// The deployed ELF, captured on first sight. Programs are immutable once
    /// deployed (an upgrade deploys a new one), so this is captured once and
    /// never refreshed.
    elf: Option<Vec<u8>>,
}

impl ProgramCounters {
    fn new(n_insns: usize) -> Self {
        Self { hits: vec![0; n_insns], elf: None }
    }
}

#[derive(Default)]
struct Counters {
    programs: HashMap<Address, ProgramCounters>,
    /// Unstripped sidecar ELFs, keyed by the program they describe. Registered
    /// explicitly or discovered next to a deployed `.so`; see the module docs
    /// for why the debug build cannot simply be deployed.
    debug_elfs: HashMap<Address, Vec<u8>>,
}

/// Process-global counters, shared by every `LiteSVM` this process builds.
///
/// Deliberately *outside* the SVM: the pytest plugin resets the global SVM
/// before every test, and coverage is a property of the whole session, not of
/// one test. The callback installed into each new SVM holds a clone of this
/// `Arc`, so counts survive any number of resets.
static COUNTERS: OnceLock<Arc<Mutex<Counters>>> = OnceLock::new();

/// Whether new SVMs should be built with register tracing on.
///
/// Read by `base_svm`. Tracing is compiled into a program when it is *loaded*,
/// so this has to be set before any SVM is constructed — flipping it later
/// leaves already-loaded programs untraced. The pytest plugin sets it during
/// session start, ahead of the first SVM.
static ENABLED: AtomicBool = AtomicBool::new(false);

fn counters() -> &'static Arc<Mutex<Counters>> {
    COUNTERS.get_or_init(|| Arc::new(Mutex::new(Counters::default())))
}

/// Poisoning only means some other thread panicked mid-update; the counts are
/// still structurally sound and coverage is diagnostic, so we take the data
/// rather than propagating a panic into an unrelated test.
macro_rules! lock {
    ($m:expr) => {
        match $m.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        }
    };
}

pub(crate) fn is_enabled() -> bool {
    ENABLED.load(Ordering::Relaxed)
}

/// Register an unstripped ELF as the debug source for `program_id`.
///
/// Called from `add_program_from_file`'s sibling lookup as well as from Python.
/// Storing the bytes rather than the path means a later `report()` cannot be
/// derailed by a rebuild that happened in between.
pub(crate) fn register_debug_elf(program_id: Address, bytes: Vec<u8>) {
    lock!(counters()).debug_elfs.insert(program_id, bytes);
}

/// Does this ELF carry the line table we need?
///
/// `.debug_info` alone is not enough — the line program is what maps addresses
/// to lines, and a build can be stripped of one without the other.
pub(crate) fn has_line_table(elf: &[u8]) -> bool {
    use object::{Object, ObjectSection};
    object::File::parse(elf)
        .ok()
        .and_then(|obj| obj.section_by_name(".debug_line").map(|s| s.size() > 0))
        .unwrap_or(false)
}

/// The callback litesvm invokes after each transaction.
pub(crate) struct CoverageCallback {
    counters: Arc<Mutex<Counters>>,
}

impl CoverageCallback {
    pub(crate) fn new() -> Self {
        Self { counters: Arc::clone(counters()) }
    }
}

impl InvocationInspectCallback for CoverageCallback {
    fn before_invocation(
        &self,
        _: &LiteSVM,
        _: &SanitizedTransaction,
        _: &[IndexOfAccount],
        _: &InvokeContext,
    ) {
    }

    fn after_invocation(
        &self,
        svm: &LiteSVM,
        invoke_context: &InvokeContext,
        register_tracing_enabled: bool,
    ) {
        if !register_tracing_enabled {
            return;
        }
        // One trace per program invocation, CPI included. The `InvokeContext` is
        // built fresh per transaction, so these are exactly this transaction's
        // invocations — there is no carry-over to subtract.
        invoke_context.iterate_vm_traces(
            &|instruction_context: InstructionContext,
              executable: &Executable,
              register_trace: RegisterTrace| {
                self.record(svm, instruction_context, executable, register_trace);
            },
        );
    }
}

impl CoverageCallback {
    fn record(
        &self,
        svm: &LiteSVM,
        instruction_context: InstructionContext,
        executable: &Executable,
        register_trace: RegisterTrace,
    ) {
        let Ok(program_id) = instruction_context.get_program_key() else {
            return;
        };
        let (_vm_addr, text) = executable.get_text_bytes();
        let n_insns = text.len() / INSN_SIZE as usize;

        // Locked per invocation, not per instruction: a transaction has a
        // handful of invocations but can have hundreds of thousands of
        // instructions.
        let mut counters = lock!(self.counters);
        let entry = counters
            .programs
            .entry(*program_id)
            .or_insert_with(|| ProgramCounters::new(n_insns));

        if entry.elf.is_none() {
            if let Ok(bytes) = svm.accounts_db().try_program_elf_bytes(program_id) {
                entry.elf = Some(bytes.to_vec());
            }
        }

        for regs in register_trace.iter() {
            // r11 is the program counter.
            let pc = regs[11] as usize;
            if let Some(slot) = entry.hits.get_mut(pc) {
                *slot = slot.saturating_add(1);
            }
        }
    }
}

// --------------------------------------------------------------------------- #
// DWARF resolution
// --------------------------------------------------------------------------- #

/// What we learned about one program while resolving it.
struct ProgramReport {
    program_id: String,
    /// Instructions executed at least once (distinct pcs, not total retired).
    instructions_covered: u64,
    /// Size of `.text` in instructions.
    instructions_total: u64,
    /// Whether the resolved ELF carries a line table at all. False is the most
    /// common reason for an empty report (built without `debug = true`), so it
    /// is surfaced rather than left to be inferred from a blank table.
    has_debug_info: bool,
    /// Statement rows that survived source-root filtering. Zero *with*
    /// `has_debug_info` is the other misconfiguration — the program has line
    /// info, but none of it lives under the roots the caller gave.
    lines_resolved: u64,
    /// Set when a sidecar was registered but its `.text` does not match the
    /// deployed program. Resolving anyway would emit confident, wrong line
    /// numbers, so the program is skipped and this says why.
    text_mismatch: bool,
    /// SHA-256 of the deployed `.text`: which build these line numbers describe.
    ///
    /// Within one run the sidecar is checked against the deployed program, but
    /// that check does not survive into an LCOV file — so merging two runs of
    /// *different* builds would silently combine line numbers that mean
    /// different things, and look perfectly healthy doing it. Reports carry this
    /// so a merge can refuse.
    text_sha256: String,
}

type LineCounts = BTreeMap<String, BTreeMap<u32, u64>>;

/// A directory of sources, tagged with the crate compiled from it.
///
/// The crate name is what makes a relative DWARF path resolvable; see
/// [`resolve_path`]. Python derives both from the source roots the caller gave,
/// reading `[package] name` out of the governing `Cargo.toml`.
struct RootSpec {
    dir: std::path::PathBuf,
    crate_name: String,
}

/// Resolve one program's pc counts into `file -> line -> count`.
///
/// Returns the per-file counts merged into `out`, plus a diagnostic record.
fn resolve_program(
    program_id: &Address,
    counters: &ProgramCounters,
    debug_elf: Option<&[u8]>,
    roots: &[RootSpec],
    out: &mut LineCounts,
) -> ProgramReport {
    let mut report = ProgramReport {
        program_id: program_id.to_string(),
        instructions_covered: counters.hits.iter().filter(|h| **h > 0).count() as u64,
        instructions_total: counters.hits.len() as u64,
        has_debug_info: false,
        lines_resolved: 0,
        text_mismatch: false,
        text_sha256: String::new(),
    };

    use object::{Object, ObjectSection};

    // Identity of the code that actually ran, recorded before any of the ways
    // resolution can bail out — a report that resolved nothing still needs to
    // say which build it was about.
    if let Some(text) = counters.elf.as_deref().and_then(text_of) {
        report.text_sha256 = sha256_hex(&text);
    }

    // A registered sidecar wins, but only if it describes the code that actually
    // ran; otherwise fall back to the deployed bytes, which carry DWARF in the
    // stripped-symtab case.
    let elf = match (debug_elf, counters.elf.as_deref()) {
        (Some(sidecar), Some(deployed)) => {
            if text_matches(text_of(sidecar).as_deref(), text_of(deployed).as_deref()) {
                sidecar
            } else {
                report.text_mismatch = true;
                return report;
            }
        }
        (Some(sidecar), None) => sidecar,
        (None, Some(deployed)) => deployed,
        (None, None) => return report,
    };

    let Ok(obj) = object::File::parse(elf) else {
        return report;
    };

    report.has_debug_info = has_line_table(elf);

    let cache_key = format!(
        "{}\u{3}{}\u{3}{}",
        program_id,
        report.text_sha256,
        roots_fingerprint(roots)
    );
    let cached = lock!(rows_cache()).get(&cache_key).cloned();
    let table = match cached {
        Some(table) => table,
        None => {
            // `.text`'s vaddr anchors pc -> file address. Without it we cannot
            // place a single instruction, so bail rather than guess at 0.
            let Some(text_addr) = obj.section_by_name(".text").map(|s| s.address()) else {
                return report;
            };
            let endian = if obj.is_little_endian() {
                gimli::RunTimeEndian::Little
            } else {
                gimli::RunTimeEndian::Big
            };
            let load_section = |id: gimli::SectionId| -> Result<gimli::EndianSlice<'_, gimli::RunTimeEndian>, gimli::Error> {
                let data = obj
                    .section_by_name(id.name())
                    .and_then(|s| s.data().ok())
                    .unwrap_or(&[]);
                Ok(gimli::EndianSlice::new(data, endian))
            };
            let Ok(dwarf) = gimli::Dwarf::load(load_section) else {
                return report;
            };
            let mut rows = match statement_rows(&dwarf, roots) {
                Ok(rows) => rows,
                Err(_) => return report,
            };
            // Address order is what makes "contiguous run" meaningful below; the
            // line program emits rows per sequence, not globally sorted. Sorted
            // once, before caching.
            rows.sort_by_key(|r| r.address);
            let table = Arc::new((text_addr, rows));
            lock!(rows_cache()).insert(cache_key, Arc::clone(&table));
            table
        }
    };

    let (text_addr, rows) = (table.0, &table.1);
    report.lines_resolved = rows.len() as u64;
    if rows.is_empty() {
        return report;
    }

    let hits_at = |addr: u64| -> u64 {
        if addr < text_addr {
            return 0;
        }
        let pc = (addr - text_addr) / INSN_SIZE;
        counters.hits.get(pc as usize).copied().unwrap_or(0)
    };

    let mut lines: LineCounts = BTreeMap::new();
    let mut prev: Option<(&str, u32)> = None;
    for row in rows.iter() {
        let key = (row.file.as_str(), row.line);
        // Every line that has code is seeded, executed or not — a coverage
        // report without its zeroes has no denominator.
        let slot = lines
            .entry(row.file.clone())
            .or_default()
            .entry(row.line)
            .or_insert(0);
        // One charge per *run* — consecutive rows for a line are that statement
        // continuing, and charging each would report the line as having run once
        // per instruction it compiled into. Across runs take the maximum, not
        // the sum: a line routinely owns several non-adjacent blocks (a macro
        // expansion, a prologue and its epilogue), and every one of them is
        // entered on the same single pass through the line. Summing reports
        // `msg!` as having run nine times for three transactions; the maximum
        // reports three.
        if prev != Some(key) {
            *slot = (*slot).max(hits_at(row.address));
        }
        prev = Some(key);
    }

    for (file, file_lines) in lines {
        let dst = out.entry(file).or_default();
        for (line, count) in file_lines {
            let slot = dst.entry(line).or_insert(0);
            *slot = slot.saturating_add(count);
        }
    }

    report
}

/// One statement-boundary row of the DWARF line table.
struct StmtRow {
    address: u64,
    file: String,
    line: u32,
}

/// Parsed line tables, reused across reports.
///
/// Resolving a program means parsing its DWARF and walking every line program —
/// work that depends only on the ELF and the source roots, never on what ran.
/// Measured at 8-13ms per report for programs of this size and *identical*
/// whether ten transactions have executed or a hundred, so a report loop would
/// otherwise pay it again every time for no reason.
///
/// Keyed by program, by the `.text` digest (so a redeployed or upgraded program
/// re-parses rather than resolving against stale lines) and by the source roots
/// (which filter the rows, so a report with different roots is a different
/// table).
type RowCache = HashMap<String, Arc<(u64, Vec<StmtRow>)>>;

static ROWS: OnceLock<Mutex<RowCache>> = OnceLock::new();

fn rows_cache() -> &'static Mutex<RowCache> {
    ROWS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn roots_fingerprint(roots: &[RootSpec]) -> String {
    roots
        .iter()
        .map(|r| format!("{}\u{1}{}", r.dir.display(), r.crate_name))
        .collect::<Vec<_>>()
        .join("\u{2}")
}

/// Every `is_stmt` row of every compilation unit, resolved to a real file.
///
/// `is_stmt` marks the rows the compiler considers a recommended breakpoint —
/// the beginning of a statement. Filtering on it is what every line-oriented
/// DWARF consumer does (gdb, kcov, cargo-tarpaulin), and for LLVM output it
/// amounts to "the first instruction attributed to this line", which is exactly
/// the boundary a hit count should be charged to.
fn statement_rows<R: gimli::Reader>(
    dwarf: &gimli::Dwarf<R>,
    roots: &[RootSpec],
) -> Result<Vec<StmtRow>, gimli::Error> {
    let mut out = Vec::new();
    let mut units = dwarf.units();
    while let Some(header) = units.next()? {
        let unit = dwarf.unit(header)?;
        let unit_ref = unit.unit_ref(dwarf);
        let krate = unit_crate_name(&unit_ref);

        let Some(program) = unit.line_program.clone() else {
            continue;
        };
        // Resolving a path means touching the filesystem, and a unit's rows
        // reference the same handful of files thousands of times over.
        let mut resolved: HashMap<u64, Option<String>> = HashMap::new();
        let mut rows = program.rows();
        while let Some((line_header, row)) = rows.next_row()? {
            if row.end_sequence() || !row.is_stmt() {
                continue;
            }
            let Some(line) = row.line() else {
                continue; // line 0: compiler-generated, belongs to no statement
            };
            let index = row.file_index();
            let file = match resolved.entry(index) {
                std::collections::hash_map::Entry::Occupied(e) => e.get().clone(),
                std::collections::hash_map::Entry::Vacant(e) => {
                    let path = row
                        .file(line_header)
                        .and_then(|f| compose_path(&unit_ref, line_header, f).ok())
                        .flatten();
                    e.insert(path.and_then(|p| resolve_path(&p, krate.as_deref(), roots)))
                        .clone()
                }
            };
            if let Some(file) = file {
                let line = line.get() as u32;
                // A line past the end of the file is proof of a misattribution:
                // in a build without `--disable-remap-cwd`, one crate's
                // `src/lib.rs` is indistinguishable from another's, and generics
                // instantiated across crates land in the wrong file. This cannot
                // reject a genuine row, so it is a free safety net.
                if line as usize > line_count(&file) {
                    continue;
                }
                out.push(StmtRow { address: row.address(), file, line });
            }
        }
    }
    Ok(out)
}

/// The crate a compilation unit belongs to.
///
/// The SBF toolchain emits no `DW_AT_comp_dir` and names each unit
/// `src/lib.rs/@/<crate>.<hash>-cgu.<n>`, so this is the only thing separating
/// one crate's `src/lib.rs` from another's — and in a typical program eighteen
/// crates claim that same path. Without this every dependency's coverage would
/// be merged into the program's own file.
fn unit_crate_name<R: gimli::Reader>(unit: &gimli::UnitRef<'_, R>) -> Option<String> {
    let name = unit.name.as_ref()?.to_string_lossy().ok()?;
    let tail = name.split("/@/").nth(1)?;
    Some(tail.split('.').next()?.to_string())
}

/// A line-table file entry as a path: compilation directory, then the entry's
/// directory, then its name — each replacing what came before if it is itself
/// absolute.
///
/// `DW_AT_comp_dir` is the piece that makes a path unambiguous, and it is
/// present only when the program was built with `--disable-remap-cwd`. Without
/// it everything below the compilation directory stays relative, and
/// [`resolve_path`] has to fall back to matching by crate.
fn compose_path<R: gimli::Reader>(
    unit: &gimli::UnitRef<'_, R>,
    header: &gimli::LineProgramHeader<R>,
    file: &gimli::FileEntry<R>,
) -> Result<Option<String>, gimli::Error> {
    let mut path = String::new();
    if let Some(comp_dir) = unit.comp_dir.as_ref() {
        push_component(&mut path, comp_dir.to_string_lossy()?.as_ref());
    }
    // Directory index 0 is the compilation unit's own directory, already
    // covered by comp_dir above.
    if file.directory_index() != 0 {
        if let Some(dir) = file.directory(header) {
            push_component(&mut path, unit.attr_string(dir)?.to_string_lossy()?.as_ref());
        }
    }
    push_component(
        &mut path,
        unit.attr_string(file.path_name())?.to_string_lossy()?.as_ref(),
    );
    Ok(Some(path))
}

/// Number of lines in a source file, cached.
///
/// An unreadable file imposes no bound (`usize::MAX`) rather than a bound of
/// zero: the check exists to catch misattribution, and must never be the reason
/// a report comes back empty.
///
/// Process-global because the same handful of files is asked about across every
/// program and every report.
fn line_count(path: &str) -> usize {
    static CACHE: OnceLock<Mutex<HashMap<String, usize>>> = OnceLock::new();
    let cache = CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    if let Some(n) = lock!(cache).get(path) {
        return *n;
    }
    let n = std::fs::read_to_string(path)
        .map(|s| s.lines().count())
        .unwrap_or(usize::MAX);
    lock!(cache).insert(path.to_string(), n);
    n
}

fn push_component(path: &mut String, component: &str) {
    if component.starts_with('/') {
        path.clear();
    } else if !path.is_empty() && !path.ends_with('/') {
        path.push('/');
    }
    path.push_str(component);
}

/// Place a DWARF path in the user's tree, or reject it.
///
/// Absolute paths (the prebuilt toolchain's, and dependencies compiled
/// elsewhere) are kept only if they fall under a source root. Relative paths
/// carry no directory at all, so they are resolved against the root belonging to
/// the same crate as the unit that emitted them, and only if the file is really
/// there — which is what keeps `borsh`'s `src/lib.rs` out of the program's.
fn resolve_path(path: &str, krate: Option<&str>, roots: &[RootSpec]) -> Option<String> {
    let candidate = Path::new(path);
    if candidate.is_absolute() {
        if roots.is_empty() || roots.iter().any(|r| candidate.starts_with(&r.dir)) {
            return Some(path.to_string());
        }
        return None;
    }
    let krate = krate?;
    for root in roots {
        if root.crate_name != krate {
            continue;
        }
        let joined = root.dir.join(candidate);
        if joined.is_file() {
            return Some(joined.to_string_lossy().into_owned());
        }
    }
    None
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(bytes).iter().fold(String::new(), |mut s, b| {
        use std::fmt::Write;
        let _ = write!(s, "{b:02x}");
        s
    })
}

/// The `.text` image of an ELF, if it parses and has one.
fn text_of(elf: &[u8]) -> Option<Vec<u8>> {
    use object::{Object, ObjectSection};
    let obj = object::File::parse(elf).ok()?;
    let text = obj.section_by_name(".text")?;
    text.data().ok().map(|d| d.to_vec())
}

/// Do two `.text` images describe the same code?
///
/// Compared instruction-wise rather than byte-wise because the loader rewrites
/// the 4-byte immediate of every `call` in place, replacing the relative target
/// with an internal function key. A deployed image and the build it came from
/// therefore differ at exactly those bytes while being the same program, so the
/// immediate is excluded and the rest of the instruction still has to match.
fn text_matches(a: Option<&[u8]>, b: Option<&[u8]>) -> bool {
    let (Some(a), Some(b)) = (a, b) else {
        return false;
    };
    if a.len() != b.len() {
        return false;
    }
    const CALL_IMM: u8 = 0x85;
    for (ia, ib) in a.chunks_exact(8).zip(b.chunks_exact(8)) {
        if ia[0] == CALL_IMM && ib[0] == CALL_IMM {
            if ia[..4] != ib[..4] {
                return false;
            }
            continue;
        }
        if ia != ib {
            return false;
        }
    }
    true
}

// --------------------------------------------------------------------------- #
// Reporting
// --------------------------------------------------------------------------- #

fn build_report(roots: &[(String, String)]) -> (LineCounts, Vec<ProgramReport>) {
    let roots: Vec<RootSpec> = roots
        .iter()
        .map(|(dir, crate_name)| RootSpec {
            dir: std::path::PathBuf::from(dir),
            crate_name: crate_name.clone(),
        })
        .collect();
    let counters = lock!(counters());
    let mut lines: LineCounts = BTreeMap::new();
    let mut programs = Vec::new();
    for (program_id, program) in counters.programs.iter() {
        let debug_elf = counters.debug_elfs.get(program_id).map(Vec::as_slice);
        programs.push(resolve_program(
            program_id,
            program,
            debug_elf,
            &roots,
            &mut lines,
        ));
    }
    programs.sort_by(|a, b| a.program_id.cmp(&b.program_id));
    (lines, programs)
}

// --------------------------------------------------------------------------- #
// Python surface
// --------------------------------------------------------------------------- #

/// Arm coverage for SVMs built from here on.
///
/// Must run before the first SVM is constructed: register tracing is baked into
/// a program's executable when it is loaded, so enabling it afterwards would
/// silently collect nothing.
#[pyfunction]
fn coverage_enable() {
    ENABLED.store(true, Ordering::Relaxed);
}

/// Disarm coverage. SVMs built after the next `reset()` stop tracing; counts
/// already collected are kept until [`coverage_reset`].
#[pyfunction]
fn coverage_disable() {
    ENABLED.store(false, Ordering::Relaxed);
}

#[pyfunction]
fn coverage_enabled() -> bool {
    is_enabled()
}

/// Drop every counter, keeping coverage armed.
///
/// Registered debug ELFs are kept: they describe the programs, not the run, and
/// re-reading them on every reset would be pure cost.
#[pyfunction]
fn coverage_reset() {
    lock!(counters()).programs.clear();
}

/// Point coverage at the unstripped build of an already-deployed program.
///
/// `program_id` is base58. The ELF is read now and kept, so a later rebuild
/// cannot change what a report resolves against.
#[pyfunction]
fn coverage_add_debug_elf(program_id: &str, path: &str) -> PyResult<()> {
    let address = Address::from_str(program_id)
        .map_err(|e| PyRuntimeError::new_err(format!("bad program id {program_id}: {e}")))?;
    let bytes = std::fs::read(path)
        .map_err(|e| PyRuntimeError::new_err(format!("reading {path}: {e}")))?;
    if !has_line_table(&bytes) {
        return Err(PyRuntimeError::new_err(format!(
            "{path} has no .debug_line section — build the program with \
             `[profile.release] debug = true` and pass the unstripped ELF from \
             target/sbpf-solana-solana/release/, not target/deploy/"
        )));
    }
    register_debug_elf(address, bytes);
    Ok(())
}

/// `{"files": {path: {line: count}}, "programs": [...]}`.
#[pyfunction]
#[pyo3(signature = (roots = vec![]))]
fn coverage_report(py: Python<'_>, roots: Vec<(String, String)>) -> PyResult<Py<PyAny>> {
    let (lines, programs) = build_report(&roots);

    let files = pyo3::types::PyDict::new(py);
    for (file, file_lines) in &lines {
        let d = pyo3::types::PyDict::new(py);
        for (line, count) in file_lines {
            d.set_item(line, count)?;
        }
        files.set_item(file, d)?;
    }

    let progs = pyo3::types::PyList::empty(py);
    for p in &programs {
        let d = pyo3::types::PyDict::new(py);
        d.set_item("program_id", &p.program_id)?;
        d.set_item("instructions_covered", p.instructions_covered)?;
        d.set_item("instructions_total", p.instructions_total)?;
        d.set_item("has_debug_info", p.has_debug_info)?;
        d.set_item("lines_resolved", p.lines_resolved)?;
        d.set_item("text_mismatch", p.text_mismatch)?;
        d.set_item("text_sha256", &p.text_sha256)?;
        progs.append(d)?;
    }

    let out = pyo3::types::PyDict::new(py);
    out.set_item("files", files)?;
    out.set_item("programs", progs)?;
    Ok(out.into_any().unbind())
}

/// LCOV text, as `genhtml`, codecov and the editor gutters consume it.
fn to_lcov(lines: &LineCounts) -> String {
    let mut out = String::new();
    for (file, file_lines) in lines {
        out.push_str("TN:\n");
        out.push_str(&format!("SF:{file}\n"));
        for (line, count) in file_lines {
            out.push_str(&format!("DA:{line},{count}\n"));
        }
        out.push_str(&format!("LF:{}\n", file_lines.len()));
        out.push_str(&format!(
            "LH:{}\n",
            file_lines.values().filter(|c| **c > 0).count()
        ));
        out.push_str("end_of_record\n");
    }
    out
}

/// The build-identity sidecar: which build each program's line numbers describe.
fn to_meta_json(programs: &[ProgramReport]) -> String {
    let mut out = String::from("{\n  \"programs\": {\n");
    for (i, p) in programs.iter().enumerate() {
        if i > 0 {
            out.push_str(",\n");
        }
        out.push_str(&format!(
            "    \"{}\": {{\n      \"text_sha256\": \"{}\",\n      \"instructions_total\": {}\n    }}",
            p.program_id, p.text_sha256, p.instructions_total
        ));
    }
    out.push_str("\n  }\n}\n");
    out
}

/// Collect, format and write the report and its sidecar — in one call.
///
/// Deliberately one call rather than a Python loop over `report()`: a live
/// export runs on a background thread while the main thread is inside
/// back-to-back transactions, and Python-level work yields the GIL between every
/// step. Doing this from Python stretched a 4ms export to **1192ms** under that
/// contention, which is not live in any useful sense. Holding the GIL once, for
/// the whole export, keeps it at its true cost and bounds the main thread's
/// stall to the same figure.
#[pyfunction]
#[pyo3(signature = (path, roots = vec![]))]
fn coverage_lcov(path: &str, roots: Vec<(String, String)>) -> PyResult<usize> {
    let (lines, programs) = build_report(&roots);
    let n = lines.len();
    write_atomic(path, &to_lcov(&lines))
        .map_err(|e| PyRuntimeError::new_err(format!("writing {path}: {e}")))?;
    // Best effort: a missing sidecar only costs the cross-build merge check,
    // and must not fail the export that already succeeded.
    let _ = write_atomic(&format!("{path}.meta.json"), &to_meta_json(&programs));
    Ok(n)
}

/// Write to a temporary beside the target, then rename it into place.
///
/// The report is rewritten every few seconds while an editor extension watches
/// it and re-reads on every change. A plain write truncates first, so a reader
/// that looks during the window sees half a file and renders nonsense coverage —
/// and a Ctrl+C landing there would leave that half-file on disk as the final
/// report. Rename within a directory is atomic, so a reader sees either the
/// previous report or the complete new one, never a partial one.
fn write_atomic(path: &str, contents: &str) -> std::io::Result<()> {
    // Pid-qualified so two processes writing the same path cannot collide on
    // the temporary and hand each other a truncated file.
    let tmp = format!("{path}.{}.tmp", std::process::id());
    std::fs::write(&tmp, contents)?;
    if let Err(e) = std::fs::rename(&tmp, path) {
        let _ = std::fs::remove_file(&tmp);
        return Err(e);
    }
    Ok(())
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(coverage_enable, m)?)?;
    m.add_function(wrap_pyfunction!(coverage_disable, m)?)?;
    m.add_function(wrap_pyfunction!(coverage_enabled, m)?)?;
    m.add_function(wrap_pyfunction!(coverage_reset, m)?)?;
    m.add_function(wrap_pyfunction!(coverage_add_debug_elf, m)?)?;
    m.add_function(wrap_pyfunction!(coverage_report, m)?)?;
    m.add_function(wrap_pyfunction!(coverage_lcov, m)?)?;
    Ok(())
}
