"""Address Lookup Table support: the god-mode cheatcode + the official builders."""
from wake_sol import Account, LiteSVM, Pubkey

ALT_PROGRAM = Pubkey("AddressLookupTab1e1111111111111111111111111")

A = Pubkey(bytes([1] * 32))
B = Pubkey(bytes([2] * 32))
C = Pubkey(bytes([3] * 32))


def test_cheat_create_lookup_table_makes_a_valid_table_account():
    svm = LiteSVM()
    table = svm.create_lookup_table([A, B, C])
    acc = Account(table, svm)
    assert acc.exists
    assert acc.owner == ALT_PROGRAM
    # data = 56-byte meta + 32 bytes per stored address
    assert len(acc.data) == 56 + 3 * 32
    # ProgramState::LookupTable bincode discriminant (u32 LE = 1)
    assert acc.data[0:4] == b"\x01\x00\x00\x00"


def test_cheat_create_lookup_table_honors_explicit_address():
    svm = LiteSVM()
    fixed = Pubkey(bytes([7] * 32))
    table = svm.create_lookup_table([A], address=fixed)
    assert table == fixed
    assert Account(fixed, svm).exists


def test_official_builders_produce_alt_program_instructions():
    svm = LiteSVM()
    payer = Account.new(svm)

    create_ix, table = svm.address_lookup_table.create(payer, payer, recent_slot=0)
    assert create_ix.program_id == ALT_PROGRAM
    assert isinstance(table, Pubkey)
    # CreateLookupTable accounts: [table(w), authority(s), payer(w,s), system_program]
    assert len(create_ix.accounts) == 4

    extend_ix = svm.address_lookup_table.extend(table, payer, [A, B], payer=payer)
    assert extend_ix.program_id == ALT_PROGRAM
    assert len(extend_ix.data) > 4          # discriminant + Vec<Pubkey> of new addresses

    for ix in (
        svm.address_lookup_table.deactivate(table, payer),
        svm.address_lookup_table.close(table, payer, payer),
        svm.address_lookup_table.freeze(table, payer),
    ):
        assert ix.program_id == ALT_PROGRAM
