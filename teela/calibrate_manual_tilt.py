#!/usr/bin/env python3
"""
Teela Manual Tilt Calibrator — Ultra-Safe
========================================
Discovers UP and DOWN servo limits with extreme caution.
Tilt is the dangerous axis — too far up/down can damage the
brain/camera assembly.

Usage:
    python teela/calibrate_manual_tilt.py --tilt-pin 1 --invert-tilt

Safety features:
  - Smaller step size: 1.0° (vs 3.0° for pan)
  - Longer delay between steps: 0.4s
  - Mandatory confirmation before UP (most dangerous direction)
  - ESC immediately stops and returns to center

Preserves existing pan limits in ~/.config/teela/calibration.json
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


def load_existing():
    """Load current config so we don't lose pan limits."""
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def sweep_direction(pca, pin, invert, step, label, confirm_each=False):
    """
    Incrementally move the tilt servo.
    'a' = advance  |  SPACE = record limit  |  ESC = abort

    If confirm_each=True, asks user to confirm before EVERY step.
    """
    print(f"\n{'='*56}")
    print(f"   {label}")
    print(f"{'='*56}")
    print(f"   Step size: {abs(step)}°")
    print("   'a' = advance  |  SPACE = record limit  |  ESC = abort\n")

    if confirm_each:
        print("   ⚠️  You must confirm EACH step by pressing 'y' + ENTER!")
        print(f"   Step size reduced to {abs(step)}°\n")

    angle = 0.0
    move_servo(pca, pin, angle, invert)
    time.sleep(0.4)
    print(f"   Starting at {angle:.0f}°")

    while True:
        if confirm_each:
            # Ultra-cautious mode: confirm every single step
            print(f"\n   Next move: {angle + step:+.1f}°")
            print("   Press 'y' + ENTER to move, SPACE to record limit, ESC to abort")
            try:
                response = input("   > ").strip().lower()
            except EOFError:
                response = ""
            if response == "y" or response == "yes":
                angle += step
                print(f"     → {angle:+.1f}°")
                move_servo(pca, pin, angle, invert)
                time.sleep(0.4)
                continue
            elif response == "":
                continue
            elif response == " " or response == "s":
                print(f"\n   ✅ {label} limit recorded: {angle:.1f}°")
                return angle
            else:
                # Re-interpret as limit record
                print(f"\n   ✅ {label} limit recorded: {angle:.1f}°")
                return angle
        else:
            # Normal cautious mode
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
                time.sleep(0.4)


def main():
    parser = argparse.ArgumentParser(description="Ultra-safe tilt limit calibrator")
    parser.add_argument("--tilt-pin", type=int, default=1)
    parser.add_argument("--invert-tilt", action="store_true",
                        help="Invert tilt direction (same as TeelaMaster)")
    parser.add_argument("--step-down", type=float, default=1.0,
                        help="Step size for DOWN direction (default: 1.0°)")
    parser.add_argument("--step-up", type=float, default=0.5,
                        help="Step size for UP direction, the dangerous axis (default: 0.5°)")
    args = parser.parse_args()

    print("\n" + "=" * 56)
    print("   Teela Manual Tilt Calibrator — ULTRA SAFE")
    print("=" * 56)
    print("\n   ⚠️  WARNING: Tilt is the DANGEROUS axis.")
    print("   Too far UP or DOWN can destroy the brain/camera mount.")
    print("   Move slowly. Stop BEFORE anything touches.\n")

    pca = PCA9685(bus=7, address=0x40, freq=50)
    print("[OK] PCA9685 connected.\n")

    # Load existing pan limits
    existing = load_existing()
    pan_min = existing.get("pan_min", -90.0)
    pan_max = existing.get("pan_max", 90.0)
    pan_center = existing.get("pan_center", 0.0)

    try:
        # --- Step 1: Tilt DOWN (generally safer) ---
        print("\n[1/3] TILT DOWN")
        print("   This direction usually has more clearance.")
        tilt_min = sweep_direction(pca, args.tilt_pin, args.invert_tilt,
                                    step=-args.step_down, label="TILT DOWN")

        # --- Step 2: Return to center ---
        print("\n   Returning to center...")
        move_servo(pca, args.tilt_pin, 0.0, args.invert_tilt)
        time.sleep(0.5)
        print("   Now centered.\n")

        # --- Step 3: Tilt UP (most dangerous!) ---
        print("\n" + "⚠️" * 20)
        print("   TILT UP — MOST DANGEROUS DIRECTION")
        print("   This moves the brain UP toward the body/mount.")
        print("   Use the smallest steps. Stop IMMEDIATELY if you hear strain.")
        print("⚠️" * 20)
        tilt_max = sweep_direction(pca, args.tilt_pin, args.invert_tilt,
                                    step=+args.step_up, label="TILT UP",
                                    confirm_each=True)  # extra cautious!

        # --- Calculate center ---
        tilt_center = (tilt_min + tilt_max) / 2.0

        print(f"\n{'='*56}")
        print(f"   Calibration Results")
        print(f"{'='*56}")
        print(f"   Tilt Down:  {tilt_min:+.1f}°")
        print(f"   Tilt Up:    {tilt_max:+.1f}°")
        print(f"   Tilt Center:{tilt_center:+.1f}°")
        print(f"{'='*56}")

        # --- Save ---
        os.makedirs(CONFIG_DIR, exist_ok=True)
        data = {
            "pan_min": pan_min,
            "pan_max": pan_max,
            "pan_center": pan_center,
            "tilt_min": tilt_min,
            "tilt_max": tilt_max,
            "tilt_center": tilt_center,
            "calibrated": True,
            "version": "manual-v1.1"
        }
        with open(CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=2)
        print(f"\n✅ Saved calibration to {CONFIG_PATH}")

        # --- Return to center ---
        print("\n   Returning to tilt center...")
        move_servo(pca, args.tilt_pin, tilt_center, args.invert_tilt)
        time.sleep(0.3)
        print("   Done.\n")

    except KeyboardInterrupt:
        print("\n   Aborted. Returning to center.")
        move_servo(pca, args.tilt_pin, 0.0, args.invert_tilt)
        time.sleep(0.3)
    finally:
        pca.deinit()


if __name__ == "__main__":
    main()
