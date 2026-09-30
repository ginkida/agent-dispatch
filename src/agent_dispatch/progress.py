"""Bounded, disposable progress notifications; final results live elsewhere."""

from __future__ import annotations

import threading
from collections import deque


class ProgressBuffer:
    """Keep recent messages without blocking the subprocess on a slow consumer."""

    MAX_MESSAGES = 100
    MAX_CHARS = 300

    def __init__(self) -> None:
        self._messages: deque[str] = deque(maxlen=self.MAX_MESSAGES)
        self._lock = threading.Lock()
        self._dropped = 0
        self._closed = False

    def append(self, message: str) -> None:
        with self._lock:
            if self._closed:
                return
            if len(self._messages) == self.MAX_MESSAGES:
                self._dropped += 1
            self._messages.append(message[:self.MAX_CHARS])

    def drain(self) -> tuple[list[str], int]:
        """Take one bounded batch and its count of omitted older messages."""
        with self._lock:
            messages = list(self._messages)
            dropped = self._dropped
            self._messages.clear()
            self._dropped = 0
        return messages, dropped

    def close(self) -> None:
        """Discard pending progress and ignore further callbacks after disconnect."""
        with self._lock:
            self._closed = True
            self._messages.clear()
            self._dropped = 0
