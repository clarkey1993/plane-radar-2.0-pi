from __future__ import annotations

import math
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import RadarConfig
from .models import Aircraft, AircraftStore, bearing_degrees, haversine_km
from .wifi import WifiNetwork, WifiSnapshot


WIDTH, HEIGHT = 320, 480
RADAR_CENTER = (160, 205)
RADAR_RADIUS = 145
CARD_TOP = 360

# Altitude palette used by common ADS-B radar maps. Values between stops are
# interpolated so climbing aircraft change colour smoothly rather than jumping
# between a few broad bands. Ground is deliberately distinct and high contrast.
ALTITUDE_COLORS = (
    (1, (244, 91, 35)),
    (500, (221, 98, 36)),
    (1_000, (202, 123, 37)),
    (2_000, (210, 161, 35)),
    (4_000, (164, 160, 34)),
    (6_000, (91, 145, 30)),
    (8_000, (52, 181, 35)),
    (10_000, (29, 139, 47)),
    (20_000, (53, 173, 151)),
    (30_000, (69, 113, 171)),
    (35_000, (104, 78, 174)),
    (40_000, (201, 39, 187)),
)

# Top-down aircraft silhouette in local (sideways, forward) coordinates.
# The first point is the nose; the wider middle points form the wings and the
# small rear points form the horizontal stabiliser.
AIRCRAFT_SHAPE = (
    (0.0, 9.0),
    (1.5, 5.0),
    (2.0, 1.0),
    (7.0, -2.0),
    (7.0, -3.5),
    (2.0, -2.0),
    (1.5, -6.0),
    (4.0, -8.0),
    (4.0, -9.5),
    (0.0, -8.5),
    (-4.0, -9.5),
    (-4.0, -8.0),
    (-1.5, -6.0),
    (-2.0, -2.0),
    (-7.0, -3.5),
    (-7.0, -2.0),
    (-2.0, 1.0),
    (-1.5, 5.0),
)


def altitude_color(altitude_ft: int) -> tuple[int, int, int]:
    if altitude_ft <= 0:
        return 255, 255, 255
    for index in range(1, len(ALTITUDE_COLORS)):
        lower_altitude, lower_color = ALTITUDE_COLORS[index - 1]
        upper_altitude, upper_color = ALTITUDE_COLORS[index]
        if altitude_ft <= upper_altitude:
            span = upper_altitude - lower_altitude
            fraction = (altitude_ft - lower_altitude) / span
            return tuple(
                int(round(lower + (upper - lower) * fraction))
                for lower, upper in zip(lower_color, upper_color)
            )
    return ALTITUDE_COLORS[-1][1]


def aircraft_marker_points(x: int, y: int, track_deg: float) -> tuple[tuple[int, int], ...]:
    angle = math.radians(track_deg)
    side_x, side_y = math.cos(angle), math.sin(angle)
    forward_x, forward_y = math.sin(angle), -math.cos(angle)
    return tuple(
        (
            int(round(x + sideways * side_x + forward * forward_x)),
            int(round(y + sideways * side_y + forward * forward_y)),
        )
        for sideways, forward in AIRCRAFT_SHAPE
    )


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


