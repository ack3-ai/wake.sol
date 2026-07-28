"""Signature-verification precompiles (ed25519 / secp256k1 / secp256r1) and the
signing API. Every `verify` is checked against the *real* runtime verifier —
valid claims succeed, forged/tampered ones fail — so these double as a
wire-format conformance suite.

The pytest plugin resets the global `svm` and reseeds `random` per test.
"""

import dataclasses

import pytest

from wake_sol import *

MEMO_PROGRAM = Pubkey("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
NATIVE_LOADER = Pubkey("NativeLoader1111111111111111111111111111111")


def _funded_payer():
    payer = Account.new()
    svm.airdrop(payer, 10_000_000_000)
    return payer


# --------------------------------------------------------------------------- #
# precompiles are present (Task 1)                                            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "pid", [ED25519_PROGRAM_ID, SECP256K1_PROGRAM_ID, SECP256R1_PROGRAM_ID]
)
def test_precompile_accounts_registered(pid):
    acc = Account(pid)
    assert acc.exists and acc.executable and acc.owner == NATIVE_LOADER


def test_precompiles_survive_reset_and_feature_delta():
    svm.reset()
    assert Account(ED25519_PROGRAM_ID).executable
    # the apply_feature_set rebuild path must keep them registered too
    other = LiteSVM(deactivate=[Pubkey(123456789)])
    assert Account(SECP256R1_PROGRAM_ID, svm=other).executable


# --------------------------------------------------------------------------- #
# ed25519                                                                     #
# --------------------------------------------------------------------------- #
def test_ed25519_sign_claim_shape():
    a = Account.new()
    sm = a.sign(b"hello")
    assert sm.curve == "ed25519"
    assert bytes(sm.identity) == a.pubkey.to_bytes()
    assert len(sm.signature) == 64 and bytes(sm) == sm.signature
    assert sm.recovery_id is None


def test_ed25519_verify_roundtrip():
    payer, a = _funded_payer(), Account.new()
    assert payer.tx(ed25519.verify(a.sign(b"hello world"))).success


def test_ed25519_batch_and_position_independent():
    payer = _funded_payer()
    a, b = Account.new(), Account.new()
    # two claims in one instruction
    assert payer.tx(ed25519.verify(a.sign(b"m1"), b.sign(b"m2"))).success
    # verify at instruction index 1 still works (0xFFFF self sentinel)
    assert payer.tx(ed25519.verify(a.sign(b"first")),
                    ed25519.verify(a.sign(b"second"))).success


def test_ed25519_rejects_forged_and_tampered():
    payer, a = _funded_payer(), Account.new()
    sm = a.sign(b"authentic")
    with pytest.raises(TransactionFailed):
        payer.tx(ed25519.verify(dataclasses.replace(sm, signature=bytes(64))))
    with pytest.raises(TransactionFailed):
        payer.tx(ed25519.verify(dataclasses.replace(sm, message=b"tampered")))


# --------------------------------------------------------------------------- #
# secp256k1 (Ethereum-style)                                                  #
# --------------------------------------------------------------------------- #
def test_secp256k1_key_and_sign():
    k = secp256k1.Key.new()
    assert len(k.eth_address) == 20 and k.identity == k.eth_address
    sm = k.sign(b"withdraw:42")
    assert sm.curve == "secp256k1" and sm.recovery_id in (0, 1, 2, 3)
    assert len(sm.signature) == 64
    # from_secret round-trips
    assert secp256k1.Key.from_secret(k.secret).eth_address == k.eth_address


def test_secp256k1_verify_and_self_index():
    payer, k = _funded_payer(), secp256k1.Key.new()
    assert payer.tx(secp256k1.verify(k.sign(b"a"))).success
    # secp256k1 has no self sentinel: at index 1 it must bind its own index (1),
    # not a hardcoded 0 — put a different precompile at index 0.
    a = Account.new()
    assert payer.tx(ed25519.verify(a.sign(b"x")),
                    secp256k1.verify(k.sign(b"b"))).success


def test_secp256k1_batch_and_rejects_forged():
    payer = _funded_payer()
    k1, k2 = secp256k1.Key.new(), secp256k1.Key.new()
    assert payer.tx(secp256k1.verify(k1.sign(b"m1"), k2.sign(b"m2"))).success
    sm = k1.sign(b"real")
    with pytest.raises(TransactionFailed):
        payer.tx(secp256k1.verify(dataclasses.replace(sm, signature=bytes(64))))


# --------------------------------------------------------------------------- #
# secp256r1 (NIST P-256)                                                       #
# --------------------------------------------------------------------------- #
def test_secp256r1_key_and_sign():
    k = secp256r1.Key.new()
    assert len(k.public_key) == 33 and k.public_key[0] in (2, 3)
    assert k.identity == k.public_key
    sm = k.sign(b"authorize")
    assert sm.curve == "secp256r1" and sm.recovery_id is None and len(sm.signature) == 64


def test_secp256r1_verify_and_rejects_forged():
    payer, k = _funded_payer(), secp256r1.Key.new()
    assert payer.tx(secp256r1.verify(k.sign(b"authorize"))).success
    # position-independent (self sentinel), at index 1
    assert payer.tx(secp256r1.verify(secp256r1.Key.new().sign(b"x")),
                    secp256r1.verify(k.sign(b"y"))).success
    sm = k.sign(b"real")
    with pytest.raises(TransactionFailed):
        payer.tx(secp256r1.verify(dataclasses.replace(sm, signature=bytes(64))))


