//! A native program exercising **events**: `ping(x)` emits the same
//! `{value, doubled}` payload two ways —
//!   * `emit!`     → `sol_log_data` → a `Program data:` log line, and
//!   * `emit_cpi!` → a self-CPI whose data is `[event tag][disc][Borsh]`
//!     (Anchor's mechanism; the harness hoists it to an event on this node).
//!
//! Discriminators are fixed test values (not real Anchor hashes); the matching
//! IDL in `tests/test_events_e2e.py` uses the same bytes.

use solana_program::{
    account_info::AccountInfo, entrypoint, entrypoint::ProgramResult,
    instruction::{AccountMeta, Instruction}, log::sol_log_data, program::invoke,
    program_error::ProgramError, pubkey::Pubkey,
};

entrypoint!(process_instruction);

/// Anchor `EVENT_IX_TAG` (little-endian) — the emit_cpi! self-CPI prefix.
const EVENT_TAG: [u8; 8] = [0xe4, 0x45, 0xa5, 0x2e, 0x51, 0xcb, 0x9a, 0x1d];
const LOGGED_DISC: [u8; 8] = [1, 1, 1, 1, 1, 1, 1, 1]; // emit! event `Logged`
const CPIED_DISC: [u8; 8] = [2, 2, 2, 2, 2, 2, 2, 2]; //  emit_cpi! event `Cpied`

pub fn process_instruction(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    data: &[u8],
) -> ProgramResult {
    // The emit_cpi! self-invoke re-enters here with the event tag — no-op.
    if data.len() >= 8 && data[..8] == EVENT_TAG {
        return Ok(());
    }
    // ping(x): data = 8-byte discriminator + u64 x (LE)
    if data.len() < 16 {
        return Err(ProgramError::InvalidInstructionData);
    }
    let x = u64::from_le_bytes(data[8..16].try_into().unwrap());
    let mut body = x.to_le_bytes().to_vec(); // Borsh { value: u64
    body.extend_from_slice(&x.wrapping_mul(2).to_le_bytes()); //      doubled: u64 }

    // emit! → Program data: <base64(LOGGED_DISC ‖ body)>
    let mut logged = LOGGED_DISC.to_vec();
    logged.extend_from_slice(&body);
    sol_log_data(&[logged.as_slice()]);

    // emit_cpi! → self-CPI with [EVENT_TAG ‖ CPIED_DISC ‖ body]. accounts[0] is
    // the (dummy) event authority; accounts[1] is this program's own account,
    // which must be in the CPI's account_infos. Direct self-recursion is allowed.
    let ev_auth = &accounts[0];
    let self_program = &accounts[1];
    let mut cpi_data = EVENT_TAG.to_vec();
    cpi_data.extend_from_slice(&CPIED_DISC);
    cpi_data.extend_from_slice(&body);
    let ix = Instruction {
        program_id: *program_id,
        accounts: vec![AccountMeta::new_readonly(*ev_auth.key, false)],
        data: cpi_data,
    };
    invoke(&ix, &[ev_auth.clone(), self_program.clone()])?;
    Ok(())
}