class RadarRenderer:
    def __init__(self, config: RadarConfig):
        self.config = config
        self.small = _font(10)
        self.body = _font(13)
        self.body_bold = _font(13, True)
        self.title = _font(18, True)
        self.large = _font(22, True)
        self.hero = _font(25, True)
        self.metric = _font(15, True)
        self.selected_icao: str | None = None
        self.hit_targets: dict[str, tuple[int, int]] = {}
        self.menu_open = False
        self.menu_page = "settings"
        self.wifi_page = 0
        self.wifi_snapshot: WifiSnapshot | None = None
        self.wifi_network: WifiNetwork | None = None
        self.wifi_password = ""
        self.keyboard_shift = False
        self.keyboard_symbols = False
        self.password_visible = False
        self._wifi_request: tuple[WifiNetwork, str] | None = None
        self.location_field = "latitude"
        self.location_latitude = f"{config.latitude:.6f}"
        self.location_longitude = f"{config.longitude:.6f}"
        self.location_error = ""

    @staticmethod
    def _point(distance: float, bearing: float, range_km: float) -> tuple[int, int]:
        radius = min(1.0, distance / range_km) * (RADAR_RADIUS - 5)
        angle = math.radians(bearing)
        return int(RADAR_CENTER[0] + math.sin(angle) * radius), int(RADAR_CENTER[1] - math.cos(angle) * radius)

    def _trail_point(self, plane: Aircraft, latitude: float, longitude: float) -> tuple[int, int]:
        distance = haversine_km(self.config.latitude, self.config.longitude, latitude, longitude)
        bearing = bearing_degrees(self.config.latitude, self.config.longitude, latitude, longitude)
        return self._point(distance, bearing, self.config.range_km)

    @staticmethod
    def _angular_distance(a: float, b: float) -> float:
        return abs((a - b + 180.0) % 360.0 - 180.0)

    def _draw_sweep(self, image: Image.Image, sweep: float) -> Image.Image:
        # Build a continuous phosphor wedge in short angular slices. Alpha rises
        # non-linearly toward the live beam, avoiding the old comb of radial lines.
        base = image.convert("RGBA")
        persistence = Image.new("RGBA", image.size, (0, 0, 0, 0))
        persistence_draw = ImageDraw.Draw(persistence)
        radar_box = (
            RADAR_CENTER[0] - RADAR_RADIUS,
            RADAR_CENTER[1] - RADAR_RADIUS,
            RADAR_CENTER[0] + RADAR_RADIUS,
            RADAR_CENTER[1] + RADAR_RADIUS,
        )
        tail_degrees = 46
        slice_degrees = 2
        for offset in range(tail_degrees, 0, -slice_degrees):
            proximity = 1.0 - (offset / tail_degrees)
            alpha = int(3 + 49 * proximity * proximity)
            start = sweep - offset - 90
            end = sweep - offset + slice_degrees - 90
            persistence_draw.pieslice(radar_box, start=start, end=end, fill=(20, 225, 145, alpha))
        base = Image.alpha_composite(base, persistence)

        # A separate blurred layer gives the leading edge a restrained halo.
        beam_glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(beam_glow)
        angle = math.radians(sweep)
        end = (
            int(RADAR_CENTER[0] + math.sin(angle) * RADAR_RADIUS),
            int(RADAR_CENTER[1] - math.cos(angle) * RADAR_RADIUS),
        )
        glow_draw.line((RADAR_CENTER, end), fill=(65, 255, 170, 150), width=7)
        beam_glow = beam_glow.filter(ImageFilter.GaussianBlur(radius=3.0))
        base = Image.alpha_composite(base, beam_glow)
        beam_draw = ImageDraw.Draw(base)
        beam_draw.line((RADAR_CENTER, end), fill=(155, 255, 210, 255), width=2)
        beam_draw.ellipse((end[0] - 2, end[1] - 2, end[0] + 2, end[1] + 2), fill=(205, 255, 230, 255))
        return base.convert("RGB")

    def _draw_plane(self, draw: ImageDraw.ImageDraw, plane: Aircraft, sweep: float, now: float) -> None:
        x, y = self._point(plane.distance_km, plane.bearing_deg, self.config.range_km)
        self.hit_targets[plane.icao] = (x, y)
        selected = plane.icao == self.selected_icao
        plane_color = altitude_color(plane.altitude_ft)
        samples = list(plane.history)
        if len(samples) >= 2:
            points = [self._trail_point(plane, sample.latitude, sample.longitude) for sample in samples]
            for index in range(1, len(points)):
                fraction = index / max(1, len(points) - 1)
                intensity = (0.18 + 0.62 * fraction) if not selected else (0.3 + 0.7 * fraction)
                color = tuple(int(channel * intensity) for channel in plane_color)
                draw.line((points[index - 1], points[index]), fill=color, width=2 if selected else 1)
        elif plane.speed_kt:
            # A short inferred vector provides direction until two real samples exist.
            tail_length = min(18, 7 + plane.speed_kt // 60)
            angle = math.radians(plane.track_deg)
            tail = (int(x - math.sin(angle) * tail_length), int(y + math.cos(angle) * tail_length))
            draw.line((tail, (x, y)), fill=tuple(int(channel * 0.42) for channel in plane_color), width=1)

        glow = self._angular_distance(sweep, plane.bearing_deg) < 7
        if glow or selected:
            radius = 12 if selected else 10
            glow_color = tuple(max(35, int(channel * 0.7)) for channel in plane_color)
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=glow_color, width=2)
        marker_color = plane_color
        if plane.emergency and plane.emergency != "none":
            marker_color = (255, 70, 60)
        marker_points = aircraft_marker_points(x, y, plane.track_deg)
        draw.polygon(marker_points, fill=marker_color, outline=(1, 7, 10), width=1)
        label = plane.label[:8]
        label_x = max(3, min(WIDTH - 58, x + 10))
        label_y = max(49, min(CARD_TOP - 14, y - 7))
        draw.rounded_rectangle((label_x - 2, label_y - 1, label_x + 53, label_y + 12), radius=2, fill=(2, 12, 16))
        draw.text((label_x, label_y), label, font=self.small, fill=marker_color)

    def _selected(self, planes: list[Aircraft]) -> Aircraft | None:
        if self.selected_icao:
            for plane in planes:
                if plane.icao == self.selected_icao:
                    return plane
        return planes[0] if planes else None

    def _choice_button(
        self,
        draw: ImageDraw.ImageDraw,
        box: tuple[int, int, int, int],
        label: str,
        selected: bool,
    ) -> None:
        fill = (12, 70, 65) if selected else (3, 23, 28)
        outline = (80, 225, 175) if selected else (25, 100, 95)
        draw.rounded_rectangle(box, radius=5, fill=fill, outline=outline, width=1)
        center_x = (box[0] + box[2]) // 2
        center_y = (box[1] + box[3]) // 2 - 1
        draw.text((center_x, center_y), label, font=self.body_bold, anchor="mm", fill=(190, 255, 225) if selected else (105, 170, 170))

    def _draw_menu(self, draw: ImageDraw.ImageDraw) -> None:
        draw.rectangle((0, 43, WIDTH, HEIGHT), fill=(2, 12, 17))
        draw.text((12, 50), "SETTINGS", font=self.large, fill=(115, 255, 188))
        draw.text((12, 81), "RADAR RANGE", font=self.small, fill=(80, 160, 165))
        for index, value in enumerate((10, 25, 50, 100)):
            left = 12 + index * 76
            self._choice_button(draw, (left, 95, left + 66, 128), f"{value}", self.config.range_km == value)

        draw.text((12, 145), "TRAIL HISTORY", font=self.small, fill=(80, 160, 165))
        for index, value in enumerate((3, 6, 12)):
            left = 12 + index * 101
            self._choice_button(draw, (left, 159, left + 91, 192), f"{value} MIN", self.config.history_minutes == value)

        draw.text((12, 209), "SWEEP TIME", font=self.small, fill=(80, 160, 165))
        for index, value in enumerate((5, 8, 12)):
            left = 12 + index * 101
            self._choice_button(draw, (left, 223, left + 91, 256), f"{value} SEC", self.config.sweep_seconds == value)

        draw.text((12, 273), "BRIGHTNESS", font=self.small, fill=(80, 160, 165))
        for index, value in enumerate((0.4, 0.7, 1.0)):
            left = 12 + index * 101
            self._choice_button(draw, (left, 287, left + 91, 320), f"{int(value * 100)}%", abs(self.config.brightness - value) < 0.01)

        draw.rounded_rectangle((12, 342, 154, 389), radius=7, fill=(4, 27, 31), outline=(45, 150, 135), width=2)
        draw.text((83, 359), "LOCATION", font=self.body_bold, anchor="mm", fill=(125, 245, 200))
        draw.text((83, 376), "RADAR CENTRE", font=self.small, anchor="mm", fill=(70, 145, 145))

        connected = bool(self.wifi_snapshot and self.wifi_snapshot.connected)
        draw.rounded_rectangle((166, 342, 308, 389), radius=7, fill=(4, 27, 31), outline=(45, 150, 135), width=2)
        draw.text((237, 359), "WI-FI", font=self.body_bold, anchor="mm", fill=(125, 245, 200) if connected else (255, 175, 80))
        wifi_detail = self.wifi_snapshot.connected_ssid if connected and self.wifi_snapshot else "NOT CONNECTED"
        draw.text((237, 376), wifi_detail[:18], font=self.small, anchor="mm", fill=(70, 145, 145))

        draw.rounded_rectangle((80, 424, 240, 468), radius=7, outline=(70, 210, 165), width=2)
        draw.text((160, 446), "CLOSE", font=self.body_bold, anchor="mm", fill=(135, 255, 205))

    @staticmethod
    def _signal_bars(draw: ImageDraw.ImageDraw, x: int, y: int, signal: int) -> None:
        active = max(1, min(4, math.ceil(signal / 25)))
        for index in range(4):
            height = 3 + index * 3
            color = (90, 230, 175) if index < active else (25, 65, 65)
            draw.rectangle((x + index * 5, y + 13 - height, x + index * 5 + 3, y + 13), fill=color)

    def _draw_wifi(self, draw: ImageDraw.ImageDraw) -> None:
        draw.rectangle((0, 43, WIDTH, HEIGHT), fill=(2, 12, 17))
        draw.text((12, 51), "WI-FI", font=self.large, fill=(115, 255, 188))
        snapshot = self.wifi_snapshot
        if snapshot is None:
            status, status_color = "Checking Wi-Fi...", (255, 180, 80)
            networks: tuple[WifiNetwork, ...] = ()
        else:
            status = snapshot.status
            status_color = (100, 235, 175) if snapshot.connected else (255, 180, 80)
            networks = snapshot.networks
        draw.text((12, 77), status[:42], font=self.small, fill=status_color)
        if snapshot and snapshot.error:
            draw.text((12, 91), snapshot.error[:47], font=self.small, fill=(255, 105, 90))

        draw.rounded_rectangle((12, 105, 150, 134), radius=5, outline=(35, 125, 115))
        draw.text((81, 119), "BACK", font=self.small, anchor="mm", fill=(110, 220, 190))
        scan_fill = (12, 70, 65) if snapshot and snapshot.busy else (4, 27, 31)
        draw.rounded_rectangle((170, 105, 308, 134), radius=5, fill=scan_fill, outline=(45, 150, 135))
        draw.text((239, 119), "SCANNING..." if snapshot and snapshot.busy else "SCAN NETWORKS", font=self.small, anchor="mm", fill=(125, 245, 200))

        page_size = 6
        pages = max(1, math.ceil(len(networks) / page_size))
        self.wifi_page = min(self.wifi_page, pages - 1)
        start = self.wifi_page * page_size
        for row_index, network in enumerate(networks[start:start + page_size]):
            top = 143 + row_index * 44
            fill = (9, 48, 47) if network.connected else (3, 23, 28)
            outline = (65, 190, 150) if network.connected else (18, 75, 73)
            draw.rounded_rectangle((8, top, 312, top + 38), radius=5, fill=fill, outline=outline)
            draw.text((17, top + 8), network.ssid[:25], font=self.body_bold, fill=(205, 245, 230))
            detail = "CONNECTED" if network.connected else (network.security or "OPEN")
            draw.text((18, top + 24), detail[:24], font=self.small, fill=(75, 145, 150))
            self._signal_bars(draw, 282, top + 11, network.signal)

        if not networks and not (snapshot and snapshot.busy):
            draw.text((160, 238), "Tap SCAN NETWORKS", font=self.body, anchor="mm", fill=(105, 160, 160))
        if pages > 1:
            draw.rounded_rectangle((8, 420, 96, 468), radius=5, outline=(30, 105, 100))
            draw.text((52, 444), "PREV", font=self.small, anchor="mm", fill=(100, 205, 180))
            draw.text((160, 444), f"{self.wifi_page + 1}/{pages}", font=self.small, anchor="mm", fill=(85, 145, 150))
            draw.rounded_rectangle((224, 420, 312, 468), radius=5, outline=(30, 105, 100))
            draw.text((268, 444), "NEXT", font=self.small, anchor="mm", fill=(100, 205, 180))

    def _keyboard_keys(self) -> list[tuple[str, str, tuple[int, int, int, int]]]:
        rows = (
            ("1234567890", 5, 139, 30),
            (("!@#$%^&*()" if self.keyboard_symbols else "qwertyuiop"), 5, 179, 30),
            (("-_+=[]{};" if self.keyboard_symbols else "asdfghjkl"), 20, 219, 30),
            ((".,?/\\:~" if self.keyboard_symbols else "zxcvbnm"), 50, 259, 30),
        )
        keys: list[tuple[str, str, tuple[int, int, int, int]]] = []
        for characters, start_x, top, width in rows:
            for index, character in enumerate(characters):
                value = character.upper() if self.keyboard_shift and character.isalpha() else character
                left = start_x + index * (width + 1)
                keys.append((value, value, (left, top, left + width - 2, top + 33)))
        return keys

    def _draw_keyboard(self, draw: ImageDraw.ImageDraw) -> None:
        draw.rectangle((0, 43, WIDTH, HEIGHT), fill=(2, 12, 17))
        ssid = self.wifi_network.ssid if self.wifi_network else "NETWORK"
        draw.text((10, 51), "WI-FI PASSWORD", font=self.title, fill=(115, 255, 188))
        draw.text((10, 76), ssid[:35], font=self.small, fill=(90, 170, 175))
        draw.rounded_rectangle((8, 94, 238, 128), radius=4, fill=(1, 8, 12), outline=(30, 95, 90))
        password_text = self.wifi_password if self.password_visible else "•" * len(self.wifi_password)
        draw.text((16, 111), password_text[-26:], font=self.body, anchor="lm", fill=(220, 245, 235))
        draw.rounded_rectangle((246, 94, 314, 128), radius=4, outline=(30, 95, 90))
        draw.text((280, 111), "HIDE" if self.password_visible else "SHOW", font=self.small, anchor="mm", fill=(100, 210, 180))

        for label, _value, box in self._keyboard_keys():
            draw.rounded_rectangle(box, radius=3, fill=(5, 29, 34), outline=(22, 75, 78))
            draw.text(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), label, font=self.body_bold, anchor="mm", fill=(190, 230, 220))

        for label, box, selected in (
            ("SHIFT", (7, 302, 98, 338), self.keyboard_shift),
            ("SYM", (105, 302, 203, 338), self.keyboard_symbols),
            ("DEL", (210, 302, 313, 338), False),
        ):
            self._choice_button(draw, box, label, selected)
        draw.rounded_rectangle((57, 345, 263, 380), radius=4, fill=(5, 29, 34), outline=(22, 75, 78))
        draw.text((160, 362), "SPACE", font=self.small, anchor="mm", fill=(170, 220, 210))
        if self.wifi_snapshot and self.wifi_snapshot.error:
            draw.text((160, 400), self.wifi_snapshot.error[:45], font=self.small, anchor="mm", fill=(255, 105, 90))
        draw.rounded_rectangle((8, 426, 145, 470), radius=6, outline=(40, 105, 105))
        draw.text((76, 448), "CANCEL", font=self.body_bold, anchor="mm", fill=(110, 190, 185))
        draw.rounded_rectangle((175, 426, 312, 470), radius=6, fill=(10, 65, 58), outline=(70, 210, 165), width=2)
        draw.text((243, 448), "CONNECT", font=self.body_bold, anchor="mm", fill=(145, 255, 210))

    def _coordinate_keys(self) -> list[tuple[str, tuple[int, int, int, int]]]:
        keys: list[tuple[str, tuple[int, int, int, int]]] = []
        rows = (("1", "2", "3", "DEL"), ("4", "5", "6", "-"), ("7", "8", "9", "."))
        for row_index, row in enumerate(rows):
            top = 205 + row_index * 45
            for column, label in enumerate(row):
                left = 8 + column * 78
                keys.append((label, (left, top, left + 69, top + 37)))
        keys.extend((("0", (8, 340, 154, 378)), ("CLEAR", (166, 340, 312, 378))))
        return keys

    def _draw_location(self, draw: ImageDraw.ImageDraw) -> None:
        draw.rectangle((0, 43, WIDTH, HEIGHT), fill=(2, 12, 17))
        draw.text((10, 51), "RADAR LOCATION", font=self.title, fill=(115, 255, 188))
        draw.text((10, 75), "Set the centre point for distance and bearing", font=self.small, fill=(80, 155, 160))
        for field, label, value, box in (
            ("latitude", "LATITUDE", self.location_latitude, (8, 95, 312, 133)),
            ("longitude", "LONGITUDE", self.location_longitude, (8, 146, 312, 184)),
        ):
            selected = self.location_field == field
            draw.rounded_rectangle(box, radius=5, fill=(7, 39, 42) if selected else (2, 20, 25), outline=(75, 210, 165) if selected else (25, 85, 85), width=2 if selected else 1)
            draw.text((17, box[1] + 7), label, font=self.small, fill=(65, 145, 150))
            draw.text((302, (box[1] + box[3]) // 2 + 5), value or "—", font=self.body_bold, anchor="rm", fill=(215, 245, 235))
        if self.location_error:
            draw.text((160, 193), self.location_error, font=self.small, anchor="mm", fill=(255, 105, 90))

        for label, box in self._coordinate_keys():
            draw.rounded_rectangle(box, radius=4, fill=(5, 29, 34), outline=(22, 75, 78))
            draw.text(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), label, font=self.body_bold, anchor="mm", fill=(190, 230, 220))

        draw.rounded_rectangle((8, 426, 145, 470), radius=6, outline=(40, 105, 105))
        draw.text((76, 448), "CANCEL", font=self.body_bold, anchor="mm", fill=(110, 190, 185))
        draw.rounded_rectangle((175, 426, 312, 470), radius=6, fill=(10, 65, 58), outline=(70, 210, 165), width=2)
        draw.text((243, 448), "SET CENTRE", font=self.body_bold, anchor="mm", fill=(145, 255, 210))

    def render(
        self,
        planes: list[Aircraft],
        store: AircraftStore,
        monotonic_now: float | None = None,
        wifi_snapshot: WifiSnapshot | None = None,
    ) -> Image.Image:
        monotonic_now = monotonic_now if monotonic_now is not None else time.monotonic()
        if wifi_snapshot is not None:
            self.wifi_snapshot = wifi_snapshot
        now = time.time()
        sweep = (monotonic_now / self.config.sweep_seconds * 360.0) % 360.0
        image = Image.new("RGB", (WIDTH, HEIGHT), (1, 7, 10))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, WIDTH, 43), fill=(2, 14, 18))
        draw.text((12, 7), "PLANE RADAR", font=self.title, fill=(115, 255, 188))
        last_update, error = store.status()
        age = int(max(0, now - last_update)) if last_update else 0
        live_color = (80, 235, 155) if last_update and age < 45 and not error else (255, 172, 64)
        draw.ellipse((278, 10, 286, 18), fill=live_color)
        draw.text((290, 7), "LIVE" if not error else "ERR", font=self.small, fill=live_color)
        draw.text((12, 29), f"{self.config.range_km} km", font=self.small, fill=(80, 160, 170))
        draw.text((68, 29), f"{len(planes)} aircraft", font=self.small, fill=(80, 160, 170))
        if last_update:
            draw.text((244, 29), f"data {age}s", font=self.small, fill=(70, 125, 135))

        for ring, label in ((1 / 3, None), (2 / 3, None), (1, f"{self.config.range_km}km")):
            radius = int(RADAR_RADIUS * ring)
            draw.ellipse((RADAR_CENTER[0] - radius, RADAR_CENTER[1] - radius, RADAR_CENTER[0] + radius, RADAR_CENTER[1] + radius), outline=(10, 62, 58), width=1)
            if label:
                draw.text((RADAR_CENTER[0] + 4, RADAR_CENTER[1] - radius + 3), label, font=self.small, fill=(25, 105, 95))
        draw.line((RADAR_CENTER[0] - RADAR_RADIUS, RADAR_CENTER[1], RADAR_CENTER[0] + RADAR_RADIUS, RADAR_CENTER[1]), fill=(7, 42, 43))
        draw.line((RADAR_CENTER[0], RADAR_CENTER[1] - RADAR_RADIUS, RADAR_CENTER[0], RADAR_CENTER[1] + RADAR_RADIUS), fill=(7, 42, 43))
        for text, point in (("N", (156, 48)), ("E", (307, 201)), ("S", (156, 348)), ("W", (3, 201))):
            draw.text(point, text, font=self.small, fill=(40, 145, 125))
        image = self._draw_sweep(image, sweep)
        draw = ImageDraw.Draw(image)
        self.hit_targets = {}
        for plane in reversed(planes):
            self._draw_plane(draw, plane, sweep, now)
        draw.ellipse((156, 201, 164, 209), fill=(105, 255, 180))

        draw.rectangle((0, CARD_TOP, WIDTH, HEIGHT), fill=(3, 16, 21))
        draw.line((0, CARD_TOP, WIDTH, CARD_TOP), fill=(25, 105, 95), width=1)
        selected = self._selected(planes)
        if selected:
            self.selected_icao = selected.icao
            emergency = bool(selected.emergency and selected.emergency != "none")
            if emergency:
                draw.rectangle((0, CARD_TOP, 4, HEIGHT), fill=(255, 70, 60))
            draw.text((10, 362), selected.label[:11], font=self.hero, fill=(255, 245, 240) if emergency else (230, 250, 245))
            type_text = selected.aircraft_type or selected.category or "UNKNOWN"
            type_box_width = max(45, min(78, 16 + len(type_text) * 8))
            draw.rounded_rectangle((310 - type_box_width, 364, 310, 387), radius=5, fill=(8, 45, 50), outline=(40, 130, 130))
            draw.text((310 - type_box_width // 2, 375), type_text[:9], font=self.body_bold, anchor="mm", fill=(90, 225, 225))

            identity_parts = [part for part in (selected.registration, selected.icao.upper()) if part]
            if selected.db_flags & 1:
                identity_parts.append("MIL")
            draw.text((11, 391), "  ·  ".join(identity_parts), font=self.small, fill=(95, 170, 175))
            description = selected.description or selected.source_type.replace("_", " ").upper() or "AIRCRAFT DATA"
            draw.text((11, 405), description[:38], font=self.body, fill=(185, 210, 210))

            trend = "↑" if selected.vertical_rate_fpm > 200 else "↓" if selected.vertical_rate_fpm < -200 else "→"
            altitude_text = "GROUND" if selected.altitude_ft <= 0 else f"{selected.altitude_ft:,} ft {trend}"
            draw.text((11, 422), altitude_text, font=self.metric, fill=altitude_color(selected.altitude_ft))
            draw.text((142, 422), f"{selected.speed_kt} kt", font=self.metric, fill=(115, 220, 255))
            draw.text((229, 422), f"{selected.distance_km:.1f} km", font=self.body_bold, fill=(210, 195, 125))

            age = int(selected.seen_seconds + max(0.0, now - selected.last_seen))
            if emergency:
                footer = f"EMERGENCY {selected.emergency.upper()}  ·  SQK {selected.squawk or '----'}"
                footer_color = (255, 105, 90)
            else:
                rate = f"{selected.vertical_rate_fpm:+,} fpm" if abs(selected.vertical_rate_fpm) > 50 else "level"
                footer = f"{rate}  ·  SQK {selected.squawk or '----'}  ·  {age}s"
                footer_color = (115, 160, 165)
            draw.text((11, 444), footer[:34], font=self.small, fill=footer_color)
            draw.rounded_rectangle((238, 451, 310, 474), radius=5, outline=(30, 125, 115))
            draw.text((274, 456), "MENU", font=self.small, anchor="ma", fill=(95, 220, 185))
        else:
            offline = bool(self.wifi_snapshot and not self.wifi_snapshot.connected)
            draw.text((160, 382), "CONNECT WI-FI" if offline else "SCANNING", font=self.large, anchor="ma", fill=(255, 175, 80) if offline else (90, 205, 165))
            draw.text((160, 418), error[:38] if error else "Waiting for nearby aircraft", font=self.body, anchor="ma", fill=(110, 145, 150))
            draw.rounded_rectangle((238, 451, 310, 474), radius=5, outline=(30, 125, 115))
            draw.text((274, 456), "MENU", font=self.small, anchor="ma", fill=(95, 220, 185))
        if self.menu_open:
            if self.menu_page == "wifi":
                self._draw_wifi(draw)
            elif self.menu_page == "keyboard":
                self._draw_keyboard(draw)
            elif self.menu_page == "location":
                self._draw_location(draw)
            else:
                self._draw_menu(draw)
        return image

    def take_wifi_request(self) -> tuple[WifiNetwork, str] | None:
        request = self._wifi_request
        self._wifi_request = None
        self.wifi_password = ""
        return request

    def _handle_wifi_touch(self, x: int, y: int) -> str | None:
        if 100 <= y <= 138:
            if x <= 160:
                self.menu_page = "settings"
                return "menu"
            self.wifi_page = 0
            return "wifi_scan"
        networks = self.wifi_snapshot.networks if self.wifi_snapshot else ()
        if 140 <= y <= 407:
            row = (y - 143) // 44
            index = self.wifi_page * 6 + row
            if 0 <= row < 6 and index < len(networks):
                network = networks[index]
                if network.connected:
                    return None
                self.wifi_network = network
                if network.secured:
                    self.wifi_password = ""
                    self.keyboard_shift = False
                    self.keyboard_symbols = False
                    self.password_visible = False
                    self.menu_page = "keyboard"
                    return "wifi_password"
                self._wifi_request = (network, "")
                return "wifi_connect"
        pages = max(1, math.ceil(len(networks) / 6))
        if 416 <= y <= 474:
            if x < 115:
                self.wifi_page = max(0, self.wifi_page - 1)
            elif x > 205:
                self.wifi_page = min(pages - 1, self.wifi_page + 1)
        return None

    def _handle_keyboard_touch(self, x: int, y: int) -> str | None:
        if 90 <= y <= 132 and x >= 240:
            self.password_visible = not self.password_visible
            return "keyboard"
        for _label, value, box in self._keyboard_keys():
            if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
                if len(self.wifi_password) < 63:
                    self.wifi_password += value
                if self.keyboard_shift:
                    self.keyboard_shift = False
                return "keyboard"
        if 298 <= y <= 342:
            if x < 102:
                self.keyboard_shift = not self.keyboard_shift
            elif x < 207:
                self.keyboard_symbols = not self.keyboard_symbols
            elif self.wifi_password:
                self.wifi_password = self.wifi_password[:-1]
            return "keyboard"
        if 341 <= y <= 385 and 45 <= x <= 275:
            if len(self.wifi_password) < 63:
                self.wifi_password += " "
            return "keyboard"
        if 420 <= y <= 474:
            if x < 160:
                self.wifi_password = ""
                self.menu_page = "wifi"
                return "wifi_cancel"
            if self.wifi_network:
                self._wifi_request = (self.wifi_network, self.wifi_password)
                self.menu_page = "wifi"
                return "wifi_connect"
        return None

    def _handle_location_touch(self, x: int, y: int) -> str | None:
        if 90 <= y <= 138:
            self.location_field = "latitude"
            self.location_error = ""
            return "location_edit"
        if 140 <= y <= 190:
            self.location_field = "longitude"
            self.location_error = ""
            return "location_edit"
        current = self.location_latitude if self.location_field == "latitude" else self.location_longitude
        for label, box in self._coordinate_keys():
            if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
                if label == "DEL":
                    current = current[:-1]
                elif label == "CLEAR":
                    current = ""
                elif label == "-":
                    current = current[1:] if current.startswith("-") else "-" + current
                elif label == ".":
                    if "." not in current and len(current) < 12:
                        current += "."
                elif len(current) < 12:
                    current += label
                if self.location_field == "latitude":
                    self.location_latitude = current
                else:
                    self.location_longitude = current
                self.location_error = ""
                return "location_edit"
        if 420 <= y <= 474:
            if x < 160:
                self.location_latitude = f"{self.config.latitude:.6f}"
                self.location_longitude = f"{self.config.longitude:.6f}"
                self.location_error = ""
                self.menu_page = "settings"
                return "location_cancel"
            try:
                latitude = float(self.location_latitude)
                longitude = float(self.location_longitude)
            except ValueError:
                self.location_error = "Enter valid decimal coordinates"
                return "location_edit"
            if not -90.0 <= latitude <= 90.0:
                self.location_error = "Latitude must be -90 to 90"
                return "location_edit"
            if not -180.0 <= longitude <= 180.0:
                self.location_error = "Longitude must be -180 to 180"
                return "location_edit"
            self.config.latitude = latitude
            self.config.longitude = longitude
            self.location_error = ""
            self.menu_page = "settings"
            return "location"
        return None

    def handle_touch(self, x: int, y: int) -> str | None:
        if self.menu_open:
            if self.menu_page == "wifi":
                return self._handle_wifi_touch(x, y)
            if self.menu_page == "keyboard":
                return self._handle_keyboard_touch(x, y)
            if self.menu_page == "location":
                return self._handle_location_touch(x, y)
            if 420 <= y <= 474 and 70 <= x <= 250:
                self.menu_open = False
                self.menu_page = "settings"
                return "menu"
            if 90 <= y <= 133:
                for index, value in enumerate((10, 25, 50, 100)):
                    left = 7 + index * 76
                    if left <= x <= left + 76:
                        self.config.range_km = value
                        return "range"
            if 154 <= y <= 197:
                for index, value in enumerate((3, 6, 12)):
                    left = 7 + index * 101
                    if left <= x <= left + 101:
                        self.config.history_minutes = float(value)
                        return "history"
            if 218 <= y <= 261:
                for index, value in enumerate((5, 8, 12)):
                    left = 7 + index * 101
                    if left <= x <= left + 101:
                        self.config.sweep_seconds = float(value)
                        return "sweep"
            if 282 <= y <= 325:
                for index, value in enumerate((0.4, 0.7, 1.0)):
                    left = 7 + index * 101
                    if left <= x <= left + 101:
                        self.config.brightness = value
                        return "brightness"
            if 335 <= y <= 398:
                if x < 160:
                    self.location_latitude = f"{self.config.latitude:.6f}"
                    self.location_longitude = f"{self.config.longitude:.6f}"
                    self.location_field = "latitude"
                    self.location_error = ""
                    self.menu_page = "location"
                    return "location_edit"
                self.menu_page = "wifi"
                self.wifi_page = 0
                return "wifi_scan"
            return None
        if y >= 445 and x >= 225:
            self.menu_open = True
            self.menu_page = "settings"
            return "menu"
        best: tuple[float, str] | None = None
        for icao, (plane_x, plane_y) in self.hit_targets.items():
            distance = math.hypot(x - plane_x, y - plane_y)
            if distance <= 22 and (best is None or distance < best[0]):
                best = (distance, icao)
        if best:
            self.selected_icao = best[1]
            return "aircraft"
        return None
