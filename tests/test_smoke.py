import pytest

from solana_fuzzer import *
from solana_fuzzer._codec import AccountFlagOverride

# The plugin (`solana-fuzzer test` / plain `pytest`) resets the global `svm`
# and reseeds `random` before each test, so tests use them directly.

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"


def test_pubkey_constructors():
    s = "11111111111111111111111111111111"
    assert str(Pubkey(s)) == s
    pk = Pubkey(s)
    assert Pubkey(pk.to_bytes()) == pk      # from bytes
    assert Pubkey(pk) == pk                  # passthrough
    assert Pubkey(1).to_bytes() == b"\x00" * 31 + b"\x01"  # from int, big-endian


def test_default_svm():
    assert isinstance(svm, LiteSVM)
    alice = Account.new()
    svm.airdrop(alice, 1_000_000_000)
    assert alice.lamports == 1_000_000_000


def test_airdrop_and_account_view():
    alice = Account.new()
    res = svm.airdrop(alice, 1_000_000_000)
    assert res.success, res.error
    assert alice.exists
    assert alice.lamports == 1_000_000_000


def test_account_keypair():
    # generated -> can sign
    a = Account.new()
    assert a.can_sign
    assert len(a.secret) == 64

    # bare address -> cannot sign
    b = Account(Pubkey(7))
    assert not b.can_sign
    try:
        _ = b.secret
        assert False, "expected ValueError"
    except ValueError:
        pass

    # PDA -> cannot sign
    pda, _ = Account.find_program_address([b"x"], Pubkey(1))
    assert not pda.can_sign

    # from_secret round-trips to the same address
    c = Account.from_secret(a.secret)
    assert c.pubkey == a.pubkey and c.can_sign


def test_address_like_args():
    alice = Account.new()

    # LiteSVM methods accept an Account directly, not just a Pubkey
    svm.airdrop(alice, 1_000_000_000)
    assert alice.lamports == 1_000_000_000

    # set_account accepts an Account for both address and owner
    data_acc = Account(Pubkey(50))
    svm.set_account(data_acc, lamports=42, owner=alice)
    assert data_acc.owner == alice.pubkey

    # constructors accept an Account too
    assert Pubkey(alice) == alice.pubkey
    ix = Instruction(alice, [alice], b"")
    assert ix.program_id == alice.pubkey
    assert ix.accounts[0].pubkey == alice.pubkey


def test_set_account_and_read():
    addr = Pubkey(42)
    owner = Pubkey(1)
    svm.set_account(addr, lamports=999, data=b"hello", owner=owner)

    acc = Account(addr)
    assert acc.lamports == 999
    assert acc.data == b"hello"
    assert acc.owner == owner


def test_missing_account_raises():
    acc = Account(Pubkey(7))
    assert not acc.exists
    try:
        _ = acc.data
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_reset():
    alice = Account.new()
    svm.airdrop(alice, 1_000_000_000)
    assert alice.exists
    svm.reset()
    assert not alice.exists


def test_account_program_address():
    program_id = Pubkey(1)
    seeds = [b"vault"]

    acc, bump = Account.find_program_address(seeds, program_id)
    assert isinstance(acc, Account)
    assert acc.svm is svm
    expected, expected_bump = Pubkey.find_program_address(seeds, program_id)
    assert acc.pubkey == expected and bump == expected_bump
    assert not acc.exists  # derived address, nothing stored yet


def test_find_program_address():
    program_id = Pubkey(1)
    seeds = [b"seed", b"\x01\x02"]

    addr, bump = Pubkey.find_program_address(seeds, program_id)
    assert isinstance(addr, Pubkey)
    assert 0 <= bump <= 255

    # deterministic
    addr2, bump2 = Pubkey.find_program_address(seeds, program_id)
    assert addr == addr2 and bump == bump2

    # find == create with the bump appended
    pk = Pubkey.create_program_address(seeds + [bytes([bump])], program_id)
    assert pk == addr

    # program_id accepts any address form (str/bytes/int)
    addr3, _ = Pubkey.find_program_address(seeds, str(program_id))
    assert addr3 == addr


def test_account_meta_and_markers():
    pk = Pubkey(5)

    # default meta: read-only non-signer
    m = AccountMeta(pk)
    assert m.pubkey == pk and not m.is_signer and not m.is_writable

    # markers
    assert signer(pk).is_signer and not signer(pk).is_writable
    assert writable(pk).is_writable and not writable(pk).is_signer
    assert writable_signer(pk).is_signer and writable_signer(pk).is_writable

    # composable: writable(signer(x)) == writable signer
    c = writable(signer(pk))
    assert c.is_signer and c.is_writable

    # readonly strips writable, keeps signer
    r = readonly(writable_signer(pk))
    assert r.is_signer and not r.is_writable


