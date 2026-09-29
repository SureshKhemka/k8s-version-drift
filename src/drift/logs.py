"""Logging with redaction (FR-32): every record passes through the Redactor before it is emitted."""

from __future__ import annotations

import logging
import sys

from drift.store.recorder import Redactor


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redactor.text(record.getMessage())
        record.args = None
        return True


def setup(level: str, redactor: Redactor, stream: object = None) -> None:
    root = logging.getLogger("drift")
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)  # type: ignore[arg-type]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(redactor))
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
