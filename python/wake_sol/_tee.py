"""``Tee`` streams — write to both a file and the original stream.

Vendored ~verbatim from wake @ ``feat/version-5.0.0`` (``wake/utils/tee.py``),
which credits https://github.com/algrebe/python-tee. Kept as its own module so
the multiprocess worker can either *redirect* its stdio into a per-worker log
(the Phase 1 default) or *tee* it — so worker 0's output stays live on the
console under ``--attach-first`` (Phase 2). Phase 1 uses plain
:func:`contextlib.redirect_stdout`/``redirect_stderr``; this is here so wiring
the tee later touches nothing else.
"""

from __future__ import annotations

import sys
from abc import abstractmethod


class Tee:
    def __init__(self, filename, mode="a"):
        self.filename = filename
        self.mode = mode

        self.stream = None
        self.fp = None

    @abstractmethod
    def set_stream(self, stream):
        pass

    @abstractmethod
    def get_stream(self):
        pass

    def write(self, message):
        self.stream.write(message)
        self.fp.write(message)

    def flush(self):
        self.stream.flush()
        self.fp.flush()

    def __enter__(self):
        self.stream = self.get_stream()
        self.fp = open(self.filename, self.mode)
        self.set_stream(self)

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        self.close()

    def close(self):
        if self.stream is not None:
            self.set_stream(self.stream)
            self.stream = None

        if self.fp is not None:
            self.fp.close()
            self.fp = None

    def isatty(self):
        return self.stream.isatty()

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self.filename)

    __str__ = __repr__
    __unicode__ = __repr__


class StdoutTee(Tee):
    def set_stream(self, stream):
        sys.stdout = stream

    def get_stream(self):
        return sys.stdout


class StderrTee(Tee):
    def set_stream(self, stream):
        sys.stderr = stream

    def get_stream(self):
        return sys.stderr