def test_instruction():
    # bare addresses coerce to read-only non-signer metas; markers set flags
    ix = Instruction(
        Pubkey(1),
        [signer(Pubkey(2)), writable(Pubkey(3)), Pubkey(4)],
        b"\x01\x02",
    )
    assert ix.program_id == Pubkey(1)
    assert ix.data == b"\x01\x02"
    assert len(ix.accounts) == 3
    assert ix.accounts[0].pubkey == Pubkey(2) and ix.accounts[0].is_signer
    assert ix.accounts[1].is_writable and not ix.accounts[1].is_signer
    assert not ix.accounts[2].is_signer and not ix.accounts[2].is_writable

    # mutable
    ix.data = b"\xff\xee"
    assert ix.data == b"\xff\xee"
    ix.program_id = Pubkey(9)
    assert ix.program_id == Pubkey(9)
    ix.accounts = [writable_signer(Pubkey(7))]
    assert len(ix.accounts) == 1 and ix.accounts[0].is_signer

    # defaults
    bare = Instruction(Pubkey(1))
    assert bare.accounts == [] and bare.data == b""


def test_account_tx_requires_keypair():
    payer = Account(Pubkey(7))  # bare address, no keypair
    ix = svm.system.transfer(1, from_=Pubkey(7), to=Pubkey(8))
    try:
        payer.tx(ix)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_account_tx_system_transfer_and_trace():
    alice = Account.new()
    bob = Account.new()
    svm.airdrop(alice, 1_000_000_000)

    # Recipient must end up rent-exempt, so transfer at least that minimum.
    amount = svm.minimum_balance_for_rent_exemption(0)
    res = alice.tx(svm.system.transfer(amount, from_=alice, to=bob))
    assert res.success, res.error
    assert bob.lamports == amount

    # One top-level System Program instruction, no CPIs.
    trace = res.call_trace
    assert len(trace) == 1
    node = trace[0]
    assert node.program_id == Pubkey(0)  # System Program = all-zeros
    assert node.stack_height == 1
    assert len(node.inner) == 0
    assert node.data[:4] == b"\x02\x00\x00\x00"  # Transfer discriminant

    # Resolved accounts carry the right addresses + privilege flags.
    accts = node.accounts
    assert len(accts) == 2
    assert accts[0].pubkey == alice.pubkey
    assert accts[0].is_signer and accts[0].is_writable
    assert accts[1].pubkey == bob.pubkey
    assert accts[1].is_writable and not accts[1].is_signer


def test_account_simulate_is_side_effect_free():
    alice = Account.new()
    bob = Account.new()
    svm.airdrop(alice, 1_000_000_000)
    amount = svm.minimum_balance_for_rent_exemption(0)

    ix = svm.system.transfer(amount, from_=alice, to=bob)
    before = alice.lamports
    res = alice.simulate(ix)

    assert res.success, res.error
    assert res.signature is None              # nothing was sent
    assert res.compute_units_consumed > 0     # but it really executed
    assert len(res.call_trace) == 1           # same trace shape as tx
    # state is untouched by simulation
    assert alice.lamports == before
    assert not bob.exists

    # the same instruction, actually sent, does commit
    assert alice.tx(ix).success
    assert bob.lamports == amount


def test_simulate_surfaces_failure_without_committing():
    alice = Account.new()
    bob = Account.new()
    svm.airdrop(alice, 2_000_000)             # enough to exist + pay fees
    # transfer far more than the balance -> the instruction fails -> raises
    with pytest.raises(TransactionFailed) as exc:
        alice.simulate(svm.system.transfer(1_000_000_000, from_=alice, to=bob))
    assert exc.value.tx.success is False
    assert alice.lamports == 2_000_000        # unchanged (nothing committed)
    assert not bob.exists


def test_error_is_structured_oneliner_reason_in_logs():
    # The raised error is structured (a code, a concise one-liner repr); the human
    # reason is not duplicated on it — it lives in the per-node logs (below).
    payer = Account.new()
    svm.airdrop(payer, 2_000_000)
    with pytest.raises(TransactionFailed) as exc:
        payer.simulate(svm.system.transfer(1_000_000_000, from_=payer, to=Account.new()))
    err = exc.value
    assert err.code == 1                             # System's Custom(1), structured
    assert "\n" not in repr(err)                     # single line, no folded reason
    assert "insufficient lamports" not in repr(err)  # reason is in the logs, not the error
    assert any("insufficient lamports" in line for line in err.tx.call_trace[0].logs)


