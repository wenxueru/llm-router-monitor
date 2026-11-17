"""Thread-safe current snapshot and history shared by all views."""

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from itertools import islice

from .config import MonitorConfig


class SnapshotStore:
    def __init__(self, history_limit: int | None = None):
        self._lock = threading.Lock()
        self._snapshot: dict = {}
        self._published_at = None
        self._history: deque[dict] = deque(maxlen=history_limit)

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

    def latest_sample(self) -> dict | None:
        with self._lock:
            return self._history[-1] if self._history else None

    def history(self, limit: int = 0) -> list:
        with self._lock:
            if limit <= 0 or limit >= len(self._history):
                return list(self._history)
            return list(islice(reversed(self._history), limit))[::-1]


@dataclass(frozen=True)
class RouterTarget:
    config: MonitorConfig
    store: SnapshotStore = field(default_factory=SnapshotStore)
