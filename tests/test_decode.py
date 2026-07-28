from wake_sol import *
from wake_sol import decode_instruction

TOKEN = Pubkey("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
MINT_LEN = 82


def _decode(node):
    return decode_instruction(node.program_id, node.data, len(node.accounts))


def test_decode_system_transfer():
    alice = Account.new()
    bob = Account.new()
    svm.airdrop(alice, 1_000_000_000)

    amount = svm.minimum_balance_for_rent_exemption(0)
    res = alice.tx(svm.system.transfer(amount, from_=alice, to=bob))
    assert res.success, res.error

    dec = _decode(res.call_trace[0])
    assert dec.program_name == "System Program"
    assert dec.name == "transfer"
    assert dec.args == {"lamports": amount}
    assert dec.account_names == ["from", "to"]


def test_decode_mint_setup_args():
    payer = Account.new()
    svm.airdrop(payer, 5_000_000_000)
    mint = Account.new()
    rent = svm.minimum_balance_for_rent_exemption(MINT_LEN)

    res = payer.tx(
        svm.system.create_account(rent, MINT_LEN, TOKEN, from_=payer, to=mint),
        svm.token.initialize_mint2(6, payer, mint=mint),
        signers=[mint],
    )
    assert res.success, res.error

    ca = _decode(res.call_trace[0])
    assert ca.name == "create_account"
    assert ca.args["space"] == MINT_LEN
    assert ca.args["owner"] == TOKEN

    im = _decode(res.call_trace[1])
    assert im.program_name == "Token Program"
    assert im.name == "initialize_mint2"
    assert im.args["decimals"] == 6
    assert im.args["mint_authority"] == payer.pubkey
    assert im.args["freeze_authority"] is None
    assert im.account_names[0] == "mint"


def test_decode_ata_creation_cpis():
    payer = Account.new()
    svm.airdrop(payer, 5_000_000_000)
    mint = Account.new()
    rent = svm.minimum_balance_for_rent_exemption(MINT_LEN)
    payer.tx(
        svm.system.create_account(rent, MINT_LEN, TOKEN, from_=payer, to=mint),
        svm.token.initialize_mint2(6, payer, mint=mint),
        signers=[mint],
    )

    owner = Account.new()
    res = payer.tx(svm.token.create_ata(payer, owner, mint))
    assert res.success, res.error

    top = res.call_trace[0]
    dtop = _decode(top)
    assert dtop.program_name == "Associated Token Account Program"
    assert dtop.name == "create"

    # The ATA program's CPIs decode by program too.
    inner_names = {_decode(c).name for c in top.inner}
    assert "create_account" in inner_names  # System CPI
    assert "initialize_account3" in inner_names  # Token CPI
    assert "initialize_immutable_owner" in inner_names


def test_unknown_program_falls_back():
    # A random program id is not registered: name-only, raw account slots.
    dec = decode_instruction(Pubkey(99), b"\x01\x02\x03", 2)
    assert dec.name is None
    assert dec.program_name is None
    assert dec.account_names == [None, None]


def test_labels():
    # .label is the raw assigned value (None if unset); str() resolves a display.
    sys_acc = Account(Pubkey(0))
    assert sys_acc.label is None
    assert str(sys_acc) == "System Program"  # well-known resolution
    assert str(Account(TOKEN)) == "Token Program"

    alice = Account.new()
    assert alice.label is None
    assert "…" in str(alice)  # truncated base58 for an unlabelled account

    alice.label = "alice"
    assert alice.label == "alice"
    assert str(alice) == "alice"
    # labels are keyed by address, so any view of it resolves the same
    assert str(Account(alice.pubkey)) == "alice"
    assert repr(alice) == f"Account({alice.pubkey}, \"alice\", signer)"