def test_per_node_logs_single_instruction():
    # a single top-level instruction's own log line lands on its node
    payer = Account.new()
    svm.airdrop(payer, 2_000_000)
    with pytest.raises(TransactionFailed) as exc:
        payer.simulate(svm.system.transfer(10**18, from_=payer, to=Account.new()))
    node = exc.value.tx.call_trace[0]
    assert any("insufficient lamports" in line for line in node.logs)


def test_per_node_logs_attributed_across_cpis():
    payer = Account.new()
    svm.airdrop(payer, 10_000_000_000)
    token = svm.token.program_id
    mint = Account.new()
    payer.tx(svm.system.create_account(
        svm.minimum_balance_for_rent_exemption(82), 82, token, from_=payer, to=mint))
    payer.tx(svm.token.initialize_mint2(0, payer.pubkey, mint=mint))

    res = payer.tx(svm.token.create_ata(payer, Account.new(), mint))
    assert res.success
    root = res.call_trace[0]
    # the ATA program's own logs attach to the root node, not its CPI children
    assert "Create" in root.logs
    assert all("Create" not in child.logs for child in root.inner)
    # `Program return:` is routed to return data, not logs: the token
    # GetAccountDataSize CPI surfaces its returned size via raw_return_value, and
    # no node mistakes that (or a `consumed`/`return` marker) for a log line.
    assert any(child.raw_return_value is not None for child in root.inner)
    assert all(
        "Program return:" not in log and "compute units" not in log
        for node in (root, *root.inner)
        for log in node.logs
    )
    # per-node enrichment is populated from the log stream
    assert root.status == "success"
    assert root.compute_units is not None


def test_sysvar_clock_get_set_and_warp():
    c = svm.clock
    assert (c.slot, c.unix_timestamp) == (0, 0)        # fresh SVM
    svm.set_clock(unix_timestamp=1_900_000_000, slot=12_345)
    after = svm.clock
    assert after.unix_timestamp == 1_900_000_000 and after.slot == 12_345
    assert after.epoch == c.epoch                       # partial: epoch untouched
    svm.warp_to_timestamp(2_000_000_000)
    assert svm.clock.unix_timestamp == 2_000_000_000
    assert svm.clock.slot == 12_345                     # warp only changed time


def test_sysvar_rent_and_others():
    r = svm.rent
    assert r.lamports_per_byte_year > 0 and r.burn_percent in range(101)
    svm.set_rent(burn_percent=0)
    assert svm.rent.burn_percent == 0
    assert svm.rent.lamports_per_byte_year == r.lamports_per_byte_year   # untouched

    assert svm.epoch_schedule.slots_per_epoch > 0
    svm.set_epoch_schedule(warmup=False)
    assert svm.epoch_schedule.warmup is False

    svm.set_last_restart_slot(7)
    assert svm.last_restart_slot == 7

    assert isinstance(svm.epoch_rewards.parent_blockhash, bytes)
    assert isinstance(svm.slot_hashes, list)


def test_tx_infers_extra_signers_from_keystore():
    payer = Account.new()
    svm.airdrop(payer, 5_000_000_000)
    new_acc = Account.new()  # must co-sign create_account; found in the keystore

    rent = svm.minimum_balance_for_rent_exemption(0)
    # No signers= passed: new_acc's keypair is resolved automatically.
    res = payer.tx(svm.system.create_account(rent, 0, Pubkey(0), from_=payer, to=new_acc))
    assert res.success, res.error
    assert new_acc.exists


def test_airdrop_has_no_trace():
    # airdrop builds its transaction inside litesvm, so no message is available.
    res = svm.airdrop(Account.new(), 1_000_000_000)
    assert res.success
    assert len(res.call_trace) == 0


