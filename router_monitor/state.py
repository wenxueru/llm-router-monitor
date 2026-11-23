"""Thread-safe current snapshot and history shared by all views."""

import threading
import time
from collections import deque
from itertools import islice


class SnapshotStore:
    def __init__(self, history_limit: int | None = None):
        self._lock = threading.Lock()
        self._snapshot: dict = {}
        self._published_at = None
        self._keepalive: dict | None = None
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

    def observation(self) -> tuple[dict, float | None]:
        """Read the last published observation and its monotonic timestamp."""
        with self._lock:
            return self._snapshot, self._published_at

    def snapshot(self) -> dict:
        with self._lock:
            snapshot = dict(self._snapshot)
            snapshot["age_seconds"] = time.monotonic() - self._published_at if self._published_at is not None else None
            if self._keepalive is not None:
                snapshot["keepalive"] = dict(self._keepalive)
        snapshot.setdefault("by_model", {})
        return snapshot

    def publish_keepalive(self, status: str, inflight: int, dispatched: int, cancelled: int) -> None:
        with self._lock:
            self._keepalive = {"status": status, "inflight": inflight, "dispatched": dispatched, "cancelled": cancelled}

    def clear_keepalive(self) -> None:
        with self._lock:
            self._keepalive = None

    def latest_sample(self) -> dict | None:
        with self._lock:
            return self._history[-1] if self._history else None

    def history(self, limit: int = 0) -> list:
        with self._lock:
            if limit <= 0 or limit >= len(self._history):
                return list(self._history)
            recent = list(islice(reversed(self._history), limit))
        recent.reverse()
        return recent
