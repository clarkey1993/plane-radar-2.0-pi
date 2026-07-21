from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    value = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def bearing_degrees(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_lambda = math.radians(lon2 - lon1)
    y = math.sin(d_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(d_lambda)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


@dataclass(frozen=True)
class Position:
    timestamp: float
    latitude: float
    longitude: float


@dataclass
class Aircraft:
    icao: str
    callsign: str
    registration: str
    aircraft_type: str
    description: str
    squawk: str
    emergency: str
    category: str
    source_type: str
    seen_seconds: float
    db_flags: int
    latitude: float
    longitude: float
    altitude_ft: int
    vertical_rate_fpm: int
    speed_kt: int
    track_deg: float
    distance_km: float
    bearing_deg: float
    last_seen: float
    history: deque[Position] = field(default_factory=lambda: deque(maxlen=80))

    @property
    def label(self) -> str:
        return self.callsign or self.registration or self.icao.upper()


def _number(raw: Any, default: float = 0.0) -> float:
    return float(raw) if isinstance(raw, (int, float)) else default


def _is_non_aircraft_ground_target(row: dict[str, Any]) -> bool:
    """Reject towers, surface vehicles, and obstacles from ground plots."""
    aircraft_type = str(row.get("t") or "").strip().upper()
    registration = str(row.get("r") or "").strip().upper()
    category = str(row.get("category") or "").strip().upper()
    return aircraft_type in {"TWR", "TOWER"} or registration in {"TWR", "TOWER"} or category.startswith("C")


class AircraftStore:
    def __init__(self, center_lat: float, center_lon: float, history_seconds: float, stale_seconds: float):
        self.center_lat = center_lat
        self.center_lon = center_lon
        self.history_seconds = history_seconds
        self.stale_seconds = stale_seconds
        self._aircraft: dict[str, Aircraft] = {}
        self._lock = threading.Lock()
        self.last_update = 0.0
        self.last_error = ""

    def update(self, payload: dict[str, Any], now: float | None = None, show_ground: bool = False) -> None:
        now = now or time.time()
        rows = payload.get("ac") or payload.get("aircraft") or []
        with self._lock:
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("lat"), (int, float)) or not isinstance(row.get("lon"), (int, float)):
                    continue
                raw_altitude = row.get("alt_baro", row.get("alt_geom", 0))
                on_ground = raw_altitude == "ground"
                altitude = 0 if on_ground else int(_number(raw_altitude))
                if not show_ground and (on_ground or altitude <= 0):
                    continue
                if (on_ground or altitude <= 0) and _is_non_aircraft_ground_target(row):
                    continue
                icao = str(row.get("hex") or "").lower().strip()
                if not icao:
                    continue
                lat, lon = float(row["lat"]), float(row["lon"])
                distance = haversine_km(self.center_lat, self.center_lon, lat, lon)
                bearing = bearing_degrees(self.center_lat, self.center_lon, lat, lon)
                current = self._aircraft.get(icao)
                history = current.history if current else deque(maxlen=80)
                if not history or haversine_km(history[-1].latitude, history[-1].longitude, lat, lon) >= 0.04:
                    history.append(Position(now, lat, lon))
                while history and now - history[0].timestamp > self.history_seconds:
                    history.popleft()
                callsign = str(row.get("flight") or "").strip()
                registration = str(row.get("r") or "").strip()
                aircraft_type = str(row.get("t") or "").strip()
                description = str(row.get("desc") or "").strip()
                squawk = str(row.get("squawk") or "").strip()
                emergency = str(row.get("emergency") or "none").strip().lower()
                category = str(row.get("category") or "").strip()
                source_type = str(row.get("type") or "").strip()
                seen_seconds = _number(row.get("seen", row.get("seen_pos", 0)))
                db_flags = int(_number(row.get("dbFlags", 0)))
                speed = int(round(_number(row.get("gs", row.get("tas", row.get("ias", 0))))))
                track = _number(row.get("track", row.get("true_heading", row.get("mag_heading", bearing))), bearing)
                vertical_rate = int(round(_number(row.get("baro_rate", row.get("geom_rate", 0)))))
                self._aircraft[icao] = Aircraft(
                    icao=icao,
                    callsign=callsign,
                    registration=registration,
                    aircraft_type=aircraft_type,
                    description=description,
                    squawk=squawk,
                    emergency=emergency,
                    category=category,
                    source_type=source_type,
                    seen_seconds=seen_seconds,
                    db_flags=db_flags,
                    latitude=lat,
                    longitude=lon,
                    altitude_ft=altitude,
                    vertical_rate_fpm=vertical_rate,
                    speed_kt=speed,
                    track_deg=track,
                    distance_km=distance,
                    bearing_deg=bearing,
                    last_seen=now,
                    history=history,
                )
            expired = [key for key, plane in self._aircraft.items() if now - plane.last_seen > self.stale_seconds]
            for key in expired:
                del self._aircraft[key]
            self.last_update = now
            self.last_error = ""

    def set_error(self, message: str) -> None:
        with self._lock:
            self.last_error = message

    def clear(self, message: str = "") -> None:
        with self._lock:
            self._aircraft.clear()
            self.last_update = 0.0
            self.last_error = message

    def set_center(self, latitude: float, longitude: float) -> None:
        with self._lock:
            self.center_lat = latitude
            self.center_lon = longitude
            self._aircraft.clear()
            self.last_update = 0.0
            self.last_error = "Updating radar location"

    def snapshot(self, range_km: float) -> list[Aircraft]:
        with self._lock:
            return sorted(
                (plane for plane in self._aircraft.values() if plane.distance_km <= range_km),
                key=lambda plane: plane.distance_km,
            )

    def status(self) -> tuple[float, str]:
        with self._lock:
            return self.last_update, self.last_error
