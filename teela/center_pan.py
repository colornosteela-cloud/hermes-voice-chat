#!/usr/bin/env python3
"""
Center the pan servo so the user can manually remove and re-align the head.

Usage:
    python teela/center_pan.py --pan-pin 0 --tilt-pin 1 --invert-pan

The script:
  1. Initializes the PCA9685 driver
  2. Sets pan servo to logical 0° (center position)
  3. Holds it there until you press any key
  4. Detaches power gracefully so the servo can move freely

Safety:
  - Tilt is moved to 0° as well so the whole assembly is in neutral
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from utils.pca9685_driver import PCA9685


def main():
    parser = argparse.ArgumentParser(description="Center pan servo for mechanical alignment")
    parser.add_argument("--pan-pin", type=int, default=0)
    parser.add_argument("--tilt-pin", type=int, default=1)
    parser.add_argument("--invert-pan", action="store_true",
                        help="Same inversion as TeelaMaster")
    parser.add_argument("--hold", action="store_true",
                        help="Keep servo powered at center (default: power off after 5s)")
    args = parser.parse_args()

    print("\n" + "=" * 50)
    print("   Centering Pan Servo")
    print("=" * 50)
    print("\nThis will move the servo to its logical center position.")
    print("Once centered, you can remove and re-attach the head.")
    print("")

    pca = PCA9685(bus=7, address=0x40, freq=50)
    print("[OK] PCA9685 connected.")

    # Logical center is 0°
    # Physical pulse 90° = 1.5ms → standard servo center
    pan_logical = 0.0
    pan_physical = (-pan_logical if args.invert_pan else pan_logical) + 90.0
    tilt_physical = 90.0  # tilt center

    print(f"\n[MOVING] Pan servo → logical {pan_logical}° (physical pulse {pan_physical}°)")
    pca.set_servo_angle(args.pan_pin, pan_physical)
    pca.set_servo_angle(args.tilt_pin, tilt_physical)

    time.sleep(0.3)
    print("\n✅ Servo is now centered and holding position.")
    print("   Re-align the head now.")

    if args.hold:
        print("\n   Running in HOLD mode. Press Ctrl+C when done.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        print("\n   Waiting 5 seconds (or press Ctrl+C when done)...")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            pass

    print("\n[RELEASE] Powering off servo channels...")
    pca.deinit()
    print("   Done. Servo is now free to move by hand.\n")


if __name__ == "__main__":
    main()