def test_system_transfer():
    alice = Account.new()
    bob = Account.new()

    ix = svm.system.transfer(1_000, from_=alice, to=bob)
    assert ix.program_id == Pubkey(0)  # System Program = all-zeros
    assert len(ix.accounts) == 2
    assert ix.accounts[0].pubkey == alice.pubkey
    assert ix.accounts[0].is_signer and ix.accounts[0].is_writable
    assert ix.accounts[1].pubkey == bob.pubkey
    assert ix.accounts[1].is_writable and not ix.accounts[1].is_signer
    assert ix.data[:4] == b"\x02\x00\x00\x00"  # Transfer discriminant

    # an explicit meta overrides the spec flags (and warns, suppressibly)
    with pytest.warns(AccountFlagOverride):
        ix2 = svm.system.transfer(1_000, from_=readonly(alice), to=bob)
    assert not ix2.accounts[0].is_signer and not ix2.accounts[0].is_writable


def test_system_create_account():
    payer = Account.new()
    new_acc = Account.new()

    ix = svm.system.create_account(500_000, 100, Pubkey(0), from_=payer, to=new_acc)
    assert ix.program_id == Pubkey(0)
    assert len(ix.accounts) == 2
    assert all(m.is_signer and m.is_writable for m in ix.accounts)
    assert ix.data[:4] == b"\x00\x00\x00\x00"  # CreateAccount discriminant

    # lamports is an explicit data arg under the data-first convention
    rent = svm.minimum_balance_for_rent_exemption(100)
    ix2 = svm.system.create_account(rent, 100, Pubkey(0), from_=payer, to=new_acc)
    lamports_field = int.from_bytes(ix2.data[4:12], "little")
    assert lamports_field == rent


def test_system_assign_allocate():
    acc = Account.new()

    ix = svm.system.assign(Pubkey(9), account=acc)
    assert ix.program_id == Pubkey(0)
    assert len(ix.accounts) == 1
    assert ix.accounts[0].is_signer and ix.accounts[0].is_writable
    assert ix.data[:4] == b"\x01\x00\x00\x00"  # Assign discriminant

    ix2 = svm.system.allocate(200, account=acc)
    assert ix2.data[:4] == b"\x08\x00\x00\x00"  # Allocate discriminant


def test_token_program_id():
    assert svm.token.program_id == Pubkey(TOKEN_PROGRAM)


def test_token_initialize_mint():
    mint = Account.new()
    authority = Account.new()

    ix = svm.token.initialize_mint2(6, authority, mint=mint)
    assert ix.program_id == Pubkey(TOKEN_PROGRAM)
    assert len(ix.accounts) == 1
    assert ix.accounts[0].pubkey == mint.pubkey
    assert ix.accounts[0].is_writable and not ix.accounts[0].is_signer
    assert ix.data[0] == 20  # InitializeMint2 discriminant


def test_token_transfer_checked():
    src = Account.new()
    dst = Account.new()
    mint = Account.new()
    authority = Account.new()

    ix = svm.token.transfer_checked(100, 6, source=src, mint=mint,
                                    destination=dst, authority=authority)
    # source(W), mint(RO), dest(W), authority(S)
    flags = [(m.is_signer, m.is_writable) for m in ix.accounts]
    assert flags == [(False, True), (False, False), (False, True), (True, False)]
    assert ix.data[0] == 12  # TransferChecked discriminant

    # explicit meta overrides the spec flags (and warns, suppressibly)
    with pytest.warns(AccountFlagOverride):
        ix2 = svm.token.transfer_checked(100, 6, source=readonly(src), mint=mint,
                                         destination=dst, authority=authority)
    assert not ix2.accounts[0].is_writable


def test_token_ata():
    owner = Account.new()
    mint = Account.new()

    ata = svm.token.ata_address(owner, mint)
    assert isinstance(ata, Pubkey)

    ix = svm.token.create_ata(owner, owner, mint)
    assert ix.program_id == Pubkey(ATA_PROGRAM)  # targets the ATA program
    assert len(ix.accounts) == 6
    assert ix.accounts[1].pubkey == ata  # derived ATA address


def test_random_exported():
    import random as stdlib_random

    assert isinstance(random, stdlib_random.Random)
    # deterministic when seeded
    random.seed(b"abc")
    first = [random.random() for _ in range(3)]
    random.seed(b"abc")
    assert [random.random() for _ in range(3)] == first


def test_sigverify_flag():
    # constructor test — needs explicit LiteSVM instances
    assert LiteSVM().sigverify is True
    assert LiteSVM(sigverify=False).sigverify is False


def test_sigverify_setter():
    alice = Account.new()
    svm.airdrop(alice, 1_000_000_000)

    svm.sigverify = False
    assert svm.sigverify is False
    assert alice.exists  # state preserved across the toggle

    svm.sigverify = True
    assert svm.sigverify is True


