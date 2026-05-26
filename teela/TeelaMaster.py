#!/usr/bin/env python3
"""
TeelaMaster — Unified Robot Controller with Calibration Wizard
===============================================================
One command runs everything. Safe, guided calibration discovers
physical servo limits to prevent mechanical damage.

Quick test (servos only, no camera):
    python TeelaMaster.py --servo-test --pan-pin 0 --tilt-pin 1

First-time calibration:
    python TeelaMaster.py --discover-limits --display

Normal run (uses saved limits):
    python teela/TeelaMaster.py --mode csi --sensor-id 0 --display

Interactive keys during run:
    c  → Calibrate (uses saved limits if available)
    l  → Re-discover limits (manual wizard)
    t  → Toggle tracking
    q  → Quit
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import select
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from typing import Optional

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from utils.pca9685_driver import PCA9685

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("TeelaMaster")

CONFIG_PATH = os.path.expanduser("~/.config/teela/calibration.json")


# ── Config ───────────────────────────────────

@dataclass
class ServoLimits:
    """Discovered hardware limits. Persisted to disk after calibration."""
    pan_min: float = -80.0   # degrees (logical: -90 left … +90 right)
    pan_max: float = +80.0
    tilt_min: float = -30.0  # down (physical danger zone — be cautious)
    tilt_max: float = +30.0  # up (most dangerous — stop early)
    pan_center: float = 0.0
    tilt_center: float = 0.0
    calibrated: bool = False
    version: str = "2.0"

    def clamp_pan(self, v: float) -> float:
        return max(self.pan_min, min(self.pan_max, v))

    def clamp_tilt(self, v: float) -> float:
        return max(self.tilt_min, min(self.tilt_max, v))


def load_limits() -> ServoLimits:
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
            return ServoLimits(**data)
        except Exception:
            logger.warning(f"Corrupt calibration file at {CONFIG_PATH}; using defaults.")
    return ServoLimits()


def save_limits(lim: ServoLimits) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(asdict(lim), f, indent=2)
    logger.info(f"Calibration saved to {CONFIG_PATH}")


# ── Camera ───────────────────────────────────

def build_gst_pipeline(sensor_id: int, width: int, height: int, fps: int) -> str:
    return (
        f"nvarguscamerasrc sensor-id={sensor_id} ! "
        f"video/x-raw(memory:NVMM),width={width},height={height},format=NV12,framerate={fps}/1 ! "
        f"nvvidconv flip-method=0 ! "
        f"video/x-raw,width=640,height=360,format=BGRx ! "
        f"videoconvert ! video/x-raw,format=BGR ! appsink drop=True max-buffers=1"
    )


class MockCamera:
    """Synthetic target for headless dev / no-camera testing."""
    def __init__(self, width: int = 640, height: int = 360, fps: int = 30):
        self.width = width
        self.height = height
        self.fps = fps
        self._open = True
        self._x = width // 2
        self._y = height // 2
        self._vx = 3.0
        self._vy = 2.5

    def isOpened(self) -> bool:
        return self._open

    def release(self) -> None:
        self._open = False

    def read(self) -> tuple[bool, Optional[np.ndarray]]:
        if not self._open:
            return False, None
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[:, :] = (40, 40, 50)
        self._x += self._vx
        self._y += self._vy
        if self._x <= 40 or self._x >= self.width - 40:
            self._vx *= -1
        if self._y <= 50 or self._y >= self.height - 50:
            self._vy *= -1
        x1 = int(self._x - 40)
        y1 = int(self._y - 50)
        x2 = int(self._x + 40)
        y2 = int(self._y + 50)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (180, 210, 240), -1)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (140, 180, 220), 2)
        eye_y = int(y1 + 35)
        cv2.circle(frame, (int(x1 + 24), eye_y), 5, (30, 30, 30), -1)
        cv2.circle(frame, (int(x1 + 56), eye_y), 5, (30, 30, 30), -1)
        time.sleep(1.0 / self.fps)
        return True, frame


# ── Face Detector ────────────────────────────

def find_haarcascade() -> Optional[str]:
    candidates = [
        "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
        "/usr/share/opencv/haarcascades/haarcascade_frontalface_default.xml",
        "/usr/local/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


# ── Master Controller ───────────────────────

class TeelaMaster:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self._running = False
        self._frame: Optional[np.ndarray] = None
        self._frame_lock = threading.RLock()
        self._track_mode: str = "idle"
        self._track_lock = threading.Lock()
        self._last_track_time: float = 0.0

        self._limits = load_limits()
        self._calibrating: bool = False
        self._calibration_phase: str = "idle"

        self._pan_current: float = 0.0
        self._tilt_current: float = 0.0
        self._pan_target: float = 0.0
        self._tilt_target: float = 0.0
        self._motion_lock = threading.RLock()

        self._cap: Optional[cv2.VideoCapture] = None
        self._pca: Optional[PCA9685] = None
        self._face_cascade: Optional[cv2.CascadeClassifier] = None

        self._vision_thread: Optional[threading.Thread] = None
        self._motion_thread: Optional[threading.Thread] = None
        self._display_thread: Optional[threading.Thread] = None

    # ── Lifecycle ────────────────────────────

    def start(self) -> None:
        self._running = True
        self._init_camera()
        self._init_servo_hardware()
        self._init_face_detector()

        self._vision_thread = threading.Thread(target=self._vision_loop, daemon=True)
        self._vision_thread.start()
        self._motion_thread = threading.Thread(target=self._motion_loop, daemon=True)
        self._motion_thread.start()
        if self.args.display:
            self._display_thread = threading.Thread(target=self._display_loop, daemon=True)
            self._display_thread.start()
        logger.info(
            f"Started: camera={self.args.mode} servo={'HW' if self._pca else 'FAKE'} "
            f"face={'ON' if self._face_cascade else 'OFF'}"
        )

    def stop(self) -> None:
        self._running = False
        for t in (self._vision_thread, self._motion_thread, self._display_thread):
            if t:
                t.join(timeout=2.0)
        if self._cap:
            self._cap.release()
        if self._pca:
            self._pca.deinit()
        if self.args.display:
            cv2.destroyAllWindows()
        logger.info("TeelaMaster stopped.")

    # ── Init ─────────────────────────────────

    def _init_camera(self) -> None:
        if self.args.mode == "mock":
            self._cap = MockCamera()
            return
        if self.args.mode in ("csi", "auto"):
            gst = build_gst_pipeline(self.args.sensor_id, self.args.width, self.args.height, self.args.fps)
            cap = cv2.VideoCapture(gst, cv2.CAP_GSTREAMER)
            if cap.isOpened():
                ret, _ = cap.read()
                if ret:
                    self._cap = cap
                    logger.info(f"CSI camera: sensor-id={self.args.sensor_id}")
                    return
                cap.release()
            logger.warning("CSI camera failed.")
        if self.args.mode in ("usb", "auto"):
            cap = cv2.VideoCapture(self.args.usb_device)
            if cap.isOpened():
                self._cap = cap
                logger.info(f"USB camera: /dev/video{self.args.usb_device}")
                return
            logger.warning(f"USB camera failed.")
        logger.warning("Using mock camera.")
        self._cap = MockCamera()

    def _init_servo_hardware(self) -> None:
        if self.args.disable_servos:
            return
        try:
            self._pca = PCA9685(bus=7, address=0x40, freq=50)
            # Center servos
            self._pan_current = self._limits.pan_center
            self._tilt_current = self._limits.tilt_center
            self._pan_target = self._pan_current
            self._tilt_target = self._tilt_current
            self._write_hardware()
            logger.info("Servo hardware initialized and centered.")
        except Exception as e:
            if self.args.require_servos:
                raise
            logger.warning(f"Servo hardware unavailable: {e}")

    def _init_face_detector(self) -> None:
        cp = find_haarcascade()
        if cp:
            self._face_cascade = cv2.CascadeClassifier(cp)
            logger.info("Face detector ready.")
        else:
            logger.warning("Face cascade missing; tracking disabled.")

    # ── Vision Thread ────────────────────────

    def _vision_loop(self) -> None:
        while self._running:
            if self._cap is None:
                time.sleep(0.1)
                continue
            ret, frame = self._cap.read()
            if not ret or frame is None:
                time.sleep(0.05)
                continue
            with self._frame_lock:
                self._frame = frame.copy()

            if self._calibrating:
                continue

            with self._track_lock:
                mode = self._track_mode

            if mode == "tracking" and self._face_cascade is not None:
                target = self._detect_face(frame)
                if target:
                    with self._motion_lock:
                        gain = 30.0
                        tx = -target["offset_x"] * gain
                        ty = -target["offset_y"] * gain
                        self._pan_target = self._limits.clamp_pan(tx)
                        self._tilt_target = self._limits.clamp_tilt(ty)

    def _detect_face(self, frame: np.ndarray) -> Optional[dict]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self._face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40)
        )
        if len(faces) == 0:
            return None
        x, y, w, h = max(faces, key=lambda fc: fc[2] * fc[3])
        fh, fw = frame.shape[:2]
        cx = x + w // 2
        cy = y + h // 2
        self._last_track_time = time.time()
        return {
            "offset_x": round((cx / fw - 0.5) * 2.0, 3),
            "offset_y": round((cy / fh - 0.5) * 2.0, 3),
        }

    # ── Motion Thread ────────────────────────

    def _motion_loop(self) -> None:
        dt = 1.0 / 60.0
        while self._running:
            t0 = time.monotonic()
            with self._motion_lock:
                if not self._calibrating:
                    max_delta = 2.0 * self.args.servo_speed
                    p_err = max(-max_delta, min(max_delta, self._pan_target - self._pan_current))
                    t_err = max(-max_delta, min(max_delta, self._tilt_target - self._tilt_current))
                    self._pan_current += p_err
                    self._tilt_current += t_err
                    self._pan_current = self._limits.clamp_pan(self._pan_current)
                    self._tilt_current = self._limits.clamp_tilt(self._tilt_current)
                self._write_hardware()
            elapsed = time.monotonic() - t0
            slp = dt - elapsed
            if slp > 0:
                time.sleep(slp)

    def _write_hardware(self) -> None:
        if self._pca is None:
            return
        pan_cmd = -self._pan_current if self.args.invert_pan else self._pan_current
        tilt_cmd = -self._tilt_current if self.args.invert_tilt else self._tilt_current
        try:
            self._pca.set_servo_angle(self.args.pan_pin, pan_cmd + 90.0)
            self._pca.set_servo_angle(self.args.tilt_pin, tilt_cmd + 90.0)
        except Exception as e:
            logger.debug(f"Servo write error: {e}")

    # ── Calibration (safe auto-sweep with saved limits) ──

    def trigger_calibration(self) -> None:
        if self._calibrating:
            logger.warning("Already calibrating.")
            return
        if not self._limits.calibrated:
            logger.error("No saved limits! Run --discover-limits first.")
            print("\n⚠️  First-time calibration required!")
            print("    Run: python teela/TeelaMaster.py --discover-limits\n")
            return
        self._calibrating = True
        self._calibration_phase = "start"
        logger.info("=== CALIBRATION STARTED ===")
        logger.info(f"Using limits: pan [{self._limits.pan_min:.0f},{self._limits.pan_max:.0f}] tilt [{self._limits.tilt_min:.0f},{self._limits.tilt_max:.0f}]")

    # ── Vision-Based AUTO Calibration ───────

    def auto_calibrate_pan(self) -> None:
        """
        Automatic pan limit detection using camera feedback.
        Slowly sweeps left and right, watching the camera image.
        When the servo hits a mechanical limit, pixels stop changing → we detect it.
        """
        if self._pca is None:
            logger.error("No servo hardware for auto-calibration.")
            return
        if self._cap is None:
            logger.error("No camera for auto-calibration.")
            return

        print("\n========================================")
        print("  Auto Pan Calibration (Vision)        ")
        print("========================================")
        print("  Moving to mechanical limits...")
        print("  Camera is watching for motion.        \n")

        def detect_motion_stopped(reference_frame: np.ndarray,
                                   angle: float, direction: str,
                                   step: float = 1.0,
                                   timeout: float = 10.0,
                                   motion_threshold: float = 3.0) -> float:
            """Move in [direction] until pixels stop changing. Returns limit angle."""
            logger.info(f"  [AUTO CAL] Starting {direction} sweep...")
            start_time = time.monotonic()
            current_angle = angle
            self._move_servo_now(current_angle, 0.0)
            time.sleep(0.3)

            while time.monotonic() - start_time < timeout:
                # Move one step
                current_angle += step
                self._move_servo_now(current_angle, 0.0)
                time.sleep(0.2)  # give servo time to move

                # Get fresh frame
                ret, frame = self._cap.read()
                if not ret or frame is None:
                    continue

                # Compare difference
                gray_ref = cv2.cvtColor(reference_frame, cv2.COLOR_BGR2GRAY)
                gray_now = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                diff = cv2.absdiff(gray_ref, gray_now)
                mean_diff = np.mean(diff)

                # If pixels barely changed, servo stopped moving (hit limit)
                if mean_diff < motion_threshold:
                    logger.info(f"  [AUTO CAL] {direction} limit detected at {current_angle:.1f}° (motion={mean_diff:.1f})")
                    return current_angle

                # Update reference for next iteration
                reference_frame = frame.copy()

                if int(current_angle) % 10 == 0:
                    logger.info(f"    {direction}: {current_angle:.0f}°, motion={mean_diff:.1f}")

            logger.warning(f"  [AUTO CAL] {direction} sweep hit timeout. Last angle: {current_angle:.1f}°")
            return current_angle

        # ── Sweep LEFT ──
        ret, ref_frame = self._cap.read()
        if not ret or ref_frame is None:
            logger.error("Camera not providing frames.")
            return

        # Start from assumed center
        pan_left = detect_motion_stopped(
            reference_frame=ref_frame,
            angle=0.0,
            direction="LEFT",
            step=-1.0,
            motion_threshold=5.0
        )
        print(f"  ✅ PAN LEFT  limit: {pan_left:.1f}°")

        # ── Sweep RIGHT ──
        # Need a new reference frame looking from the left position
        self._move_servo_now(pan_left + 5.0, 0.0)  # Back off slightly
        time.sleep(0.3)
        ret, ref_frame = self._cap.read()
        if not ret:
            ref_frame = None
        if ref_frame is not None:
            cv2.resize(ref_frame, (ref_frame.shape[1]//2, ref_frame.shape[0]//2))  # warm cache

        # Re-center before going right
        self._move_servo_now(0.0, 0.0)
        time.sleep(0.3)
        ret, ref_frame = self._cap.read()
        if not ret:
            ref_frame = np.zeros((360, 640, 3), dtype=np.uint8)

        pan_right = detect_motion_stopped(
            reference_frame=ref_frame,
            angle=0.0,
            direction="RIGHT",
            step=+1.0,
            motion_threshold=5.0
        )
        print(f"  ✅ PAN RIGHT limit: {pan_right:.1f}°")

        # ── Calculate center ──
        pan_center = (pan_left + pan_right) / 2.0
        print(f"  📐 CENTER: {pan_center:.1f}°")

        # Save to limits
        self._limits.pan_min = pan_left
        self._limits.pan_max = pan_right
        self._limits.pan_center = pan_center

        # ── Tilt (manual — too dangerous to automate) ──
        print("\n  Tilt limits must be set manually.")
        print("  Run --discover-limits for full calibration.\n")
        print("  Default tilt limits used: [-30°, +30°]")

        self._limits.tilt_min = -30.0
        self._limits.tilt_max = +30.0
        self._limits.tilt_center = 0.0
        self._limits.calibrated = True
        save_limits(self._limits)
        self._limits = self._limits

        # Return to center
        self._move_servo_now(pan_center, 0.0)
        time.sleep(0.3)

        print("  ✅ Auto-calibration saved!")
        print(f"  Pan range: [{pan_left:.0f} ... {pan_center:.0f} ... {pan_right:.0f}]")
        print("========================================\n")

    # ── Time-based calibration sweep ──────────

    def _run_calibration_frame(self) -> None:
        """Runs inside motion_loop during calibration phase. Time-based sweep."""
        now = time.monotonic()
        phase = self._calibration_phase

        if phase == "start":
            self._pan_target = self._limits.pan_min
            self._tilt_target = self._limits.tilt_center
            self._calibration_phase = "pan_left"
            self._phase_start = now
            logger.info("[CAL] Moving to pan left limit")

        elif phase == "pan_left":
            if now - self._phase_start > 2.0:
                self._pan_target = self._limits.pan_max
                self._calibration_phase = "pan_right"
                logger.info("[CAL] Moving to pan right limit")

        elif phase == "pan_right":
            if now - self._phase_start > 4.0:
                self._pan_target = self._limits.pan_center
                self._calibration_phase = "pan_center"
                logger.info("[CAL] Returning to pan center")

        elif phase == "pan_center":
            if now - self._phase_start > 5.5:
                self._tilt_target = self._limits.tilt_min
                self._calibration_phase = "tilt_down"
                logger.info("[CAL] Moving to tilt down limit")

        elif phase == "tilt_down":
            if now - self._phase_start > 7.0:
                self._tilt_target = self._limits.tilt_max
                self._calibration_phase = "tilt_up"
                logger.info("[CAL] Moving to tilt up limit (watch for clearance!)")

        elif phase == "tilt_up":
            if now - self._phase_start > 8.5:
                self._tilt_target = self._limits.tilt_center
                self._calibration_phase = "center"
                logger.info("[CAL] Returning to center")

        elif phase == "center":
            if now - self._phase_start > 10.0:
                logger.info("[CAL] Horizon lock + confirmation")
                self._calibration_phase = "confirm"

        elif phase == "confirm":
            if now - self._phase_start > 12.0:
                logger.info("🎤 Calibration complete. I am looking at you.")
                self._calibrating = False
                self._calibration_phase = "idle"
                self._track_mode = "tracking"

    # ── DISCOVERY WIZARD (manual, step-by-step) ──
        now = time.monotonic()
        phase = self._calibration_phase

        if phase == "start":
            self._pan_target = self._limits.pan_min
            self._tilt_target = self._limits.tilt_center
            self._calibration_phase = "pan_left"
            self._phase_start = now
            logger.info("[CAL] Moving to pan left limit")

        elif phase == "pan_left":
            if now - self._phase_start > 2.0:
                self._pan_target = self._limits.pan_max
                self._calibration_phase = "pan_right"
                logger.info("[CAL] Moving to pan right limit")

        elif phase == "pan_right":
            if now - self._phase_start > 4.0:
                self._pan_target = self._limits.pan_center
                self._calibration_phase = "pan_center"
                logger.info("[CAL] Returning to pan center")

        elif phase == "pan_center":
            if now - self._phase_start > 5.5:
                self._tilt_target = self._limits.tilt_min
                self._calibration_phase = "tilt_down"
                logger.info("[CAL] Moving to tilt down limit")

        elif phase == "tilt_down":
            if now - self._phase_start > 7.0:
                self._tilt_target = self._limits.tilt_max
                self._calibration_phase = "tilt_up"
                logger.info("[CAL] Moving to tilt up limit (watch for clearance!)")

        elif phase == "tilt_up":
            if now - self._phase_start > 8.5:
                self._tilt_target = self._limits.tilt_center
                self._calibration_phase = "center"
                logger.info("[CAL] Returning to center")

        elif phase == "center":
            if now - self._phase_start > 10.0:
                logger.info("[CAL] Horizon lock + confirmation")
                self._calibration_phase = "confirm"

        elif phase == "confirm":
            if now - self._phase_start > 12.0:
                logger.info("🎤 Calibration complete. I am looking at you.")
                self._calibrating = False
                self._calibration_phase = "idle"
                self._track_mode = "tracking"

    # ── DISCOVERY WIZARD (manual, step-by-step) ──

    def run_limit_discovery_wizard(self) -> None:
        """
        Guided manual calibration. The user slowly drives each axis to its
        physical limit and presses SPACE when the limit is reached.
        Tilt UP is done last with tiny steps to prevent damage.
        """
        if self._pca is None:
            logger.error("No servo hardware connected.")
            return

        print("\n" + "=" * 56)
        print("  Teela Limit Discovery Wizard")
        print("=" * 56)
        print("  This will discover your robot's physical limits.")
        print("  Move slowly. Press SPACE when a LIMIT is reached.")
        print("  Press ESC to abort at any time.")
        print("=" * 56 + "\n")

        lim = ServoLimits()

        # Helper: single-axis discovery
        def discover_axis(name: str, step: float, start: float,
                          tiny: bool = False) -> float:
            """Drive one axis in [step]° increments until user hits SPACE.
            If tiny=True, asks for confirmation every step.
            """
            print(f"\n--- Discovering {name} ---")
            print(f"  Current step size: {step}° {'(CAUTION MODE)' if tiny else ''}")
            print("  a = advance  |  SPACE = limit reached  |  ESC = abort\n")
            angle = start
            self._move_servo_now(angle, 0.0 if "pan" in name.lower() else None)
            time.sleep(0.3)

            while True:
                ch = _getch(timeout=0.05)
                if ch == " ":
                    print(f"  ✅ {name} limit recorded: {angle:.1f}°")
                    return angle
                if ch == "\x1b" or ch == "\x03":  # ESC or Ctrl+C
                    raise KeyboardInterrupt("User aborted discovery")
                if ch == "a":
                    angle += step
                    print(f"    Moving {name} → {angle:.1f}°")
                    if "pan" in name.lower():
                        self._move_servo_now(angle, self._tilt_current)
                        self._pan_current = angle
                    else:
                        self._move_servo_now(self._pan_current, angle)
                        self._tilt_current = angle

        try:
            # 1. Pan left (negative)
            lim.pan_min = discover_axis("PAN LEFT", step=-3.0, start=0.0)

            # 2. Pan right (positive)
            lim.pan_max = discover_axis("PAN RIGHT", step=+3.0, start=0.0)

            # 3. Pan center
            lim.pan_center = (lim.pan_min + lim.pan_max) / 2.0
            print(f"\n  📐 Pan center calculated: {lim.pan_center:.1f}°")
            self._move_servo_now(lim.pan_center, self._tilt_current)
            time.sleep(0.5)

            # 4. Tilt down (negative, generally safe)
            lim.tilt_min = discover_axis("TILT DOWN", step=-2.0, start=0.0)

            # 5. Tilt UP (most dangerous — tiny steps, extra caution)
            print("\n" + "⚠️ " * 10)
            print("  TILT UP is the MOST DANGEROUS direction!")
            print("  Use TINY steps. Stop BEFORE anything touches.")
            print("⚠️ " * 10)
            lim.tilt_max = discover_axis("TILT UP", step=+1.0, start=0.0, tiny=True)

            # 6. Tilt center
            lim.tilt_center = (lim.tilt_min + lim.tilt_max) / 2.0
            print(f"\n  📐 Tilt center calculated: {lim.tilt_center:.1f}°")

            # Return to discovered center
            self._move_servo_now(lim.pan_center, lim.tilt_center)
            time.sleep(0.5)

            # Save
            lim.calibrated = True
            save_limits(lim)
            self._limits = lim

            print("\n" + "=" * 56)
            print("  ✅ CALIBRATION SAVED")
            print(f"  Pan range:  [{lim.pan_min:.0f}° ... {lim.pan_center:.0f}° ... {lim.pan_max:.0f}°]")
            print(f"  Tilt range: [{lim.tilt_min:.0f}° ... {lim.tilt_center:.0f}° ... {lim.tilt_max:.0f}°]")
            print("=" * 56 + "\n")

        except KeyboardInterrupt:
            logger.warning("Discovery aborted by user.")
            self._move_servo_now(0.0, 0.0)

    def _move_servo_now(self, pan: float, tilt: Optional[float] = None) -> None:
        if self._pca is None:
            return
        if tilt is None:
            tilt = self._tilt_current
        self._pan_current = pan
        self._tilt_current = tilt
        pan_cmd = -pan if self.args.invert_pan else pan
        tilt_cmd = -tilt if self.args.invert_tilt else tilt
        self._pca.set_servo_angle(self.args.pan_pin, pan_cmd + 90.0)
        self._pca.set_servo_angle(self.args.tilt_pin, tilt_cmd + 90.0)

    # ── Display Thread ─────────────────────────

    def _display_loop(self) -> None:
        cv2.namedWindow("Teela", cv2.WINDOW_AUTOSIZE)
        font = cv2.FONT_HERSHEY_SIMPLEX
        while self._running:
            with self._frame_lock:
                frame = self._frame.copy() if self._frame is not None else None
            if frame is None:
                cv2.waitKey(50)
                continue
            fh, fw = frame.shape[:2]
            color = (0, 255, 0)
            cx, cy = fw // 2, fh // 2
            cv2.line(frame, (cx - 20, cy), (cx + 20, cy), color, 1)
            cv2.line(frame, (cx, cy - 20), (cx, cy + 20), color, 1)

            with self._motion_lock:
                hud = [
                    f"PAN: {self._pan_current:+.1f}°",
                    f"TILT: {self._tilt_current:+.1f}°",
                    f"TRACK: {self._track_mode.upper()}",
                    f"CAL: {self._calibration_phase if self._calibrating else 'IDLE'}",
                    f"LIMITS: {'SAVED' if self._limits.calibrated else 'NOT CONFIGURED'}",
                ]
            y = 20
            for line in hud:
                cv2.putText(frame, line, (10, y), font, 0.5, color, 1)
                y += 20

            if self._track_mode == "tracking":
                tx = int(cx - self._pan_current * 3)
                ty = int(cy - self._tilt_current * 3)
                cv2.circle(frame, (tx, ty), 8, (0, 0, 255), 2)

            cv2.imshow("Teela", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                self._running = False
                break
        cv2.destroyAllWindows()

    # ── Interactive Control ──────────────────

    def interactive(self) -> None:
        print("\n╔═══════════════════════════════════════╗")
        print("║  Teela Master Controller            ║")
        print("╠═══════════════════════════════════════╣")
        print("║  c  → Calibrate (auto-sweep)          ║")
        print("║  l  → Discover limits (manual wizard) ║")
        print("║  t  → Toggle face tracking            ║")
        print("║  q  → Quit                            ║")
        print("╚═══════════════════════════════════════╝\n")

        while self._running:
            ch = _getch(timeout=0.1)
            if not ch:
                continue
            if ch == "c":
                self.trigger_calibration()
            elif ch == "l":
                self.run_limit_discovery_wizard()
            elif ch == "t":
                with self._track_lock:
                    self._track_mode = "idle" if self._track_mode == "tracking" else "tracking"
                    logger.info(f"Tracking: {'ON' if self._track_mode == 'tracking' else 'OFF'}")
            elif ch == "q":
                self._running = False
            elif ch == "\x1b" or ch == "\x03":
                self._running = False

    # ── Servo sweep (quick hardware check) ───

    def servo_sweep_test(self) -> None:
        if self._pca is None:
            logger.error("No servo hardware.")
            return
        logger.info("Sweeping pan servo...")
        for a in range(0, 181, 30):
            self._pca.set_servo_angle(self.args.pan_pin, a)
            time.sleep(0.4)
        for a in range(180, -1, -30):
            self._pca.set_servo_angle(self.args.pan_pin, a)
            time.sleep(0.4)
        self._pca.set_servo_angle(self.args.pan_pin, 90)
        logger.info("Sweep complete.")


# ── Raw single-char stdin helper ───────────

def _getch(timeout: float = 0.0) -> Optional[str]:
    """Read a single character from stdin without Enter. Returns None if no input."""
    fd = sys.stdin.fileno()
    try:
        import tty, termios
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
    except Exception:
        old = None
    try:
        if timeout > 0:
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
            if not ready:
                return None
        ch = sys.stdin.read(1)
        return ch
    except Exception:
        return None
    finally:
        if old:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── CLI ──────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TeelaMaster — Unified Controller")
    parser.add_argument("--mode", choices=["csi", "usb", "mock", "auto"], default="auto")
    parser.add_argument("--sensor-id", type=int, default=0)
    parser.add_argument("--usb-device", type=int, default=0)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--display", action="store_true")
    parser.add_argument("--calibrate", action="store_true", help="Auto-sweep calibration")
    parser.add_argument("--discover-limits", action="store_true", help="Manual limit discovery wizard")
    parser.add_argument("--servo-test", action="store_true", help="Quick servo sweep and exit")
    parser.add_argument("--auto-cal-pan", action="store_true",
                        help="Run vision-based automatic pan limit detection and exit")
    parser.add_argument("--pan-pin", type=int, default=0)
    parser.add_argument("--tilt-pin", type=int, default=1)
    parser.add_argument("--invert-pan", action="store_true",
                        help="Invert pan direction (servo physically moves opposite to command)")
    parser.add_argument("--invert-tilt", action="store_true",
                        help="Invert tilt direction")
    parser.add_argument("--pan-min", type=float, default=-80.0)
    parser.add_argument("--pan-max", type=float, default=80.0)
    parser.add_argument("--tilt-min", type=float, default=-30.0)
    parser.add_argument("--tilt-max", type=float, default=30.0)
    parser.add_argument("--servo-speed", type=float, default=0.15)
    parser.add_argument("--disable-servos", action="store_true")
    parser.add_argument("--require-servos", action="store_true")
    args = parser.parse_args()

    master = TeelaMaster(args)

    if args.servo_test:
        try:
            master._init_servo_hardware()
            master.servo_sweep_test()
        except Exception as e:
            logger.error(f"Servo test failed: {e}")
            sys.exit(1)
        finally:
            if master._pca:
                master._pca.deinit()
        sys.exit(0)

    if args.discover_limits:
        try:
            master._init_servo_hardware()
            master.run_limit_discovery_wizard()
        except Exception as e:
            logger.error(f"Discovery failed: {e}")
            traceback.print_exc()
        finally:
            if master._pca:
                master._pca.deinit()
        sys.exit(0)

    if args.auto_cal_pan:
        try:
            master._init_camera()
            master._init_servo_hardware()
            master.auto_calibrate_pan()
        except Exception as e:
            logger.error(f"Auto-cal failed: {e}")
            traceback.print_exc()
        finally:
            if master._pca:
                master._pca.deinit()
            if master._cap:
                master._cap.release()
        sys.exit(0)

    master.start()
    if args.calibrate:
        time.sleep(1.0)
        master.trigger_calibration()

    try:
        master.interactive()
    except KeyboardInterrupt:
        pass
    finally:
        master.stop()
        logger.info("Shutdown complete.")
