"""The ``wake-sol gen`` generator: read Anchor IDL JSON and emit a
self-registering ``pytypes/`` package. Pure Python; emits modules shaped like
``tests/fixture_program.py`` against the ``wake_sol._codec`` engine. See
``docs/07-programs-and-addresses.md`` for the user-facing surface.
"""

from __future__ import annotations

GENERATOR_VERSION = "wake-sol gen 0.2.0"
SCHEMA_VERSION = "1.1.0"

from .run import run_gen   # noqa: E402  (public entry point; imported lazily by cli)

__all__ = ["run_gen", "GENERATOR_VERSION", "SCHEMA_VERSION"]