def test_tx_allows_missing_signer_when_sigverify_off():
    # With signature verification off, a required signer whose key the harness
    # doesn't hold gets a placeholder signature instead of raising.
    payer = Account.new()
    svm.airdrop(payer, 1_000_000_000)
    stranger = Account(Pubkey(bytes([7] * 31 + [1])))   # bare view: key not known
    svm.airdrop(stranger, 1_000_000_000)
    bob = Account.new()
    assert not stranger.can_sign

    # sigverify on (default): can't resolve a key for `stranger` -> raises
    with pytest.raises(ValueError, match="no keypair known"):
        payer.tx(svm.system.transfer(2_000_000, from_=stranger, to=bob))

    # sigverify off: the tx builds with a placeholder signature, runs, and commits
    svm.sigverify = False
    before = stranger.lamports
    res = payer.tx(svm.system.transfer(2_000_000, from_=stranger, to=bob))
    assert res.success, res.error
    assert stranger.lamports == before - 2_000_000
    assert bob.lamports == 2_000_000


def test_tx_allows_keyless_fee_payer_when_sigverify_off():
    # The fee payer is a signer too; with sigverify off it need not hold a key.
    funder = Account(Pubkey(bytes([9] * 31 + [2])))     # bare view: key not known
    svm.airdrop(funder, 1_000_000_000)
    bob = Account.new()
    assert not funder.can_sign

    with pytest.raises(ValueError, match="fee payer account has no keypair"):
        funder.tx(svm.system.transfer(1_000_000, from_=funder, to=bob))

    svm.sigverify = False
    res = funder.tx(svm.system.transfer(1_000_000, from_=funder, to=bob))
    assert res.success, res.error


def test_tx_skips_signing_when_sigverify_and_history_off():
    # Fast path: with sigverify off AND transaction_history off, tx signatures are
    # cosmetic (never verified, never used as a dedup key), so the ed25519 work is
    # skipped even for a keypair-holding fee payer. The tx still executes and
    # commits; its signature is the all-zero placeholder.
    svm.sigverify = False
    svm.transaction_history = False
    payer = Account.new()
    svm.airdrop(payer, 1_000_000_000)
    bob = Account.new()
    assert payer.can_sign  # holds a key, yet signing is still skipped

    res = payer.tx(svm.system.transfer(1_000_000, from_=payer, to=bob))
    assert res.success, res.error
    assert bob.lamports == 1_000_000
    # The deterministic proof signing was skipped: a key-holding fee payer would
    # otherwise return a real signature (see test_tx_signs_for_real_when_sigverify_on),
    # but here the slot is left at the all-zero placeholder.
    assert res.signature == bytes(64)


def test_tx_still_signs_when_history_on_and_sigverify_off():
    # Regression guard: with transaction_history ON, signatures are the dedup key
    # even when sigverify is off, so real signing must be kept — otherwise every tx
    # would carry the all-zero signature and the second would collide as
    # AlreadyProcessed. Distinct txs must both execute and carry real signatures.
    svm.sigverify = False
    svm.transaction_history = True
    payer = Account.new()
    svm.airdrop(payer, 1_000_000_000)
    carol = Account.new()
    dave = Account.new()

    res_a = payer.tx(svm.system.transfer(1_000_000, from_=payer, to=carol))
    res_b = payer.tx(svm.system.transfer(2_000_000, from_=payer, to=dave))
    assert res_a.success, res_a.error
    assert res_b.success, res_b.error  # no false AlreadyProcessed
    assert res_a.signature != bytes(64)  # real signature, not a placeholder
    assert res_a.signature != res_b.signature


def test_tx_signs_for_real_when_sigverify_on():
    # With sigverify on (the default), a keypair-holding fee payer still produces a
    # real, non-placeholder signature — the fast path must not touch this mode.
    assert svm.sigverify is True
    payer = Account.new()
    svm.airdrop(payer, 1_000_000_000)
    bob = Account.new()

    res = payer.tx(svm.system.transfer(1_000_000, from_=payer, to=bob))
    assert res.success, res.error
    assert res.signature != bytes(64)


def test_blockhash_check_setter():
    assert svm.blockhash_check is True
    alice = Account.new()
    svm.airdrop(alice, 1_000_000_000)

    svm.blockhash_check = False
    assert svm.blockhash_check is False
    assert alice.exists  # state preserved across the toggle

    svm.blockhash_check = True
    assert svm.blockhash_check is True
