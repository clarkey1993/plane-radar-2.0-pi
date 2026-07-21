# Plane Radar 2.0 — Raspberry Pi

Plane Radar 2.0 is a touchscreen live-aircraft display for a Raspberry Pi Zero
2 W and the Waveshare 3.5-inch ST7796S/FT6336U display.

Features include:

- animated radar sweep and persistent aircraft trails
- altitude-coloured, direction-aware aircraft silhouettes
- selectable aircraft with live flight information
- touchscreen range, trail, sweep, brightness, Wi-Fi, and location settings
- verified over-the-air updates from the public
  [`plane-radar-ota`](https://github.com/clarkey1993/plane-radar-ota) repository

The complete wiring table and installation guide are in
[`pi/README.md`](pi/README.md).

## Quick installation

```bash
sudo apt update
sudo apt install -y git network-manager python3-numpy python3-pil python3-spidev python3-gpiozero python3-smbus
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_i2c 0
git clone https://github.com/clarkey1993/plane-radar-2.0-pi.git
cd plane-radar-2.0-pi/pi
sudo ./install.sh
```

After installation, use `MENU → WI-FI` and `MENU → LOCATION` on the display.
Fresh installations contain no Wi-Fi credentials or personal coordinates.

## OTA releases

This repository contains source code only. Release assets use `pi-v*` tags in
the shared OTA repository, while ESP firmware uses `firmware-v*`; the two
device families cannot install each other's assets.

The current Airplanes.live source is documented for non-commercial use.
Commercial distribution requires permission or a suitably licensed/local data
provider.
