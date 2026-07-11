//! A minimal native Solana program exercising **return data**: `add(a, b) -> u64`.
//!
//! Instruction data is an 8-byte (Anchor-style) discriminator prefix followed by
//! two little-endian `u64`s. The program ignores the discriminator, reads the two
//! args, and writes their sum to the transaction return data via
//! `set_return_data` — the value the harness decodes into `result.return_value`.

use solana_program::{
    account_info::AccountInfo, entrypoint, entrypoint::ProgramResult,
    program::set_return_data, program_error::ProgramError, pubkey::Pubkey,
};

entrypoint!(process_instruction);

pub fn process_instruction(
    _program_id: &Pubkey,
    _accounts: &[AccountInfo],
    data: &[u8],
) -> ProgramResult {
    // data = 8-byte discriminator + u64 a (LE) + u64 b (LE)
    if data.len() < 24 {
        return Err(ProgramError::InvalidInstructionData);
    }
    let a = u64::from_le_bytes(data[8..16].try_into().unwrap());
    let b = u64::from_le_bytes(data[16..24].try_into().unwrap());
    set_return_data(&a.wrapping_add(b).to_le_bytes());
    Ok(())
}
