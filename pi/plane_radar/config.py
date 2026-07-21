from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class RadarConfig:
    latitude: float = 0.0
    longitude: float = 0.0
    range_km: int = 50
    range_options_km: tuple[int, ...] = (10, 25, 50, 100)
    tracking_range_km: int = 100
    fetch_interval_seconds: float = 15.0
    history_minutes: float = 12.0
    stale_after_seconds: float = 90.0
    sweep_seconds: float = 8.0
    frames_per_second: float = 8.0
    spi_hz: int = 40_000_000
    brightness: float = 1.0
    display_mirror_x: bool = True
    touch_mirror_x: bool = False
    show_ground_aircraft: bool = False
    auto_update: bool = True
    update_repository: str = "clarkey1993/plane-radar-ota"
    update_channel: str = "stable"

    @classmethod
    def load(cls, path: str | Path | None) -> "RadarConfig":
        config = cls()
        if not path:
            return config
        source = Path(path)
        if not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(json.dumps(asdict(config), indent=2) + "\n")
            return config
        values = json.loads(source.read_text())
        if "range_options_km" in values:
            values["range_options_km"] = tuple(values["range_options_km"])
        for key, value in values.items():
            if hasattr(config, key):
                setattr(config, key, value)
        return config

    def save(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps(asdict(self), indent=2) + "\n")
        temporary.replace(destination)

    def cycle_range(self) -> int:
        try:
            index = self.range_options_km.index(self.range_km)
        except ValueError:
            index = 0
        self.range_km = self.range_options_km[(index + 1) % len(self.range_options_km)]
        return self.range_km
