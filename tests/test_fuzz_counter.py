"""Living smoke test: fuzz the native-counter program with the FuzzTest engine.

Doubles as the canonical example of the fuzzing API:

* deploy the program and build per-sequence state in ``pre_sequence`` (the engine
  wipes the SVM to genesis before each sequence);
* drive it with ``@flow`` methods that draw randomness from
  ``wake_sol.random`` (so a run reproduces from ``--seed``);
* check on-chain state against a Python model in an ``@invariant``.

Run just this: ``pytest tests/test_fuzz_counter.py`` — the flow-stats table
prints in the ``wake.sol`` summary section either way (no ``-s`` needed).
Reproduce a failure with ``pytest --seed <hex> ...``.
"""

from pathlib import Path

import pytest

from wake_sol import (
    Account,
    FuzzTest,
    Instruction,
    Pubkey,
    flow,
    invariant,
    random,
    svm,
    writable,
)

SO = Path(__file__).parent.parent / "programs/native-counter/target/deploy/native_counter.so"
PROGRAM_ID = Pubkey(bytes([0xC0] * 32))

pytestmark = pytest.mark.skipif(
    not SO.exists(),
    reason="native-counter .so not built (run: cd programs/native-counter && cargo build-sbf)",
)


class CounterFuzz(FuzzTest):
    """Increment a program-owned ``u64`` counter; the invariant asserts the
    on-chain value tracks a Python model of how many increments we've sent."""

    def pre_sequence(self) -> None:
        # The SVM was wiped to genesis before this sequence, so rebuild the
        # world: redeploy the program, fund a payer, and create a fresh
        # program-owned account holding a zeroed u64.
        svm.add_program(PROGRAM_ID, SO.read_bytes())
        self.payer = Account.new()
        svm.airdrop(self.payer, 1_000_000_000)
        self.counter = Account.new()
        svm.set_account(
            self.counter,
            lamports=svm.minimum_balance_for_rent_exemption(8),
            data=bytes(8),
            owner=PROGRAM_ID,
        )
        self.model = 0

    def _increment_ix(self) -> Instruction:
        # The program ignores instruction data; it just needs the counter as a
        # writable, program-owned account.
        return Instruction(PROGRAM_ID, [writable(self.counter)], b"")

    # `run()` disables transaction history for the fuzz run, so repeated
    # byte-identical increment txs execute instead of being rejected as
    # AlreadyProcessed — no need to rotate the blockhash per step.

    @flow(weight=300)
    def increment(self) -> None:
        self.payer.tx(self._increment_ix())  # raises TransactionFailed on error
        self.model += 1

    @flow(weight=100)
    def increment_batch(self) -> None:
        n = random.randint(2, 4)  # in-flow randomness, drawn from the seeded RNG
        self.payer.tx(*[self._increment_ix() for _ in range(n)])
        self.model += n

    @invariant()
    def counter_matches_model(self) -> None:
        onchain = int.from_bytes(self.counter.data[:8], "little")
        assert onchain == self.model, f"on-chain {onchain} != model {self.model}"


def test_fuzz_counter():
    CounterFuzz.run(sequences_count=4, flows_count=15)