# --------------------------------------------------------------------------- #
# reproducibility, cross-references, raw hatch                                #
# --------------------------------------------------------------------------- #
def test_keys_reproducible_from_seed():
    random.seed(4242)
    a1, b1 = secp256k1.Key.new().eth_address, secp256r1.Key.new().public_key
    random.seed(4242)
    a2, b2 = secp256k1.Key.new().eth_address, secp256r1.Key.new().public_key
    assert a1 == a2 and b1 == b2


def test_cross_instruction_ref_binds_to_sibling():
    payer, a = _funded_payer(), Account.new()
    msg = b"the-signed-payload"
    sibling = Instruction(MEMO_PROGRAM, [], b"prefix::" + msg)  # data contains msg
    ix = ed25519.verify(a.sign(msg).at(message=Ref(sibling)))
    assert payer.tx(sibling, ix).success
    # the message is NOT duplicated into the precompile instruction's own data
    concrete = ix.resolve(1, [sibling, None])
    assert msg not in bytes(concrete.data)


def test_cross_ref_explicit_offset_and_raw_index():
    payer, a = _funded_payer(), Account.new()
    msg = b"payload-xyz"
    sib = Instruction(MEMO_PROGRAM, [], b"...." + msg)
    off = 4
    assert payer.tx(sib, ed25519.verify(a.sign(msg).at(
        message=Ref(sib, offset=off, size=len(msg))))).success
    # raw index= hatch: message at instruction 0
    sib0 = Instruction(MEMO_PROGRAM, [], msg)
    assert payer.tx(sib0, ed25519.verify(a.sign(msg).at(
        message=Ref(index=0, offset=0, size=len(msg))))).success


def test_cross_ref_not_found_is_refused():
    payer, a = _funded_payer(), Account.new()
    wrong = Instruction(MEMO_PROGRAM, [], b"unrelated-bytes")
    with pytest.raises(ValueError, match="not found"):
        payer.tx(wrong, ed25519.verify(a.sign(b"absent").at(message=Ref(wrong))))


def test_ref_without_tx_context_refused():
    a = Account.new()
    sib = Instruction(MEMO_PROGRAM, [], b"whatever")
    with pytest.raises(ValueError):
        ed25519.verify(a.sign(b"m").at(message=Ref(sib))).resolve(0, None)


def test_pack_zero_entries_verifies_nothing():
    payer = _funded_payer()
    assert payer.tx(ed25519.pack(0, [])).success


def test_mixed_three_curve_transaction():
    payer, a = _funded_payer(), Account.new()
    k, r = secp256k1.Key.new(), secp256r1.Key.new()
    res = payer.tx(
        ed25519.verify(a.sign(b"ed")),
        secp256r1.verify(r.sign(b"r1")),
        secp256k1.verify(k.sign(b"k1")),  # at index 2 — self-index resolves to 2
    )
    assert res.success


def test_call_trace_renders_precompile():
    payer, a = _funded_payer(), Account.new()
    trace = str(payer.tx(ed25519.verify(a.sign(b"trace"))).call_trace)
    assert "Ed25519 Program" in trace


def test_tx_rejects_non_instruction():
    payer = _funded_payer()
    with pytest.raises(TypeError):
        payer.tx("not an instruction")


# --------------------------------------------------------------------------- #
# regression: builtin Custom(code) attribution behind a precompile            #
# --------------------------------------------------------------------------- #
def test_system_error_attributed_behind_precompile():
    # A precompile emits no `invoke` log, so log-to-node pairing must skip it —
    # otherwise a builtin Custom(code) at a later index loses its program and
    # degrades to UnknownError. Here: System AccountAlreadyInUse (Custom 0).
    svm.transaction_history = False  # we send the same create twice on purpose
    payer, a, target = _funded_payer(), Account.new(), Account.new()
    rent = svm.minimum_balance_for_rent_exemption(0)
    create = lambda: svm.system.create_account(rent, 0, Pubkey(0), from_=payer, to=target)
    payer.tx(create())  # target now exists

    with pytest.raises(SystemProgramError.AccountAlreadyInUse) as e0:   # ix0 baseline
        payer.tx(create())
    assert e0.value.code == 0

    # behind ed25519 at ix1: still the specific System error (was UnknownError)
    with pytest.raises(SystemProgramError.AccountAlreadyInUse) as e1:
        payer.tx(ed25519.verify(a.sign(b"x")), create())
    assert e1.value.code == 0 and e1.value.instruction_index == 1


def test_token_error_attributed_behind_precompile():
    # Same, for SPL Token InsufficientFunds (Custom 1) behind a precompile.
    payer, a = _funded_payer(), Account.new()
    token = svm.token.program_id
    mint = Account.new()
    payer.tx(svm.system.create_account(
        svm.minimum_balance_for_rent_exemption(82), 82, token, from_=payer, to=mint))
    payer.tx(svm.token.initialize_mint2(0, payer.pubkey, mint=mint))
    owner, dst_owner = Account.new(), Account.new()
    payer.tx(svm.token.create_ata(payer, owner, mint))
    payer.tx(svm.token.create_ata(payer, dst_owner, mint))
    src = svm.token.ata_address(owner, mint)
    dst = svm.token.ata_address(dst_owner, mint)
    # transfer 1 token from an empty account -> InsufficientFunds
    transfer = svm.token.transfer_checked(
        1, 0, source=src, mint=mint, destination=dst, authority=owner)

    with pytest.raises(TokenError.InsufficientFunds) as e0:            # ix0 baseline
        payer.tx(transfer)
    assert e0.value.code == 1

    # behind ed25519 at ix1: still the specific Token error (was UnknownError)
    with pytest.raises(TokenError.InsufficientFunds) as e1:
        payer.tx(ed25519.verify(a.sign(b"x")), transfer)
    assert e1.value.code == 1 and e1.value.instruction_index == 1
