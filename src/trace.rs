//! Structured call-trace extraction from a transaction's execution.
//!
//! litesvm hands back `inner_instructions` — the flat, stack-height-tagged list
//! of CPIs per top-level instruction — but referenced by index into the
//! transaction's account keys. Here we resolve those indices against the
//! message (to real pubkeys + signer/writable flags) and rebuild the nested
//! call tree that the Python layer renders.

use base64::Engine as _;
use pyo3::exceptions::PyIndexError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList};

use solana_address::Address;
use solana_message::compiled_instruction::CompiledInstruction;
use solana_message::inner_instruction::{InnerInstruction, InnerInstructionsList};
use solana_message::VersionedMessage;

use crate::instruction::PyAccountMeta;
use crate::PyPubkey;

/// Anchor's `emit_cpi!` prefixes the self-CPI instruction data with this fixed
/// 8-byte tag (Anchor `EVENT_IX_TAG`, little-endian), followed by the event's own
/// 8-byte discriminator and the Borsh body. We use it to recognize an `emit_cpi!`
/// invocation and hoist it to an event on the emitting node.
///
/// NOTE: this constant is Anchor's published value; verify against a real Anchor
/// build if events silently fail to appear (a wrong tag degrades gracefully — the
/// self-CPI just renders as an unknown instruction instead of an event).
const ANCHOR_EVENT_CPI_TAG: [u8; 8] = [0xe4, 0x45, 0xa5, 0x2e, 0x51, 0xcb, 0x9a, 0x1d];

/// Outcome of one invocation, recovered best-effort from the log stream's
/// `success`/`failed:` brackets. `Unknown` = the frame never closed in the logs
/// (truncated stream, or an instruction that never ran after an earlier failure).
#[derive(Clone, Copy, PartialEq)]
pub(crate) enum NodeStatus {
    Success,
    Failed,
    Unknown,
}

impl NodeStatus {
    fn as_str(self) -> &'static str {
        match self {
            NodeStatus::Success => "success",
            NodeStatus::Failed => "failed",
            NodeStatus::Unknown => "unknown",
        }
    }
}

/// An owned node in the call tree: one (possibly nested) program invocation.
#[derive(Clone)]
pub(crate) struct Traced {
    program_id: Address,
    /// `(pubkey, is_signer, is_writable)` for each referenced account, in order.
    accounts: Vec<(Address, bool, bool)>,
    data: Vec<u8>,
    /// Invocation depth: 1 for a top-level instruction, ≥2 for a CPI.
    stack_height: u32,
    /// Raw program-log lines emitted **directly** by this invocation (its own
    /// frame), in order — not its children's, and not the runtime's structural
    /// `invoke`/`consumed`/`success`/`failed`/`return`/`data` markers. Text only.
    logs: Vec<String>,
    /// Best-effort per-node enrichment recovered from the log stream (§10):
    /// cumulative compute units (incl. this frame's CPIs), status, the raw
    /// `failed: <msg>` text, and the frame's return-data bytes. `None`/`Unknown`
    /// when the relevant marker wasn't present (e.g. truncated logs).
    compute_units: Option<u64>,
    status: NodeStatus,
    error_msg: Option<String>,
    return_data: Option<Vec<u8>>,
    /// Raw event payloads (each `disc ‖ Borsh`) this invocation emitted — from
    /// `Program data:` logs (`emit!`) and hoisted `emit_cpi!` self-CPIs. Decoded
    /// per the program's event table on the Python side.
    event_data: Vec<Vec<u8>>,
    children: Vec<Traced>,
}

/// Resolve one compiled instruction's indices into pubkeys + privilege flags.
///
/// Indices outside the static account keys (only reachable via address-lookup
/// tables, which this harness does not yet build) resolve to the default
/// address with no privileges rather than panicking.
fn resolve(
    ci: &CompiledInstruction,
    keys: &[Address],
    message: &VersionedMessage,
    stack_height: u32,
) -> Traced {
    let program_id = keys
        .get(ci.program_id_index as usize)
        .copied()
        .unwrap_or_default();
    let accounts = ci
        .accounts
        .iter()
        .map(|&idx| {
            let i = idx as usize;
            match keys.get(i).copied() {
                Some(pubkey) => (pubkey, message.is_signer(i), message.is_maybe_writable(i, None)),
                None => (Address::default(), false, false),
            }
        })
        .collect();
    Traced {
        program_id,
        accounts,
        data: ci.data.clone(),
        stack_height,
        logs: Vec::new(),
        compute_units: None,
        status: NodeStatus::Unknown,
        error_msg: None,
        return_data: None,
        event_data: Vec::new(),
        children: Vec::new(),
    }
}

