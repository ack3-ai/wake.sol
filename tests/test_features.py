"""Feature-set control: mainnet default, constructor deltas, mid-life changes.

litesvm's `LiteSVM::new()` already starts from mainnet-beta's feature set, so the
default is mainnet parity. `activate=`/`deactivate=` flip features on top; the
`activate_features`/`deactivate_features` cheatcodes change them on a live SVM
while preserving account state.
"""
from solana_fuzzer import Account, LiteSVM, Pubkey

# Two long-active mainnet features (ids from agave-feature-set), safe to assume
# active in any mainnet snapshot.
PICO_INFLATION = Pubkey("4RWNif6C2WCNiKVW7otP4G7dkmkHGyKQWRpuZ1pxKU5m")
DEPRECATE_REWARDS = Pubkey("GaBtBJvmS4Arjj5W1NmFcyvPjsHN38UGYDq2MDwbs9Qu")
FAKE = Pubkey(bytes([7] * 32))   # not a real feature — exercises the mechanism


def test_default_is_mainnet_feature_set():
    svm = LiteSVM()
    assert svm.is_feature_active(PICO_INFLATION)
    assert svm.is_feature_active(DEPRECATE_REWARDS)
    assert not svm.is_feature_active(FAKE)


def test_constructor_deltas():
    svm = LiteSVM(deactivate=[PICO_INFLATION], activate=[FAKE])
    assert not svm.is_feature_active(PICO_INFLATION)   # deactivated
    assert svm.is_feature_active(FAKE)                 # activated
    assert svm.is_feature_active(DEPRECATE_REWARDS)    # others untouched


def test_midlife_change_preserves_accounts():
    svm = LiteSVM()
    payer = Account.new(svm)
    svm.airdrop(payer, 5_000_000_000)
    assert not svm.is_feature_active(FAKE)

    svm.activate_features(FAKE)                         # mid-life cheatcode
    assert svm.is_feature_active(FAKE)
    assert payer.lamports == 5_000_000_000             # state survived the rebuild

    svm.deactivate_features(FAKE)
    assert not svm.is_feature_active(FAKE)
    assert payer.lamports == 5_000_000_000
