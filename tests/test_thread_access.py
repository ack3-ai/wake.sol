"""The SVM must be readable from a non-owning thread.

`PyLiteSVM` used to be `#[pyclass(unsendable)]`, which pinned it to its creating
thread: any access elsewhere aborted with a PyO3 thread-check panic
(`assertion 'left == right' failed: ... is unsendable, but sent to another
thread`). That fired during ordinary debugging, because prompt_toolkit runs
ipdb/IPython tab-completion on a background thread via `ThreadedCompleter`, and
completing a local like `svm.` or `self.vault.` introspects the object there.

`litesvm::LiteSVM` is `Send` and no method in the crate releases the GIL, so the
GIL serializes access and the marker was unnecessary.
"""
import threading

from wake_sol import Account, svm


def _in_thread(fn):
    """Run `fn` on a fresh thread; re-raise whatever it raised."""
    box = {}
    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:      # PanicException is not an Exception
            box["error"] = exc
    t = threading.Thread(target=run)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_svm_method_call_from_other_thread():
    assert _in_thread(lambda: svm.minimum_balance_for_rent_exemption(16)) > 0


def test_account_read_from_other_thread():
    acc = Account.new()
    svm.airdrop(acc, 1_000_000_000)
    assert _in_thread(lambda: acc.lamports) == 1_000_000_000


def test_completer_style_introspection_from_other_thread():
    """What tab-completion actually does: dir() + getattr over every name."""
    def sweep():
        return sum(1 for n in dir(svm) if getattr(svm, n, None) is not None)
    assert _in_thread(sweep) > 0


def test_state_written_on_thread_is_visible_on_main_thread():
    """Not just readable — mutations cross the boundary coherently."""
    acc = Account.new()
    _in_thread(lambda: svm.airdrop(acc, 2_000_000_000))
    assert acc.lamports == 2_000_000_000
