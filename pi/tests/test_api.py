import unittest

from plane_radar.api import AircraftFetcher
from plane_radar.config import RadarConfig
from plane_radar.models import AircraftStore


class FetcherTests(unittest.TestCase):
    def test_tracking_radius_does_not_shrink_with_display_range(self):
        config = RadarConfig(range_km=10, tracking_range_km=100)
        store = AircraftStore(config.latitude, config.longitude, 600, 90)
        fetcher = AircraftFetcher(config, store)
        self.assertTrue(fetcher._url().endswith("/54.0"))
        config.range_km = 50
        self.assertTrue(fetcher._url().endswith("/54.0"))
