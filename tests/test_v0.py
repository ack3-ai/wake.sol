"""v0 (versioned) transactions with Address Lookup Tables, end to end.

`Account.tx(..., lookup_tables=[...])` builds a v0 message that sources eligible
accounts from the given ALTs; omitting `lookup_tables` stays legacy (covered by
the rest of the suite). Here a System transfer credits a recipient whose address
is only provided through an ALT — so a correct credit proves the v0 message
compiled *and* litesvm resolved the lookup at execution.
"""
import pytest

from solana_fuzzer import Account, LiteSVM, Pubkey


def _svm_with_payer():
    svm = LiteSVM()
    # A god-mode ALT has last_extended_slot = 0, so its addresses are active at
    # any slot >= 1; advance the clock off genesis so lookups resolve.
    svm.warp_to_slot(100)
    payer = Account.new(svm)
    svm.airdrop(payer, 10_000_000_000)
    return svm, payer


def test_v0_tx_sources_account_from_alt():
    svm, payer = _svm_with_payer()
    recipient = Account.new(svm)
    alt = svm.create_lookup_table([recipient.pubkey])   # recipient only via the ALT
    ix = svm.system.transfer(2_000_000, from_=payer, to=recipient)
    res = payer.tx(ix, lookup_tables=[alt])
    assert res.success, res.error
    assert recipient.lamports == 2_000_000              # resolved to the right pubkey


def test_v0_simulate_commits_nothing():
    svm, payer = _svm_with_payer()
    recipient = Account.new(svm)
    alt = svm.create_lookup_table([recipient.pubkey])
    ix = svm.system.transfer(2_000_000, from_=payer, to=recipient)
    res = payer.simulate(ix, lookup_tables=[alt])
    assert res.success, res.error
    assert not recipient.exists                          # simulation committed nothing


def test_v0_missing_lookup_table_is_a_clear_error():
    svm, payer = _svm_with_payer()
    ghost = Pubkey(bytes([8] * 32))                      # no table at this address
    ix = svm.system.transfer(2_000_000, from_=payer, to=Account.new(svm))
    with pytest.raises(Exception):
        payer.tx(ix, lookup_tables=[ghost])
