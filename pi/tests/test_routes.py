import json
import tempfile
import unittest
from pathlib import Path

from plane_radar.models import AircraftStore
from plane_radar.routes import FlightRoute, RouteEnricher, normalize_callsign, parse_route, route_url


class RouteTests(unittest.TestCase):
    def test_route_payload_uses_first_and_last_airport(self):
        route = parse_route(
            {
                "_airports": [
                    {"iata": "LIN", "icao": "LIML", "location": "Milan"},
                    {"iata": "LHR", "icao": "EGLL", "location": "London"},
                ]
            }
        )
        self.assertEqual(route, FlightRoute("LIN", "LHR", "Milan", "London"))

    def test_callsign_is_sanitized_for_static_route_url(self):
        self.assertEqual(normalize_callsign(" baw577 "), "BAW577")
        self.assertEqual(
            route_url(" baw577 "),
            "https://vrs-standing-data.adsb.lol/routes/BA/BAW577.json",
        )

    def test_persistent_cache_enriches_without_network_lookup(self):
        store = AircraftStore(51.47, -0.46, 600, 90)
        store.update(
            {
                "ac": [
                    {
                        "hex": "4081bb",
                        "flight": "BAW577",
                        "lat": 51.48,
                        "lon": -0.53,
                        "alt_baro": 425,
                    }
                ]
            },
            now=90,
        )
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "routes.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "BAW577": {
                            "expires": 200,
                            "route": {
                                "origin": "LIN",
                                "destination": "LHR",
                                "origin_city": "Milan",
                                "destination_city": "London",
                            },
                        }
                    }
                )
            )
            enricher = RouteEnricher(store, cache_path)
            self.assertTrue(enricher.enrich_once(now=100))

        plane = store.snapshot(100)[0]
        self.assertEqual((plane.route_origin, plane.route_destination), ("LIN", "LHR"))
        self.assertEqual(plane.route_status, "available")

    def test_route_survives_subsequent_live_position_update(self):
        store = AircraftStore(0, 0, 600, 90)
        payload = {
            "ac": [
                {
                    "hex": "abc123",
                    "flight": "TEST123",
                    "lat": 0.1,
                    "lon": 0.1,
                    "alt_baro": 10000,
                }
            ]
        }
        store.update(payload, now=100)
        store.set_route("TEST123", "AAA", "BBB", "Alpha", "Bravo")
        payload["ac"][0]["lat"] = 0.2
        store.update(payload, now=110)
        plane = store.snapshot(100)[0]
        self.assertEqual((plane.route_origin, plane.route_destination), ("AAA", "BBB"))
        self.assertEqual(plane.route_status, "available")
