"""Thread-safe current snapshot and history shared by all views."""

import itertools
import threading
import time
from collections import deque


class SnapshotStore:
    def __init__(self, interval: float):
        self._lock = threading.Lock()
        self._snapshot: dict = {}
        self._published_at = None
        self._history: deque = deque(maxlen=max(1, round(60 * 60 / interval)))

    def publish(self, snapshot: dict) -> None:
        history_item = {
            "at": snapshot["collected_at"],
            "by_model": snapshot["by_model_stats"],
        }
        with self._lock:
            self._snapshot = snapshot
            self._published_at = time.monotonic()
            self._history.append(history_item)

    def snapshot(self) -> dict:
        with self._lock:
            snapshot = dict(self._snapshot)
            snapshot["age_seconds"] = max(0, time.monotonic() - self._published_at) if self._published_at else None
        snapshot.setdefault("by_model", {})
        return snapshot

    def history(self, limit: int = 0) -> list:
        with self._lock:
            if limit <= 0:
                return list(self._history)
            start = max(0, len(self._history) - limit)
            return list(itertools.islice(self._history, start, None))