// --------------------------------------------------------------------------- //
// per-node log attribution (text only)
// --------------------------------------------------------------------------- //

/// Classify a runtime frame-marker line as `(program_id, verb)`.
///
/// Markers have the shape `Program <pid> <verb> …` where the verb is the THIRD
/// whitespace token (the pid is the second): `invoke` / `consumed` / `success`
/// / `failed:`. This is anchored to that position on purpose — a program's own
/// `Program log: …` output is never a marker (its second token is `log:`), and
/// crucially a message whose *text* contains "failed:" (e.g. a Rust
/// `panicked at 'assertion failed: …'`) is NOT mistaken for a frame close.
fn marker_verb(l: &str) -> Option<(&str, &str)> {
    if !l.starts_with("Program ") {
        return None;
    }
    let mut it = l.split_whitespace();
    let _program = it.next()?; // "Program"
    let pid = it.next()?; // program id, or "log:" / "data:" / "return:"
    match it.next().unwrap_or("") {
        verb @ ("invoke" | "consumed" | "success" | "failed:") => Some((pid, verb)),
        _ => None,
    }
}

/// Attribute each raw log line to the call-tree node that emitted it.
///
/// The runtime's log stream is a pre-order traversal of the same tree
/// `build_call_tree` reconstructs: `invoke [depth]` opens a frame,
/// `success`/`failed:` closes it. We pair the *k*-th `invoke` with the *k*-th
/// pre-order node positionally (program id can repeat across CPI depths, so a
/// positional pairing is the only sound one) and drop every non-marker line
/// into the node whose frame is currently open.
///
/// This is deliberately *text only* — no event/error/return-data decoding (§10).
/// On any divergence from the structured tree (program-id mismatch, or more
/// `invoke`s than nodes — e.g. truncated logs) it stops attributing rather than
/// misattribute; nodes past that point simply carry no logs.
fn attach_logs(tree: &mut [Traced], logs: &[String]) {
    let mut pids: Vec<String> = Vec::new();
    fn collect(nodes: &[Traced], out: &mut Vec<String>) {
        for n in nodes {
            out.push(n.program_id.to_string());
            collect(&n.children, out);
        }
    }
    collect(tree, &mut pids);
    if pids.is_empty() {
        return;
    }

    let n = pids.len();
    let mut buckets: Vec<Vec<String>> = vec![Vec::new(); n];
    let mut cu: Vec<Option<u64>> = vec![None; n];
    let mut status: Vec<NodeStatus> = vec![NodeStatus::Unknown; n];
    let mut err: Vec<Option<String>> = vec![None; n];
    let mut ret: Vec<Option<Vec<u8>>> = vec![None; n];
    let mut events: Vec<Vec<Vec<u8>>> = vec![Vec::new(); n];
    let mut stack: Vec<usize> = Vec::new();
    let mut next = 0usize; // pre-order index of the next node an `invoke` opens
    let mut aligned = true;

    for raw in logs {
        let l = raw.trim();
        if l.is_empty() {
            continue;
        }
        // Return data: `Program return: <pid> <base64>` — attach the bytes to the
        // frame currently open (the returning program), decoded later per its
        // `returns` type. Handled before `marker_verb` (its 3rd token is the pid).
        if let Some(rest) = l.strip_prefix("Program return: ") {
            if aligned {
                if let (Some(&idx), Some((_pid, b64))) = (stack.last(), rest.split_once(' ')) {
                    if let Ok(bytes) = base64::engine::general_purpose::STANDARD.decode(b64.trim()) {
                        ret[idx] = Some(bytes);
                    }
                }
            }
            continue;
        }
        // Event payloads: `Program data: <base64>` (`emit!`, via `sol_log_data`).
        // Capture the first base64 chunk (Anchor emits one = `disc ‖ Borsh`) as an
        // event on the open node; decoded per the program's event table in Python.
        if let Some(rest) = l.strip_prefix("Program data: ") {
            if aligned {
                if let Some(&idx) = stack.last() {
                    let chunk = rest.split_whitespace().next().unwrap_or("");
                    if let Ok(bytes) = base64::engine::general_purpose::STANDARD.decode(chunk) {
                        events[idx].push(bytes);
                    }
                }
            }
            continue;
        }
        match marker_verb(l) {
            Some((pid, "invoke")) => {
                if !aligned {
                    continue;
                }
                if next >= pids.len() || pids[next].as_str() != pid {
                    aligned = false; // diverged from the structured tree — stop here
                    continue;
                }
                stack.push(next);
                next += 1;
            }
            Some((_, "consumed")) => {
                // `Program <pid> consumed <N> of <M> compute units` — N is
                // cumulative for this frame incl. its CPIs; logged while open.
                if aligned {
                    if let Some(&idx) = stack.last() {
                        cu[idx] = parse_consumed(l);
                    }
                }
            }
            Some((_, "success")) => {
                if aligned {
                    if let Some(&idx) = stack.last() {
                        status[idx] = NodeStatus::Success;
                    }
                    stack.pop();
                }
            }
            Some((_, "failed:")) => {
                if aligned {
                    if let Some(&idx) = stack.last() {
                        status[idx] = NodeStatus::Failed;
                        err[idx] = l.split_once("failed: ").map(|(_, m)| m.trim().to_string());
                    }
                    stack.pop();
                }
            }
            Some(_) => {}
            None => {
                // A program message line; attach to the open frame's node.
                if let Some(&idx) = stack.last() {
                    let msg = l.strip_prefix("Program log: ").unwrap_or(l);
                    buckets[idx].push(msg.to_string());
                }
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn distribute(
        nodes: &mut [Traced],
        buckets: &mut [Vec<String>],
        cu: &[Option<u64>],
        status: &[NodeStatus],
        err: &mut [Option<String>],
        ret: &mut [Option<Vec<u8>>],
        events: &mut [Vec<Vec<u8>>],
        i: &mut usize,
    ) {
        for node in nodes.iter_mut() {
            let k = *i;
            node.logs = std::mem::take(&mut buckets[k]);
            node.compute_units = cu[k];
            node.status = status[k];
            node.error_msg = err[k].take();
            node.return_data = ret[k].take();
            node.event_data = std::mem::take(&mut events[k]);
            *i += 1;
            distribute(&mut node.children, buckets, cu, status, err, ret, events, i);
        }
    }
    let mut i = 0usize;
    distribute(tree, &mut buckets, &cu, &status, &mut err, &mut ret, &mut events, &mut i);
}

/// `Program <pid> consumed <N> of <M> compute units` -> `N`.
fn parse_consumed(l: &str) -> Option<u64> {
    let mut it = l.split_whitespace();
    while let Some(tok) = it.next() {
        if tok == "consumed" {
            return it.next().and_then(|n| n.parse().ok());
        }
    }
    None
}

/// Fold a flat, stack-height-tagged CPI list into `root`'s children, rebuilding
/// the nesting. Inner entries start at stack height 2 and increase by at most
/// one per step, so a stack of open nodes indexed by depth reconstructs the
/// tree in a single pass.
fn nest(
    root: Traced,
    inner: &[InnerInstruction],
    keys: &[Address],
    message: &VersionedMessage,
) -> Traced {
    // `stack[d - 1]` is the currently-open node at depth `d`; the root is depth 1.
    let mut stack: Vec<Traced> = vec![root];
    for entry in inner {
        let depth = entry.stack_height as usize;
        let node = resolve(&entry.instruction, keys, message, entry.stack_height as u32);
        // Close every open node at this depth or deeper, attaching each to its
        // parent, until the new node's parent is on top of the stack.
        while stack.len() >= depth && stack.len() > 1 {
            let finished = stack.pop().unwrap();
            stack.last_mut().unwrap().children.push(finished);
        }
        stack.push(node);
    }
    // Collapse the remaining open path back into the root.
    while stack.len() > 1 {
        let finished = stack.pop().unwrap();
        stack.last_mut().unwrap().children.push(finished);
    }
    stack.pop().unwrap()
}

/// Build the per-top-level-instruction call tree for an executed transaction,
/// with each node's directly-emitted log lines attached (`logs`).
pub(crate) fn build_call_tree(
    message: &VersionedMessage,
    inner_instructions: &InnerInstructionsList,
    logs: &[String],
) -> Vec<Traced> {
    let keys = message.static_account_keys();
    let mut tree: Vec<Traced> = message
        .instructions()
        .iter()
        .enumerate()
        .map(|(i, ci)| {
            let root = resolve(ci, keys, message, 1);
            match inner_instructions.get(i) {
                Some(inner) if !inner.is_empty() => nest(root, inner, keys, message),
                _ => root,
            }
        })
        .collect();
    attach_logs(&mut tree, logs);
    hoist_cpi_events(&mut tree);
    tree
}

/// Fold `emit_cpi!` self-CPIs into events on their emitting node (Option A): a
/// child that self-CPIs with the fixed Anchor event tag isn't a real call — its
/// payload (`disc ‖ Borsh`, after the tag) becomes an event on the parent, and
/// the child node is dropped, so `emit!` and `emit_cpi!` render identically.
fn hoist_cpi_events(nodes: &mut [Traced]) {
    for node in nodes.iter_mut() {
        let mut kept = Vec::with_capacity(node.children.len());
        for child in std::mem::take(&mut node.children) {
            if child.program_id == node.program_id
                && child.data.len() >= 16
                && child.data[..8] == ANCHOR_EVENT_CPI_TAG
            {
                node.event_data.push(child.data[8..].to_vec()); // strip the tag
            } else {
                kept.push(child);
            }
        }
        node.children = kept;
        hoist_cpi_events(&mut node.children);
    }
}

/// Decode a node's raw event payloads via the Python event registry (scoped by
/// program id) — returns a Python list of decoded events / `UnknownEvent`s.
fn decode_events_py<'py>(
    py: Python<'py>,
    program_id: &Address,
    payloads: &[Vec<u8>],
) -> PyResult<Bound<'py, PyAny>> {
    let raws: Vec<Bound<'py, PyBytes>> =
        payloads.iter().map(|d| PyBytes::new(py, d)).collect();
    py.import("solana_fuzzer._interface")?
        .getattr("decode_events")?
        .call1((program_id.to_string(), raws))
}

/// Walk the tree pre-order, decoding each node's events and appending them to
/// `out` — the flat `result.events` roll-up.
pub(crate) fn collect_events(
    py: Python<'_>,
    nodes: &[Traced],
    out: &Bound<'_, PyList>,
) -> PyResult<()> {
    for node in nodes {
        if !node.event_data.is_empty() {
            let decoded = decode_events_py(py, &node.program_id, &node.event_data)?;
            for ev in decoded.try_iter()? {
                out.append(ev?)?;
            }
        }
        collect_events(py, &node.children, out)?;
    }
    Ok(())
}

/// Find the instruction data of the invocation that most likely produced the
/// transaction's (tx-wide, last-writer-wins) return data: the **last**
/// invocation of `program_id` in execution order (pre-order DFS = execution
/// order, incl. CPIs). Its data lets the Python side match the instruction and
/// pick the right IDL `returns` type. `None` if the program never appears (e.g.
/// return data set by a program not present as an instruction in this tree).
pub(crate) fn return_candidate(tree: &[Traced], program_id: &Address) -> Option<Vec<u8>> {
    fn walk(nodes: &[Traced], pid: &Address, found: &mut Option<Vec<u8>>) {
        for n in nodes {
            if &n.program_id == pid {
                *found = Some(n.data.clone()); // later match wins => last in execution order
            }
            walk(&n.children, pid, found);
        }
    }
    let mut found = None;
    walk(tree, program_id, &mut found);
    found
}

/// Parse a runtime `failed:` message of the form `custom program error: 0x<hex>`
/// into its `Custom(code)` value. `None` for any other failure text (a native
/// `InstructionError` variant rendered as prose, a builtin's message, truncation).
fn parse_custom_code(msg: &str) -> Option<u32> {
    let hex = msg.trim().strip_prefix("custom program error: 0x")?;
    let hex = hex.split_whitespace().next().unwrap_or(hex);
    u32::from_str_radix(hex, 16).ok()
}

/// The program that originated a `Custom(code)` transaction failure, for error
/// attribution. Only the bare code bubbles up in the `TransactionError`, so which
/// program (System, SPL Token, a user program…) produced it is recovered here from
/// the call tree.
///
/// A failed CPI propagates its code upward unchanged, so every frame on the
/// failing path logs the same `custom program error: 0x<code>`; the **innermost**
/// such frame is the origin. A frame that *remapped* the callee's error to a
/// different code logs that different code and is correctly skipped — attribution
/// lands on the frame that actually produced `code`. Falls back to the innermost
/// failed frame when no message parses a matching code (e.g. a builtin whose
/// failure the runtime rendered as prose), then `None` (no failed frame — a
/// tx-level error, or an empty/absent trace).
pub(crate) fn failing_program(tree: &[Traced], code: u32) -> Option<Address> {
    // Pre-order DFS = execution order; descending overwrites keep the deepest
    // (innermost) hit on the failing chain, later siblings winning ties.
    fn walk(
        nodes: &[Traced],
        code: u32,
        matched: &mut Option<Address>,
        failed: &mut Option<Address>,
    ) {
        for n in nodes {
            if n.status == NodeStatus::Failed {
                *failed = Some(n.program_id);
                if n.error_msg.as_deref().and_then(parse_custom_code) == Some(code) {
                    *matched = Some(n.program_id);
                }
            }
            walk(&n.children, code, matched, failed);
        }
    }
    let (mut matched, mut failed) = (None, None);
    walk(tree, code, &mut matched, &mut failed);
    matched.or(failed)
}

/// One node of a transaction's call tree: a program invocation with its
/// resolved accounts, data payload, depth, and nested CPIs.
#[pyclass(name = "TracedInstruction", module = "solana_fuzzer._native", frozen)]
pub struct PyTracedInstruction {
    node: Traced,
}

impl PyTracedInstruction {
    pub(crate) fn from_node(node: Traced) -> Self {
        Self { node }
    }
}

#[pymethods]
impl PyTracedInstruction {
    /// The program this instruction invokes.
    #[getter]
    fn program_id(&self) -> PyPubkey {
        PyPubkey { inner: self.node.program_id }
    }

    /// The account slots, in instruction order, as `AccountMeta`s (pubkey plus
    /// the signer/writable privileges this instruction saw).
    #[getter]
    fn accounts(&self) -> Vec<PyAccountMeta> {
        self.node
            .accounts
            .iter()
            .map(|&(pubkey, is_signer, is_writable)| PyAccountMeta {
                pubkey: PyPubkey { inner: pubkey },
                is_signer,
                is_writable,
            })
            .collect()
    }

    /// The opaque instruction data payload.
    #[getter]
    fn data<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.node.data)
    }

    /// Invocation depth: 1 for a top-level instruction, ≥2 for a CPI.
    #[getter]
    fn stack_height(&self) -> u32 {
        self.node.stack_height
    }

    /// Raw program-log lines emitted directly by this invocation, in order
    /// (its own frame only — children have their own; structural
    /// `invoke`/`consumed`/`success`/`failed` markers excluded). Text only.
    #[getter]
    fn logs(&self) -> Vec<String> {
        self.node.logs.clone()
    }

    /// This invocation's outcome, best-effort from the logs: `"success"`,
    /// `"failed"`, or `"unknown"` (frame never closed — e.g. truncated logs).
    #[getter]
    fn status(&self) -> &'static str {
        self.node.status.as_str()
    }

    /// Cumulative compute units for this frame (incl. its CPIs), or `None` if the
    /// `consumed` marker wasn't in the (possibly truncated) log stream.
    #[getter]
    fn compute_units(&self) -> Option<u64> {
        self.node.compute_units
    }

    /// The raw `failed: <msg>` runtime text if this frame failed, else `None`.
    #[getter]
    fn error(&self) -> Option<String> {
        self.node.error_msg.clone()
    }

    /// This frame's return-data bytes (`set_return_data`), or `None`.
    #[getter]
    fn raw_return_value<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        self.node.return_data.as_ref().map(|d| PyBytes::new(py, d))
    }

    /// Raw event payloads (each `disc ‖ Borsh`) emitted directly by this frame
    /// (`emit!` + hoisted `emit_cpi!`), in order.
    #[getter]
    fn events_raw<'py>(&self, py: Python<'py>) -> Vec<Bound<'py, PyBytes>> {
        self.node.event_data.iter().map(|d| PyBytes::new(py, d)).collect()
    }

    /// This frame's **decoded** events (typed per the program's event table;
    /// `UnknownEvent` for an unregistered program/discriminator).
    #[getter]
    fn events<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        decode_events_py(py, &self.node.program_id, &self.node.event_data)
    }

    /// The CPIs this instruction made, in order.
    #[getter]
    fn inner(&self) -> Vec<PyTracedInstruction> {
        self.node
            .children
            .iter()
            .cloned()
            .map(PyTracedInstruction::from_node)
            .collect()
    }

    fn __repr__(&self) -> String {
        format!(
            "TracedInstruction(program_id={}, accounts={}, data={} bytes, depth={}, inner={})",
            self.node.program_id,
            self.node.accounts.len(),
            self.node.data.len(),
            self.node.stack_height,
            self.node.children.len(),
        )
    }
}

