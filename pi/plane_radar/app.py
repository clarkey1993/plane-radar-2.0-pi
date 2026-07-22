from __future__ import annotations

import argparse
import logging
import signal
import time
from pathlib import Path

from .api import AircraftFetcher
from .config import RadarConfig
from .hardware import FT6336Touch, ST7796Display
from .models import AircraftStore
from .renderer import RadarRenderer
from .routes import RouteEnricher
from .wifi import WifiManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plane Radar 2.0 for Raspberry Pi")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--headless", action="store_true", help="render without display hardware")
    parser.add_argument("--once", action="store_true", help="render one frame and exit")
    parser.add_argument("--output", default="plane-radar-preview.png")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = RadarConfig.load(args.config)
    if config.show_ground_aircraft:
        # Version 2.2.1 removes ground/surface plots from the product display.
        # Persist the migration so older installed configurations do not keep
        # re-enabling the white ground targets after an OTA update.
        config.show_ground_aircraft = False
        config.save(args.config)
    store = AircraftStore(
        config.latitude,
        config.longitude,
        config.history_minutes * 60,
        config.stale_after_seconds,
    )
    renderer = RadarRenderer(config)
    wifi = None if args.headless else WifiManager()
    fetcher = AircraftFetcher(config, store, wifi.is_connected if wifi else None)
    fetcher.start()
    route_enricher = None
    if config.route_lookup_enabled:
        route_enricher = RouteEnricher(
            store,
            config.route_cache_path,
            config.route_cache_hours,
            wifi.is_connected if wifi else None,
        )
        route_enricher.start()
    display = None if args.headless else ST7796Display(
        config.spi_hz,
        config.brightness,
        config.display_mirror_x,
    )
    touch = None if args.headless else FT6336Touch(config.touch_mirror_x)
    running = True
    was_connected = wifi.is_connected() if wifi else True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    frame_interval = 1.0 / max(1.0, config.frames_per_second)
    try:
        while running:
            started = time.monotonic()
            if wifi:
                wifi.poll()
                connected = wifi.is_connected()
                if connected and not was_connected:
                    fetcher.refresh()
                elif was_connected and not connected:
                    store.clear("Connect to Wi-Fi")
                was_connected = connected
                wifi_snapshot = wifi.snapshot()
            else:
                wifi_snapshot = None
            planes = store.snapshot(config.range_km)
            frame = renderer.render(planes, store, started, wifi_snapshot)
            if display:
                display.show(frame)
            elif args.once:
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                frame.save(args.output)
            if touch:
                point = touch.read()
                if point:
                    action = renderer.handle_touch(*point)
                    if action == "range":
                        fetcher.refresh()
                    if action == "history":
                        store.history_seconds = config.history_minutes * 60
                    if action == "location":
                        store.set_center(config.latitude, config.longitude)
                        fetcher.refresh()
                    if action == "brightness" and display:
                        display.set_brightness(config.brightness)
                    if action == "wifi_scan" and wifi:
                        wifi.scan()
                    if action == "wifi_connect" and wifi:
                        request = renderer.take_wifi_request()
                        if request:
                            network, password = request
                            wifi.connect(network, password)
                    if action in {"range", "history", "sweep", "brightness", "location"}:
                        config.save(args.config)
            if args.once:
                break
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, frame_interval - elapsed))
    finally:
        if route_enricher:
            route_enricher.stop()
            route_enricher.join(timeout=2)
        fetcher.stop()
        fetcher.join(timeout=2)
        if touch:
            touch.close()
        if display:
            display.close()
    return 0
