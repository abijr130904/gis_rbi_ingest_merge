from __future__ import annotations

import sys
from typing import TextIO


class ProgressBar:
    """Small dependency-free terminal progress bar for feature/chunk loops."""

    def __init__(self, total: int, label: str, unit: str = "fitur", stream: TextIO | None = None):
        self.total = max(0, int(total))
        self.current = 0
        self.label = label
        self.unit = unit
        self.stream = stream or sys.stderr
        self._last_percent = -1
        self._closed = False
        self._render(force=True)

    def update(self, amount: int = 1, *, label: str | None = None) -> None:
        self.current = min(self.total, self.current + max(0, int(amount)))
        changed_label = label is not None and label != self.label
        if changed_label:
            self.label = label
        self._render(force=changed_label)

    def _render(self, force: bool = False) -> None:
        percent = 100 if self.total == 0 else int(self.current * 100 / self.total)
        if not force and percent // 10 == self._last_percent // 10:
            return
        width = 24
        filled = width if self.total == 0 else int(width * self.current / self.total)
        bar = "=" * filled + " " * (width - filled)
        current = f"{self.current:,}".replace(",", ".")
        total = f"{self.total:,}".replace(",", ".")
        line = f"{self.label[:48]:48} [{bar}] {percent:3d}% ({current}/{total} {self.unit})"
        # Print complete lines instead of carriage-return updates so captured
        # logs and secure terminal streams always show the percentage.
        self.stream.write(line + "\n")
        self.stream.flush()
        self._last_percent = percent

    def close(self) -> None:
        if not self._closed:
            self._closed = True

    def __enter__(self) -> "ProgressBar":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
