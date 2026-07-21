from __future__ import annotations

import time
from typing import Any

import numpy as np
from PIL import Image


class ST7796Display:
    width = 320
    height = 480

    def __init__(self, spi_hz: int = 40_000_000, brightness: float = 1.0, mirror_x: bool = True):
        import spidev
        from gpiozero import DigitalOutputDevice, PWMOutputDevice

        self.reset_pin = DigitalOutputDevice(27, initial_value=True)
        self.dc_pin = DigitalOutputDevice(25, initial_value=True)
        self.backlight = PWMOutputDevice(18, frequency=1000, initial_value=max(0.0, min(1.0, brightness)))
        self.spi = spidev.SpiDev(0, 0)
        self.spi.max_speed_hz = spi_hz
        self.spi.mode = 0
        self.madctl = 0x48 if mirror_x else 0x08
        self._initialize()

    def _command(self, value: int) -> None:
        self.dc_pin.off()
        self.spi.writebytes([value])

    def _data(self, *values: int) -> None:
        self.dc_pin.on()
        self.spi.writebytes(list(values))

    def _initialize(self) -> None:
        self.reset_pin.on()
        time.sleep(0.02)
        self.reset_pin.off()
        time.sleep(0.02)
        self.reset_pin.on()
        time.sleep(0.12)
        sequence: list[tuple[int, tuple[int, ...]]] = [
            (0x11, ()), (0x36, (self.madctl,)), (0x3A, (0x05,)),
            (0xF0, (0xC3,)), (0xF0, (0x96,)), (0xB4, (0x01,)),
            (0xB7, (0xC6,)), (0xC0, (0x80, 0x45)), (0xC1, (0x13,)),
            (0xC2, (0xA7,)), (0xC5, (0x0A,)),
            (0xE8, (0x40, 0x8A, 0x00, 0x00, 0x29, 0x19, 0xA5, 0x33)),
            (0xE0, (0xD0, 0x08, 0x0F, 0x06, 0x06, 0x33, 0x30, 0x33, 0x47, 0x17, 0x13, 0x13, 0x2B, 0x31)),
            (0xE1, (0xD0, 0x0A, 0x11, 0x0B, 0x09, 0x07, 0x2F, 0x33, 0x47, 0x38, 0x15, 0x16, 0x2C, 0x32)),
            (0xF0, (0x3C,)), (0xF0, (0x69,)), (0x21, ()), (0x29, ()),
        ]
        self._command(0x11)
        time.sleep(0.12)
        for command, data in sequence[1:]:
            self._command(command)
            if data:
                self._data(*data)
        time.sleep(0.05)

    def _window(self) -> None:
        self._command(0x2A)
        self._data(0, 0, 1, 0x3F)
        self._command(0x2B)
        self._data(0, 0, 1, 0xDF)
        self._command(0x2C)

    def show(self, image: Image.Image) -> None:
        rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
        red = (rgb[:, :, 0].astype(np.uint16) >> 3) << 11
        green = (rgb[:, :, 1].astype(np.uint16) >> 2) << 5
        blue = rgb[:, :, 2].astype(np.uint16) >> 3
        rgb565 = red | green | blue
        payload = np.empty((self.height, self.width, 2), dtype=np.uint8)
        payload[:, :, 0] = rgb565 >> 8
        payload[:, :, 1] = rgb565 & 0xFF
        raw = payload.tobytes()
        self._window()
        self.dc_pin.on()
        for offset in range(0, len(raw), 4096):
            self.spi.writebytes2(raw[offset : offset + 4096])

    def close(self) -> None:
        self.spi.close()
        self.reset_pin.close()
        self.dc_pin.close()
        self.backlight.close()

    def set_brightness(self, brightness: float) -> None:
        self.backlight.value = max(0.05, min(1.0, brightness))


class FT6336Touch:
    def __init__(self, mirror_x: bool = False):
        from gpiozero import DigitalInputDevice, DigitalOutputDevice
        try:
            from smbus2 import SMBus
        except ImportError:
            from smbus import SMBus  # type: ignore[no-redef]

        self.reset_pin = DigitalOutputDevice(17, initial_value=False)
        self.interrupt_pin = DigitalInputDevice(4, pull_up=True)
        self.reset_pin.off()
        time.sleep(0.02)
        self.reset_pin.on()
        time.sleep(0.12)
        self.bus: Any = SMBus(1)
        self.address = 0x38
        self._pressed = False
        self.mirror_x = mirror_x

    def read(self) -> tuple[int, int] | None:
        try:
            data = self.bus.read_i2c_block_data(self.address, 0x00, 7)
        except OSError:
            return None
        count = data[2] & 0x0F
        if count == 0:
            self._pressed = False
            return None
        if count > 2:
            return None
        x = ((data[3] & 0x0F) << 8) | data[4]
        y = ((data[5] & 0x0F) << 8) | data[6]
        if self._pressed:
            return None
        self._pressed = True
        x = max(0, min(319, x))
        y = max(0, min(479, y))
        if self.mirror_x:
            x = 319 - x
        return x, y

    def close(self) -> None:
        self.bus.close()
        self.interrupt_pin.close()
        self.reset_pin.close()
