"""Run one Router's collection and keepalive against a shared observation store."""

import logging
import threading
from collections.abc import Callable
from contextlib import ExitStack

from .collection.collector import RouterCollector
from .config import MonitorConfig
from .maintenance.keepalive import KeepaliveControl
from .state import SnapshotStore

logger = logging.getLogger(__name__)


class RouterTarget:
    def __init__(self, config: MonitorConfig, store: SnapshotStore | None = None):
        self.config = config
        self.store = store if store is not None else SnapshotStore()
        self.keepalive = KeepaliveControl(config, self.store)
        self._stop = threading.Event()

    def run(self, update: Callable[[dict], None] | None = None) -> None:
        with ExitStack() as resources:
            collector = RouterCollector(self.config)
            resources.callback(collector.close)
            resources.callback(self.keepalive.close)
            self.keepalive.start()
            while not self._stop.is_set():
                try:
                    snapshot = collector.collect()
                    self.store.publish(snapshot)
                    if update is not None:
                        update(snapshot)
                except Exception:
                    logger.exception("Monitor collection cycle failed for %s", self.config.router_url)
                self._stop.wait(self.config.interval)

    def stop(self) -> None:
        self._stop.set()
        self.keepalive.stop()
