//! A minimal native Solana program: increments a `u64` counter stored in the
//! first (writable, program-owned) account.

use solana_program::{
    account_info::{next_account_info, AccountInfo},
    entrypoint,
    entrypoint::ProgramResult,
    msg,
    program_error::ProgramError,
    pubkey::Pubkey,
};

entrypoint!(process_instruction);

pub fn process_instruction(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    _instruction_data: &[u8],
) -> ProgramResult {
    let account_iter = &mut accounts.iter();
    let counter = next_account_info(account_iter)?;

    if counter.owner != program_id {
        return Err(ProgramError::IncorrectProgramId);
    }

    let mut data = counter.try_borrow_mut_data()?;
    if data.len() < 8 {
        return Err(ProgramError::AccountDataTooSmall);
    }

    let value = u64::from_le_bytes(data[..8].try_into().unwrap());
    let next = value.wrapping_add(1);
    data[..8].copy_from_slice(&next.to_le_bytes());

    msg!("counter incremented to {}", next);
    Ok(())
}
