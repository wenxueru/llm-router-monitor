"""Run the collectors and presentations for a configured set of Routers."""

import logging
import math
import threading
from contextlib import nullcontext

from .collectors import RouterCollector
from .config import MonitorConfig, PrometheusHistoryConfig
from .dashboard import create_dashboard
from .maintenance import KeepaliveWarmer
from .state import RouterTarget, SnapshotStore
from .terminal import terminal_updates

logger = logging.getLogger(__name__)


class MonitorApplication:
    def __init__(self, routers: dict[str, MonitorConfig], *, history: PrometheusHistoryConfig | None = None):
        self.web_port = next(iter(routers.values())).web_port
        self.history = history
        self.targets = {
            name: RouterTarget(config, SnapshotStore(math.ceil(600 / config.interval) if history else None))
            for name, config in routers.items()
        }

    def _collect_loop(self, target: RouterTarget, collector: RouterCollector, stop: threading.Event) -> None:
        config = target.config
        terminal = terminal_updates(config) if len(self.targets) == 1 else nullcontext()
        with terminal as update:
            while not stop.is_set():
                try:
                    snapshot = collector.collect()
                    target.store.publish(snapshot)
                    if update:
                        update(snapshot)
                except Exception:
                    logger.exception("Monitor collection cycle failed for %s", config.router_url)
                stop.wait(config.interval)

    def run(self) -> None:
        stop = threading.Event()
        collectors = []
        threads = []

        def start(target, *args):
            thread = threading.Thread(target=target, args=args, daemon=True)
            thread.start()
            threads.append(thread)

        try:
            for target in self.targets.values():
                config = target.config
                collector = RouterCollector(config)
                collectors.append(collector)
                if config.keepalive:
                    start(KeepaliveWarmer(config, target.store).run, stop)
                if self.web_port:
                    start(self._collect_loop, target, collector, stop)
                else:
                    self._collect_loop(target, collector, stop)

            if self.web_port:
                import uvicorn

                dashboard = create_dashboard(self.targets, history_config=self.history)
                print(f"[Monitor] Web UI → http://0.0.0.0:{self.web_port}")
                uvicorn.run(dashboard, host="0.0.0.0", port=self.web_port, log_level="error")
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            for thread in threads:
                thread.join()
            for collector in collectors:
                collector.close()
