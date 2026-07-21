import unittest

from plane_radar.models import AircraftStore, bearing_degrees, haversine_km


class ModelTests(unittest.TestCase):
    def test_distance_and_bearing_are_sensible(self):
        self.assertAlmostEqual(haversine_km(0, 0, 0, 1), 111.195, places=2)
        self.assertAlmostEqual(bearing_degrees(0, 0, 0, 1), 90.0)

    def test_store_preserves_real_position_history(self):
        store = AircraftStore(0, 0, history_seconds=600, stale_seconds=90)
        store.update({"ac": [{"hex": "abc123", "lat": 0.1, "lon": 0.1, "alt_baro": 10000, "desc": "TEST AIRCRAFT", "squawk": "1234"}]}, now=100)
        store.update({"ac": [{"hex": "abc123", "lat": 0.11, "lon": 0.12, "alt_baro": 10200, "desc": "TEST AIRCRAFT", "squawk": "1234"}]}, now=115)
        planes = store.snapshot(100)
        self.assertEqual(len(planes), 1)
        self.assertEqual(len(planes[0].history), 2)
        self.assertEqual(planes[0].description, "TEST AIRCRAFT")
        self.assertEqual(planes[0].squawk, "1234")

    def test_ground_aircraft_can_be_retained_at_zero_feet(self):
        store = AircraftStore(0, 0, history_seconds=600, stale_seconds=90)
        store.update(
            {
                "ac": [
                    {"hex": "ground1", "lat": 0.01, "lon": 0.01, "alt_baro": "ground", "t": "B738", "category": "A3"},
                    {"hex": "tower1", "lat": 0.02, "lon": 0.02, "alt_baro": "ground", "t": "TWR", "r": "TWR", "category": "C0"},
                    {"hex": "truck1", "lat": 0.03, "lon": 0.03, "alt_baro": "ground", "category": "C2"},
                ]
            },
            now=100,
            show_ground=True,
        )
        planes = store.snapshot(100)
        self.assertEqual(len(planes), 1)
        self.assertEqual(planes[0].icao, "ground1")
        self.assertEqual(planes[0].altitude_ft, 0)

    def test_changing_center_clears_old_aircraft(self):
        store = AircraftStore(0, 0, history_seconds=600, stale_seconds=90)
        store.update({"ac": [{"hex": "abc123", "lat": 0.1, "lon": 0.1, "alt_baro": 10000}]}, now=100)
        store.set_center(10.0, 20.0)
        self.assertEqual(store.snapshot(100), [])
        self.assertEqual((store.center_lat, store.center_lon), (10.0, 20.0))
