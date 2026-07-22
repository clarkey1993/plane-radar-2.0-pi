import unittest

from plane_radar.config import RadarConfig
from plane_radar.models import AircraftStore
from plane_radar.renderer import RadarRenderer, aircraft_marker_points, altitude_color
from plane_radar.wifi import WifiNetwork, WifiSnapshot


class RendererTests(unittest.TestCase):
    def test_renderer_outputs_native_screen_size(self):
        config = RadarConfig()
        store = AircraftStore(config.latitude, config.longitude, 600, 90)
        renderer = RadarRenderer(config)
        image = renderer.render([], store, monotonic_now=0)
        self.assertEqual(image.size, (320, 480))

    def test_menu_range_selection(self):
        config = RadarConfig(range_km=50)
        renderer = RadarRenderer(config)
        self.assertEqual(renderer.handle_touch(274, 460), "menu")
        self.assertTrue(renderer.menu_open)
        self.assertEqual(renderer.handle_touch(275, 125), "range")
        self.assertEqual(config.range_km, 100)

    def test_altitude_palette_and_ground_colour(self):
        self.assertEqual(altitude_color(0), (255, 255, 255))
        self.assertEqual(altitude_color(40_000), (201, 39, 187))
        self.assertNotEqual(altitude_color(5_000), altitude_color(10_000))

    def test_aircraft_marker_rotates_nose_to_track(self):
        north = aircraft_marker_points(100, 100, 0)
        east = aircraft_marker_points(100, 100, 90)
        self.assertEqual(north[0], (100, 91))
        self.assertEqual(east[0], (109, 100))
        self.assertGreater(max(point[0] for point in north) - min(point[0] for point in north), 10)

    def test_selected_aircraft_renders_enriched_route(self):
        config = RadarConfig(latitude=51.47, longitude=-0.46, range_km=25)
        store = AircraftStore(config.latitude, config.longitude, 600, 90)
        store.update(
            {
                "ac": [
                    {
                        "hex": "4081bb",
                        "flight": "BAW577",
                        "r": "G-TNEF",
                        "t": "A21N",
                        "desc": "AIRBUS A321NEO",
                        "lat": 51.48,
                        "lon": -0.53,
                        "alt_baro": 425,
                        "gs": 119,
                    }
                ]
            }
        )
        store.set_route("BAW577", "LIN", "LHR", "Milan", "London")
        renderer = RadarRenderer(config)
        image = renderer.render(store.snapshot(config.range_km), store, monotonic_now=0)
        route_area = image.crop((8, 412, 225, 432))
        route_pixels = {
            route_area.getpixel((x, y))
            for y in range(route_area.height)
            for x in range(route_area.width)
        }
        self.assertIn((95, 220, 185), route_pixels)

    def test_wifi_menu_opens_keyboard_for_secured_network(self):
        renderer = RadarRenderer(RadarConfig())
        renderer.wifi_snapshot = WifiSnapshot(
            available=True,
            connected=False,
            connected_ssid="",
            busy=False,
            status="Not connected",
            error="",
            networks=(WifiNetwork("Test WiFi", 80, "WPA2"),),
        )
        renderer.menu_open = True
        renderer.menu_page = "wifi"
        self.assertEqual(renderer.handle_touch(100, 160), "wifi_password")
        self.assertEqual(renderer.menu_page, "keyboard")
        self.assertEqual(renderer.handle_touch(15, 195), "keyboard")
        self.assertEqual(renderer.wifi_password, "q")
        self.assertEqual(renderer.handle_touch(245, 448), "wifi_connect")
        network, password = renderer.take_wifi_request()
        self.assertEqual(network.ssid, "Test WiFi")
        self.assertEqual(password, "q")

    def test_location_menu_validates_and_updates_radar_centre(self):
        config = RadarConfig()
        renderer = RadarRenderer(config)
        renderer.menu_open = True
        self.assertEqual(renderer.handle_touch(80, 360), "location_edit")
        renderer.location_latitude = "91"
        renderer.location_longitude = "-4.5"
        self.assertEqual(renderer.handle_touch(245, 448), "location_edit")
        self.assertIn("Latitude", renderer.location_error)
        renderer.location_latitude = "36.681234"
        self.assertEqual(renderer.handle_touch(245, 448), "location")
        self.assertAlmostEqual(config.latitude, 36.681234)
        self.assertAlmostEqual(config.longitude, -4.5)
