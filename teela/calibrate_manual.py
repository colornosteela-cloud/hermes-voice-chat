#!/usr/bin/env python3
"""
Teela Manual Pan Calibrator
===========================
Simple step-by-step limit finder. The user watches the servo and
presses a key when the mechanical limit is reached.

Usage:
    python teela/calibrate_manual.py --pan-pin 0 --tilt-pin 1 --invert-pan

Steps:
  1. Press 'a' repeatedly to sweep LEFT one direction
  2. Press SPACE when servo hits the physical limit  → records Pan Min
  3. Press 'a' repeatedly to sweep RIGHT the other direction
  4. Press SPACE when servo hits the physical limit  → records Pan Max
  5. Script returns to center and saves limits
"""
import argparse
import json
import os
import sys
import termios
import time
import tty

sys.path.insert(0, os.path.dirname(__file__))
from utils.pca9685_driver import PCA9685

CONFIG_DIR = os.path.expanduser("~/.config/teela")
CONFIG_PATH = os.path.join(CONFIG_DIR, "calibration.json")


def getch():
    """Read single char from terminal without Enter."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def move_servo(pca, pin, angle, invert):
    cmd = -angle if invert else angle
    pca.set_servo_angle(pin, cmd + 90.0)


def sweep_direction(pca, pin, invert, step, label):
    """
    Incrementally move the servo. User presses 'a' to advance,
    SPACE to record the current angle as the limit.
    """
    print(f"\n{'='*50}")
    print(f"   Sweeping {label}")
    print(f"{'='*50}")
    print(f"   Step size: {abs(step)}°")
    print("   'a' = advance  |  SPACE = record limit  |  ESC = abort\n")

    angle = 0.0
    move_servo(pca, pin, angle, invert)
    time.sleep(0.3)

    print(f"   Starting at {angle:.0f}°")

    while True:
        ch = getch()
        if ch == " ":               # SPACE — record limit
            print(f"\n   ✅ {label} limit recorded: {angle:.1f}°")
            return angle
        if ch == "\x1b" or ch == "\x03":  # ESC / Ctrl-C
            raise KeyboardInterrupt("User aborted")
        if ch == "a" or ch == "A":
            angle += step
            print(f"     → {angle:+.1f}°")
            move_servo(pca, pin, angle, invert)
            time.sleep(0.15)


def main():
    parser = argparse.ArgumentParser(description="Manual servo limit calibrator")
    parser.add_argument("--pan-pin", type=int, default=0)
    parser.add_argument("--tilt-pin", type=int, default=1)
    parser.add_argument("--invert-pan", action="store_true",
                        help="Invert pan direction (same as TeelaMaster)")
    parser.add_argument("--invert-tilt", action="store_true",
                        help="Invert tilt direction")
    parser.add_argument("--step", type=float, default=3.0,
                        help="Degrees per 'a' keypress (default: 3.0)")
    args = parser.parse_args()

    print("\n" + "=" * 50)
    print("   Teela Manual Pan Calibrator")
    print("=" * 50)
    print("\nThis discovers your robot's physical pan limits.")
    print("Watch the servo and press SPACE when it stops turning.\n")

    pca = PCA9685(bus=7, address=0x40, freq=50)
    print("[OK] PCA9685 connected.\n")

    try:
        # --- Step 1: Sweep LEFT ---
        # With --invert-pan, +angle pulses → physical LEFT.
        if args.invert_pan:
            pan_min = sweep_direction(pca, args.pan_pin, args.invert_pan,
                                       step=+args.step, label="PAN LEFT")
        else:
            pan_min = sweep_direction(pca, args.pan_pin, args.invert_pan,
                                       step=-args.step, label="PAN LEFT")

        # --- Step 2: Return to center (0°) ---
        print("\n   Returning to center...")
        move_servo(pca, args.pan_pin, 0.0, args.invert_pan)
        time.sleep(0.4)
        print("   Now centered.\n")

        # --- Step 3: Sweep RIGHT ---
        if args.invert_pan:
            pan_max = sweep_direction(pca, args.pan_pin, args.invert_pan,
                                       step=-args.step, label="PAN RIGHT")
        else:
            pan_max = sweep_direction(pca, args.pan_pin, args.invert_pan,
                                       step=+args.step, label="PAN RIGHT")

        # --- Step 4: Calculate center ---
        pan_center = (pan_min + pan_max) / 2.0
        print(f"\n{'='*50}")
        print(f"   Calibration Results")
        print(f"{'='*50}")
        print(f"   Pan Left:   {pan_min:+.1f}°")
        print(f"   Pan Right:  {pan_max:+.1f}°")
        print(f"   Pan Center: {pan_center:+.1f}°")
        print(f"{'='*50}")

        # --- Save ---
        os.makedirs(CONFIG_DIR, exist_ok=True)
        data = {
            "pan_min": pan_min,
            "pan_max": pan_max,
            "pan_center": pan_center,
            "tilt_min": -30.0,
            "tilt_max": 30.0,
            "tilt_center": 0.0,
            "calibrated": True,
            "version": "manual-v1.0"
        }
        with open(CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=2)
        print(f"\n✅ Saved calibration to {CONFIG_PATH}")

        # --- Return to center ---
        print("\n   Returning to center...")
        move_servo(pca, args.pan_pin, pan_center, args.invert_pan)
        time.sleep(0.3)
        print("   Done.\n")

    except KeyboardInterrupt:
        print("\n   Aborted.")
    finally:
        pca.deinit()


if __name__ == "__main__":
    main()
