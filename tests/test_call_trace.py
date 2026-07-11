import pytest

from solana_fuzzer import *

TOKEN = Pubkey("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
MINT_LEN = 82


def test_render_system_transfer():
    alice = Account.new()
    bob = Account.new()
    alice.label = "alice"
    bob.label = "bob"
    svm.airdrop(alice, 1_000_000_000)

    amount = svm.minimum_balance_for_rent_exemption(0)
    res = alice.tx(svm.system.transfer(amount, from_=alice, to=bob))
    assert res.success, res.error

    text = str(res.call_trace)
    assert "✓ Transaction" in text
    assert "System Program::transfer" in text
    assert f"lamports={amount:,}" in text
    # account roles + identity labels both appear
    assert "from: alice [SW]" in text
    assert "to: bob [W]" in text


def test_render_nested_ata_creation():
    payer = Account.new()
    payer.label = "payer"
    svm.airdrop(payer, 5_000_000_000)
    mint = Account.new()
    mint.label = "mint"
    rent = svm.minimum_balance_for_rent_exemption(MINT_LEN)
    payer.tx(
        svm.system.create_account(rent, MINT_LEN, TOKEN, from_=payer, to=mint),
        svm.token.initialize_mint2(6, payer, mint=mint),
        signers=[mint],
    )

    owner = Account.new()
    owner.label = "owner"
    res = payer.tx(svm.token.create_ata(payer, owner, mint))

    text = str(res.call_trace)
    # top-level instruction and its decoded CPIs all render
    assert "Associated Token Account Program::create" in text
    assert "System Program::create_account" in text
    assert "Token Program::initialize_account3" in text
    # a pubkey arg resolved through the well-known table
    assert "owner=Token Program" in text


def test_render_failed_transaction():
    # Transfer below rent-exemption to a fresh account fails at runtime -> raises;
    # the failed receipt (and its call_trace) is on the exception's .tx.
    alice = Account.new()
    bob = Account.new()
    svm.airdrop(alice, 1_000_000_000)
    with pytest.raises(TransactionFailed) as exc:
        alice.tx(svm.system.transfer(1, from_=alice, to=bob))

    assert exc.value.tx.success is False
    text = str(exc.value.tx.call_trace)
    assert "✗ Transaction" in text
    assert "System Program::transfer" in text


def test_render_failed_transaction_resolves_builtin_error():
    # A System `Custom` error is resolved to its named class in the trace, attributed
    # to the System program — not surfaced as a bare UnknownError.
    alice = Account.new()
    svm.airdrop(alice, 2_000_000)
    with pytest.raises(TransactionFailed) as exc:
        alice.tx(svm.system.transfer(10**18, from_=alice, to=Account.new()))
    text = str(exc.value.tx.call_trace)
    assert "ResultWithNegativeLamports" in text
    assert "UnknownError" not in text


def test_decode_error_resolves_builtin_and_shows_once():
    # renderer-level: a custom code resolves program-scoped on the originating frame;
    # a parent that only propagated the same code is left to its glyph (shown once).
    from types import SimpleNamespace
    from solana_fuzzer import call_trace as _ct

    system = "11111111111111111111111111111111"
    inner = SimpleNamespace(error="custom program error: 0x0", inner=[], program_id=system)
    outer = SimpleNamespace(error="custom program error: 0x0", inner=[inner],
                            program_id=str(Pubkey(bytes([9] * 32))))

    origin = _ct._decode_error(inner)
    assert origin is not None and "AccountAlreadyInUse" in str(origin)
    assert _ct._decode_error(outer) is None            # propagating parent suppressed

    assert _ct._custom_error_code("custom program error: 0x11") == 17
    assert _ct._custom_error_code("Allocate: already in use") is None
    native = SimpleNamespace(error="native failure text", inner=[], program_id=system)
    assert str(_ct._decode_error(native)) == "native failure text"


def test_render_colored_output_has_ansi():
    from rich.console import Console
    import io

    alice = Account.new()
    svm.airdrop(alice, 1_000_000_000)
    bob = Account.new()
    amount = svm.minimum_balance_for_rent_exemption(0)
    res = alice.tx(svm.system.transfer(amount, from_=alice, to=bob))

    buf = io.StringIO()
    Console(file=buf, force_terminal=True, width=100).print(res.call_trace)
    out = buf.getvalue()
    assert "\x1b[" in out  # ANSI color escapes present
