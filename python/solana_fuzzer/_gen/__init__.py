"""The ``solana-fuzzer gen`` generator: read Anchor IDL JSON and emit a
self-registering ``pytypes/`` package (§9). Pure Python; emits modules shaped
like ``tests/fixture_program.py`` against the ``solana_fuzzer._codec`` engine.
"""

from __future__ import annotations

GENERATOR_VERSION = "solana-fuzzer gen 0.1.0"
SCHEMA_VERSION = "1.0.0"

from .run import run_gen   # noqa: E402  (public entry point; imported lazily by cli)

__all__ = ["run_gen", "GENERATOR_VERSION", "SCHEMA_VERSION"]
