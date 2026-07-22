from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .models import AircraftStore


ROUTE_BASE_URL = "https://vrs-standing-data.adsb.lol/routes"


@dataclass(frozen=True)
class FlightRoute:
    origin: str
    destination: str
    origin_city: str = ""
    destination_city: str = ""


def normalize_callsign(callsign: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", callsign.upper())


def route_url(callsign: str) -> str:
    normalized = normalize_callsign(callsign)
    if len(normalized) < 3:
        return ""
    return f"{ROUTE_BASE_URL}/{normalized[:2]}/{normalized}.json"


def parse_route(payload: dict[str, Any]) -> FlightRoute | None:
    airports = payload.get("_airports")
    if isinstance(airports, list) and len(airports) >= 2:
        first, last = airports[0], airports[-1]
        if isinstance(first, dict) and isinstance(last, dict):
            origin = str(first.get("iata") or first.get("icao") or "").strip().upper()
            destination = str(last.get("iata") or last.get("icao") or "").strip().upper()
            if origin and destination:
                return FlightRoute(
                    origin=origin,
                    destination=destination,
                    origin_city=str(first.get("location") or "").strip(),
                    destination_city=str(last.get("location") or "").strip(),
                )

    codes = str(payload.get("_airport_codes_iata") or payload.get("airport_codes") or "")
    parts = [part.strip().upper() for part in codes.split("-") if part.strip()]
    if len(parts) >= 2 and "UNKNOWN" not in parts:
        return FlightRoute(parts[0], parts[-1])
    return None


def fetch_route(callsign: str, timeout: float = 8.0) -> FlightRoute | None:
    url = route_url(callsign)
    if not url:
        return None
    request = urllib.request.Request(url, headers={"User-Agent": "PlaneRadarPi/2.3"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if not isinstance(payload, dict):
        return None
    return parse_route(payload)


class RouteEnricher(threading.Thread):
    def __init__(
        self,
        store: AircraftStore,
        cache_path: str | Path,
        cache_hours: float = 6.0,
        online_check=None,
    ):
        super().__init__(name="route-enricher", daemon=True)
        self.store = store
        self.cache_path = Path(cache_path)
        self.cache_seconds = max(1.0, cache_hours * 3600)
        self.negative_cache_seconds = min(self.cache_seconds, 3600.0)
        self.online_check = online_check
        self._stop_event = threading.Event()
        self._cache: dict[str, dict[str, Any]] = self._load_cache()
        self._retry_after: dict[str, float] = {}

    def stop(self) -> None:
        self._stop_event.set()

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            temporary.write_text(json.dumps(self._cache, separators=(",", ":")) + "\n")
            temporary.replace(self.cache_path)
        except OSError as exc:
            logging.warning("Could not save route cache: %s", exc)

    def _cached(self, callsign: str, now: float) -> tuple[bool, FlightRoute | None]:
        entry = self._cache.get(callsign)
        try:
            expires = float(entry.get("expires", 0)) if isinstance(entry, dict) else 0.0
        except (TypeError, ValueError):
            expires = 0.0
        if not isinstance(entry, dict) or expires <= now:
            self._cache.pop(callsign, None)
            return False, None
        route_data = entry.get("route")
        if not isinstance(route_data, dict):
            return True, None
        try:
            return True, FlightRoute(**route_data)
        except TypeError:
            self._cache.pop(callsign, None)
            return False, None

    def _apply(self, callsign: str, route: FlightRoute | None) -> None:
        if route:
            self.store.set_route(
                callsign,
                route.origin,
                route.destination,
                route.origin_city,
                route.destination_city,
                "available",
            )
        else:
            self.store.set_route(callsign, status="unavailable")

    def enrich_once(self, now: float | None = None) -> bool:
        if self.online_check is not None and not self.online_check():
            return False
        now = time.time() if now is None else now
        for raw_callsign in self.store.route_candidates():
            callsign = normalize_callsign(raw_callsign)
            if not callsign:
                self.store.set_route(raw_callsign, status="unavailable")
                continue
            cached, route = self._cached(callsign, now)
            if cached:
                self._apply(raw_callsign, route)
                return True
            if self._retry_after.get(callsign, 0) > now:
                continue
            try:
                route = fetch_route(callsign)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                logging.info("Route lookup unavailable for %s: %s", callsign, exc)
                self._retry_after[callsign] = now + 300
                continue
            self._retry_after.pop(callsign, None)
            ttl = self.cache_seconds if route else self.negative_cache_seconds
            self._cache[callsign] = {
                "expires": now + ttl,
                "route": asdict(route) if route else None,
            }
            self._save_cache()
            self._apply(raw_callsign, route)
            return True
        return False

    def run(self) -> None:
        while not self._stop_event.is_set():
            worked = self.enrich_once()
            self._stop_event.wait(0.35 if worked else 2.0)