/// A transaction's call trace: the list of top-level instructions (each with
/// its CPIs) plus the transaction outcome. It is both a sequence over the
/// top-level `TracedInstruction`s and a `rich`-renderable (its `__rich__` /
/// `__str__` delegate to the Python renderer in `solana_fuzzer.call_trace`).
#[pyclass(name = "CallTrace", module = "solana_fuzzer._native", frozen)]
pub struct PyCallTrace {
    nodes: Vec<Traced>,
    success: bool,
    error: Option<String>,
    compute_units: u64,
}

impl PyCallTrace {
    pub(crate) fn new(
        nodes: Vec<Traced>,
        success: bool,
        error: Option<String>,
        compute_units: u64,
    ) -> Self {
        Self { nodes, success, error, compute_units }
    }
}

#[pymethods]
impl PyCallTrace {
    /// Whether the transaction executed without error.
    #[getter]
    fn success(&self) -> bool {
        self.success
    }

    /// The failure description, or `None` on success.
    #[getter]
    fn error(&self) -> Option<String> {
        self.error.clone()
    }

    /// Total compute units the transaction consumed.
    #[getter]
    fn compute_units_consumed(&self) -> u64 {
        self.compute_units
    }

    /// The top-level instructions, in order.
    #[getter]
    fn instructions(&self) -> Vec<PyTracedInstruction> {
        self.nodes
            .iter()
            .cloned()
            .map(PyTracedInstruction::from_node)
            .collect()
    }

    fn __len__(&self) -> usize {
        self.nodes.len()
    }

    fn __getitem__(&self, index: isize) -> PyResult<PyTracedInstruction> {
        let len = self.nodes.len() as isize;
        let i = if index < 0 { index + len } else { index };
        if i < 0 || i >= len {
            return Err(PyIndexError::new_err("call trace index out of range"));
        }
        Ok(PyTracedInstruction::from_node(self.nodes[i as usize].clone()))
    }

    /// Delegate rich rendering to the Python renderer.
    fn __rich__(slf: Py<Self>, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let module = py.import("solana_fuzzer.call_trace")?;
        Ok(module.call_method1("_rich", (slf,))?.unbind())
    }

    fn __str__(slf: Py<Self>, py: Python<'_>) -> PyResult<String> {
        let module = py.import("solana_fuzzer.call_trace")?;
        module.call_method1("_to_str", (slf,))?.extract()
    }

    fn __repr__(&self) -> String {
        format!(
            "CallTrace({} top-level instruction(s), {})",
            self.nodes.len(),
            if self.success { "success" } else { "failed" },
        )
    }
}
