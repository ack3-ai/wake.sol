"""Report serialization for the multiprocess runner's results queue.

Putting ``TestReport`` objects directly on the ``multiprocessing.Queue`` and
letting the queue's own pickling carry them to the server is fine for ordinary
reports, but a report whose ``longrepr`` is unpicklable fails *asynchronously* in
the queue's background feeder thread — which can silently drop a failure instead
of reporting it. These helpers serialize explicitly in the worker so a pickling
failure is caught synchronously and falls back to
``TestReport._to_json()`` / ``_from_json`` (the wire format pytest-xdist uses).

Two tagged kinds travel the queue, both with the worker index at ``[1]``:

* ``("pytest_runtest_logreport", index, <pickle bytes>)`` — the fast path;
* ``("pytest_runtest_logreport_json", index, <json-able dict>)`` — the fallback.
"""

from __future__ import annotations

import pickle
from typing import Tuple

import pytest

#: Queue-message kinds for a serialized report (see module docstring).
REPORT_PICKLE = "pytest_runtest_logreport"
REPORT_JSON = "pytest_runtest_logreport_json"


def dump_report(report: pytest.TestReport) -> Tuple[str, object]:
    """Serialize ``report`` for the queue, returning ``(kind, payload)``.

    Tries pickle (lossless, fast); on any exception falls back to the JSON form.
    The payload is always something the queue can move trivially (bytes or a
    plain dict), so the transfer itself never fails in the feeder thread.
    """
    try:
        return REPORT_PICKLE, pickle.dumps(report)
    except Exception:
        return REPORT_JSON, report._to_json()


def load_report(kind: str, payload: object) -> pytest.TestReport:
    """Reconstruct a ``TestReport`` from a :func:`dump_report` ``(kind, payload)``."""
    if kind == REPORT_JSON:
        return pytest.TestReport._from_json(payload)  # type: ignore[arg-type]
    return pickle.loads(payload)  # type: ignore[arg-type]
