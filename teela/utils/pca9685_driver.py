#!/usr/bin/env python3
"""
Direct I2C PCA9685 Servo Driver for Teela
==========================================
Bypasses broken Jetson.GPIO / Adafruit ServoKit.
Talks straight to the PCA9685 over smbus2 on /dev/i2c-7.

Usage:
    from utils.pca9685_driver import PCA9685
    pca = PCA9685(bus=7, address=0x40)
    pca.set_servo_angle(channel=0, angle=90)   # 0-180°
"""
from __future__ import annotations

import logging
import time
from typing import Optional

try:
    from smbus2 import SMBus
except ImportError:
    raise ImportError("smbus2 required. Install: pip3 install --user smbus2")

logger = logging.getLogger("PCA9685")


# ── PCA9685 Register Map ────────────────────
_REG_MODE1 = 0x00
_REG_MODE2 = 0x01
_REG_SUBADR1 = 0x02
_REG_SUBADR2 = 0x03
_REG_SUBADR3 = 0x04
_REG_ALLCALLADR = 0x05
_REG_LED0_ON_L = 0x06
_REG_LED0_ON_H = 0x07
_REG_LED0_OFF_L = 0x08
_REG_LED0_OFF_H = 0x09
_REG_ALL_LED_ON_L = 0xFA
_REG_ALL_LED_ON_H = 0xFB
_REG_ALL_LED_OFF_L = 0xFC
_REG_ALL_LED_OFF_H = 0xFD
_REG_PRE_SCALE = 0xFE
_REG_TEST = 0xFF

# Mode1 bits
_MODE1_RESTART = 0x80
_MODE1_SLEEP = 0x10
_MODE1_ALLCALL = 0x01
_MODE1_AI = 0x20

# Mode2 bits
_MODE2_OUTDRV = 0x04

_OSC_FREQ = 25000000.0  # 25 MHz


class PCA9685:
    """Raw I2C driver for PCA9685 16-channel PWM servo controller."""

    def __init__(self, bus: int = 7, address: int = 0x40, freq: float = 50.0):
        self._bus_num = bus
        self._address = address
        self._freq = freq
        self._bus: Optional[SMBus] = None
        self._open()
        self._init()
        logger.info(f"PCA9685 initialized: bus={bus} addr=0x{address:02X} freq={freq}Hz")

    def _open(self) -> None:
        dev_path = f"/dev/i2c-{self._bus_num}"
        try:
            self._bus = SMBus(dev_path)
            logger.debug(f"Opened {dev_path}")
        except FileNotFoundError:
            raise RuntimeError(f"I2C bus {self._bus_num} not found.")
        except PermissionError:
            raise RuntimeError(f"Permission denied on {dev_path}. Are you in 'i2c' group?")

    def _write_byte(self, reg: int, val: int) -> None:
        self._bus.write_byte_data(self._address, reg, val)

    def _read_byte(self, reg: int) -> int:
        return self._bus.read_byte_data(self._address, reg)

    def _init(self) -> None:
        mode1 = self._read_byte(_REG_MODE1)
        new_mode1 = (mode1 & ~_MODE1_RESTART) | _MODE1_SLEEP
        self._write_byte(_REG_MODE1, new_mode1 & ~_MODE1_ALLCALL)
        time.sleep(0.001)

        prescale_val = round((_OSC_FREQ / (4096.0 * self._freq)) - 1.0)
        prescale_val = max(0x03, min(0xFF, prescale_val))
        logger.debug(f"Prescale: {prescale_val}")

        self._write_byte(_REG_PRE_SCALE, prescale_val)
        time.sleep(0.001)

        self._write_byte(_REG_MODE1, _MODE1_AI | _MODE1_ALLCALL)
        time.sleep(0.001)

        self._write_byte(_REG_MODE2, _MODE2_OUTDRV)
        time.sleep(0.001)

        self._write_byte(_REG_ALL_LED_ON_L, 0x00)
        self._write_byte(_REG_ALL_LED_ON_H, 0x00)
        self._write_byte(_REG_ALL_LED_OFF_L, 0x00)
        self._write_byte(_REG_ALL_LED_OFF_H, 0x10)

    def _calc_pwm_counts(self, angle: float) -> tuple[int, int]:
        """Map 0-180° to PCA9685 counts."""
        pulse_ms = 1.0 + (angle / 180.0) * 1.0
        off_count = int(pulse_ms * 4096.0 / 20.0)
        off_count = max(0, min(4095, off_count))
        return 0, off_count

    def set_pwm(self, channel: int, on: int, off: int) -> None:
        if not 0 <= channel <= 15:
            raise ValueError(f"Channel {channel} out of range 0-15")
        base = _REG_LED0_ON_L + 4 * channel
        self._write_byte(base + 0, on & 0xFF)
        self._write_byte(base + 1, (on >> 8) & 0x0F)
        self._write_byte(base + 2, off & 0xFF)
        self._write_byte(base + 3, (off >> 8) & 0x0F)

    def set_servo_angle(self, channel: int, angle: float) -> None:
        angle = max(0.0, min(180.0, angle))
        on_val, off_val = self._calc_pwm_counts(angle)
        self.set_pwm(channel, on_val, off_val)
        logger.debug(f"CH{channel} → {angle:.1f}°")

    def set_all_off(self) -> None:
        self._write_byte(_REG_ALL_LED_ON_L, 0x00)
        self._write_byte(_REG_ALL_LED_ON_H, 0x00)
        self._write_byte(_REG_ALL_LED_OFF_L, 0x00)
        self._write_byte(_REG_ALL_LED_OFF_H, 0x10)

    def deinit(self) -> None:
        self.set_all_off()
        if self._bus:
            self._bus.close()
            self._bus = None
            logger.info("PCA9685 deinitialized.")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.deinit()


# ── Standalone test ─────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("PCA9685 Direct I2C Driver Test")
    print("Sweep servo on channel 0...")
    try:
        with PCA9685(bus=7, address=0x40, freq=50) as pca:
            for angle in range(0, 181, 15):
                pca.set_servo_angle(0, angle)
                time.sleep(0.3)
            for angle in range(180, -1, -15):
                pca.set_servo_angle(0, angle)
                time.sleep(0.3)
            print("Test complete!")
    except Exception as e:
        print(f"Error: {e}")
