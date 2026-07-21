# Plane Radar 2.0 — Raspberry Pi

This application targets a Raspberry Pi Zero 2 W and the Waveshare 3.5-inch
320×480 capacitive display with an ST7796S LCD and FT6336U touch controller.

## Wiring

Use the Waveshare 15-pin connector and BCM GPIO assignments below. For a
Raspberry Pi, power `VCC` from 3.3 V so the display and Pi SPI logic levels
match. Leave the display's separate `3V3` and `SD_CS` pins disconnected.

| Display | BCM GPIO | Pi physical pin |
|---|---:|---:|
| VCC | 3.3 V | 1 |
| GND | Ground | 6 |
| MISO | 9 | 21 |
| MOSI | 10 | 19 |
| SCLK | 11 | 23 |
| LCD_CS | 8 | 24 |
| LCD_DC | 25 | 22 |
| LCD_RST | 27 | 13 |
| LCD_BL | 18 | 12 |
| TP_SDA | 2 | 3 |
| TP_SCL | 3 | 5 |
| TP_INT | 4 | 7 |
| TP_RST | 17 | 11 |

## Install on a fresh Pi

Use Raspberry Pi OS Lite (64-bit), enable SSH during imaging, then install the
runtime packages and enable SPI/I2C:

```bash
sudo apt update
sudo apt install -y git network-manager python3-numpy python3-pil python3-spidev python3-gpiozero python3-smbus
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_i2c 0
git clone https://github.com/clarkey1993/plane-radar-2.0-pi.git
cd plane-radar-2.0-pi/pi
sudo ./install.sh
```

The service starts automatically. On the display, open `MENU → WI-FI` to
connect, then `MENU → LOCATION` to set the radar centre. A fresh installation
uses neutral `0,0` coordinates and contains no personal Wi-Fi credentials.

`display_mirror_x` defaults to `true` for the photographed Waveshare panel
revision. Set it to `false` if a later panel revision uses normal column order.
`touch_mirror_x` defaults to `false` because correcting the LCD controller's
column order does not change the FT6336U's physical coordinate order.
Touch release/debounce uses the FT6336U touch-count register rather than its
interrupt level because some panel revisions hold the interrupt line low.

The app always fetches and retains aircraft out to `tracking_range_km` (100 km
by default). The selected radar range only filters the display, so switching
ranges reveals already accumulated aircraft trails immediately.

Aircraft markers and trails use a continuous ADS-B altitude colour scale from
orange at low altitude through green, cyan, blue, and magenta at 40,000 ft.
Ground/0-ft aircraft, towers, vehicles, and other surface targets are filtered
from the radar.

## Touchscreen Wi-Fi setup

Open `MENU`, tap `WI-FI`, then `SCAN NETWORKS`. Select an open network to
connect directly, or select a secured network to enter its password with the
on-screen keyboard. NetworkManager stores the resulting system connection;
the radar app neither stores nor logs the password. Aircraft fetching remains
paused and the radar stays empty whenever Wi-Fi is disconnected.

The first release supports open, WPA2-Personal, and WPA3-Personal networks.
Enterprise authentication and captive-portal sign-in require a separate setup
flow. Connecting the development unit to a different network can naturally
end an active SSH session because the Pi's address may change.

Open `MENU` and tap `LOCATION` to enter the latitude and longitude of the
radar's centre point with the numeric touchscreen keypad. Coordinates are
validated before saving. Changing the centre clears positions calculated from
the old location and immediately requests fresh aircraft data—no reboot is
needed.

The public Airplanes.live API documents its feed as non-commercial. Commercial
distribution requires permission or a suitably licensed/local data provider;
the fetcher is kept separate from the display and aircraft store so another
provider can be substituted without redesigning the UI.

To reinstall after making local changes:

```bash
sudo ./install.sh
```

## Automatic updates

Distributed devices check the public `clarkey1993/plane-radar-ota` repository
at boot and every 12 hours.
Only releases whose tags start with `pi-v` are considered. The updater requires
the `plane-radar-pi.tar.gz` and `pi-manifest.json` assets, verifies the declared
size and SHA-256, validates archive paths, compiles the staged package, then
atomically switches `/opt/plane-radar/current`. The previous two versions are
retained for recovery. A failed check leaves the installed version untouched.

Run the `Build and publish Raspberry Pi release` workflow in the
`plane-radar-ota` repository to publish an update. Increase
`plane_radar.__version__` here before publishing.
