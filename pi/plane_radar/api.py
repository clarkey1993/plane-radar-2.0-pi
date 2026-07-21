from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from .config import RadarConfig
from .models import AircraftStore


class AircraftFetcher(threading.Thread):
    def __init__(
        self,
        config: RadarConfig,
        store: AircraftStore,
        online_check: Callable[[], bool] | None = None,
    ):
        super().__init__(name="aircraft-fetcher", daemon=True)
        self.config = config
        self.store = store
        self.online_check = online_check
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def refresh(self) -> None:
        self._wake_event.set()

    def _url(self) -> str:
        # Track the full configured area regardless of the currently visible
        # range. Range changes are then instant and never discard trail history.
        tracking_range_km = max(self.config.tracking_range_km, max(self.config.range_options_km))
        radius_nm = tracking_range_km * 0.539957
        return (
            "https://api.airplanes.live/v2/point/"
            f"{self.config.latitude:.6f}/{self.config.longitude:.6f}/{radius_nm:.1f}"
        )

    def fetch_once(self) -> None:
        request = urllib.request.Request(self._url(), headers={"User-Agent": "PlaneRadarPi/2.0"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read())
            self.store.update(payload, show_ground=self.config.show_ground_aircraft)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            self.store.set_error(str(exc))

    def run(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.clear()
            if self.online_check is not None and not self.online_check():
                self.store.clear("Connect to Wi-Fi")
                self._wake_event.wait(min(3.0, self.config.fetch_interval_seconds))
                continue
            self.fetch_once()
            self._wake_event.wait(self.config.fetch_interval_seconds)
