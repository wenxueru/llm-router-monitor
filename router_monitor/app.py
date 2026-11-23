"""Run the collectors and presentations for a configured set of Routers."""

import math
import threading
from contextlib import nullcontext

from .config import MonitorConfig, PrometheusHistoryConfig
from .presentation.dashboard import create_dashboard
from .presentation.terminal import terminal_updates
from .state import SnapshotStore
from .target import RouterTarget


class MonitorApplication:
    def __init__(self, routers: dict[str, MonitorConfig], *, history: PrometheusHistoryConfig | None = None):
        self.web_port = next(iter(routers.values())).web_port
        self.history = history
        self.targets = {
            name: RouterTarget(config, SnapshotStore(math.ceil(600 / config.interval) if history else None))
            for name, config in routers.items()
        }

    def _run_target(self, target: RouterTarget) -> None:
        terminal = terminal_updates(target.config) if len(self.targets) == 1 else nullcontext()
        with terminal as update:
            target.run(update)

    def run(self) -> None:
        threads = []
        try:
            if not self.web_port:
                self._run_target(next(iter(self.targets.values())))
                return

            for target in self.targets.values():
                thread = threading.Thread(target=self._run_target, args=(target,), daemon=True)
                thread.start()
                threads.append(thread)

            import uvicorn

            dashboard = create_dashboard(self.targets, history_config=self.history)
            print(f"[Monitor] Web UI → http://0.0.0.0:{self.web_port}")
            uvicorn.run(dashboard, host="0.0.0.0", port=self.web_port, log_level="error", proxy_headers=False)
        except KeyboardInterrupt:
            pass
        finally:
            for target in self.targets.values():
                target.stop()
            for thread in threads:
                thread.join()
