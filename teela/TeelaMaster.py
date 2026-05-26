#!/usr/bin/env python3
"""
TeelaMaster — Vision + Servos + Calibration in One Command
==========================================================
Single-entrypoint launcher for all three Teela subsystems.
Replaces the 3-terminal ZMQ-bus approach with a unified controller.

    python TeelaMaster.py --mode csi --sensor-id 0 --calibrate

Commands (interactive):
    c   — Run calibration
    t   — Toggle face tracking ON/OFF
    s   — Trigger self-calibration sequence
    q   — Quit

Hardware test (no camera needed):
    python TeelaMaster.py --servo-test --pan-pin 0 --tilt-pin 1
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import traceback
from typing import Optional

import cv2
import numpy as np
import zmq

sys.path.insert(0, os.path.dirname(__file__))

from utils.pca9685_driver import PCA9685

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("TeelaMaster")


# ── Camera Pipeline ──────────────────────────

def build_gst_pipeline(sensor_id: int = 0, width: int = 1280, height: int = 720, fps: int = 30) -> str:
    return (
        f"nvarguscamerasrc sensor-id={sensor_id} ! "
        f"video/x-raw(memory:NVMM),width={width},height={height},format=NV12,framerate={fps}/1 ! "
        f"nvvidconv flip-method=0 ! "
        f"video/x-raw,width=640,height=360,format=BGRx ! "
        f"videoconvert ! video/x-raw,format=BGR ! appsink drop=True max-buffers=1"
    )


class MockCamera:
    """Synthetic bouncing rectangle when no real camera is available."""
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
    """Search common Jetson/Ubuntu paths for the frontal face classifier."""
    candidates = [
        "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
        "/usr/share/opencv/haarcascades/haarcascade_frontalface_default.xml",
        "/usr/local/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
        "/usr/local/share/opencv/haarcascades/haarcascade_frontalface_default.xml",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


# ── Unified Controller ───────────────────────

class TeelaMaster:
    """Brings Vision, Servo Control, and Calibration together in one process.

    Internals run in threads:
        - _vision_thread   : captures frames, does face detection
        - _motion_thread   : smooth pan/tilt interpolation to hardware
    """

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self._running = False
        self._frame: Optional[np.ndarray] = None
        self._frame_lock = threading.RLock()
        self._track_mode: str = "idle"
        self._track_lock = threading.Lock()

        # State machine
        self._calibrating: bool = False
        self._calibration_phase: str = "idle"
        self._cal_start_time: float = 0.0

        # Servo state
        self._pan_current: float = 0.0
        self._tilt_current: float = 0.0
        self._pan_target: float = 0.0
        self._tilt_target: float = 0.0
        self._lock = threading.RLock()

        # Hardware
        self._cap: Optional[cv2.VideoCapture] = None
        self._pca: Optional[PCA9685] = None
        self._face_cascade: Optional[cv2.CascadeClassifier] = None

        # Threads
        self._vision_thread: Optional[threading.Thread] = None
        self._motion_thread: Optional[threading.Thread] = None
        self._display_thread: Optional[threading.Thread] = None

        logger.info("TeelaMaster initialized.")

    # ── Startup / Shutdown ─────────────────────

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

    # ── Init ──────────────────────────────────

    def _init_camera(self) -> None:
        if self.args.mode == "mock":
            self._cap = MockCamera()
            logger.info("Using mock camera.")
            return
        if self.args.mode in ("csi", "auto"):
            gst = build_gst_pipeline(self.args.sensor_id, self.args.width, self.args.height, self.args.fps)
            self._cap = cv2.VideoCapture(gst, cv2.CAP_GSTREAMER)
            if self._cap.isOpened():
                ret, _ = self._cap.read()
                if ret:
                    logger.info(f"CSI camera opened: sensor-id={self.args.sensor_id}")
                    return
                self._cap.release()
            logger.warning("CSI camera failed.")
            if self.args.mode == "csi":
                raise RuntimeError("CSI mode requested but camera not available.")
        if self.args.mode in ("usb", "auto"):
            usb_idx = self.args.usb_device
            self._cap = cv2.VideoCapture(usb_idx)
            if self._cap.isOpened():
                logger.info(f"USB camera opened: /dev/video{usb_idx}")
                return
            logger.warning(f"USB camera /dev/video{usb_idx} failed.")
            if self.args.mode == "usb":
                raise RuntimeError("USB mode requested but camera not available.")
        logger.warning("No real camera found: using mock.")
        self._cap = MockCamera()

    def _init_servo_hardware(self) -> None:
        if self.args.servo_test or not self.args.disable_servos:
            try:
                self._pca = PCA9685(bus=7, address=0x40, freq=50)
                # Center servos on startup
                self._pan_current = 0.0
                self._tilt_current = 0.0
                self._pan_target = 0.0
                self._tilt_target = 0.0
                self._write_servo_hardware()
                logger.info("Servo hardware initialized and centered.")
            except Exception as e:
                if self.args.require_servos:
                    raise RuntimeError(f"Servo hardware required but failed: {e}")
                logger.warning(f"Servo hardware unavailable: {e}")
                self._pca = None

    def _init_face_detector(self) -> None:
        cascade_path = find_haarcascade()
        if cascade_path:
            self._face_cascade = cv2.CascadeClassifier(cascade_path)
            logger.info("Face detector loaded.")
        else:
            logger.warning(
                "No haarcascade XML found. Face tracking disabled.\n"
                "Install: sudo apt install opencv-data"
            )

    # ── Vision Thread ────────────────────────

    def _vision_loop(self) -> None:
        if self._cap is None:
            return
        while self._running:
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
                    with self._lock:
                        # Convert normalized offset (-1..+1) to servo degrees
                        # Small gain to avoid aggressive hunting
                        gain = 30.0
                        self._pan_target = -target["offset_x"] * gain
                        self._tilt_target = -target["offset_y"] * gain

    def _detect_face(self, frame: np.ndarray) -> Optional[dict]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self._face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40)
        )
        if len(faces) == 0:
            return None
        best = max(faces, key=lambda fc: fc[2] * fc[3])
        x, y, w, h = best
        cx = x + w // 2
        cy = y + h // 2
        fh, fw = frame.shape[:2]
        return {
            "offset_x": round((cx / fw - 0.5) * 2.0, 3),
            "offset_y": round((cy / fh - 0.5) * 2.0, 3),
            "confidence": round(min(1.0, (w * h) / (fw * fh) * 10 + 0.3), 3),
        }

    # ── Motion Thread ─────────────────────────

    def _motion_loop(self) -> None:
        """Interpolates pan/tilt toward their targets at ~60 Hz."""
        dt = 1.0 / 60.0
        while self._running:
            t0 = time.monotonic()

            with self._lock:
                # Simple proportional approach (no easing needed for tracking)
                # Cap max step per frame to avoid jerky movement
                max_delta = 2.0  # degrees per frame (~120°/s)
                pan_err = self._pan_target - self._pan_current
                tilt_err = self._tilt_target - self._tilt_current
                pan_err = max(-max_delta, min(max_delta, pan_err))
                tilt_err = max(-max_delta, min(max_delta, tilt_err))
                self._pan_current += pan_err * self.args.servo_speed
                self._tilt_current += tilt_err * self.args.servo_speed

                # Clamp to safe limits
                pc = self._pan_current
                tc = self._tilt_current
                pan_min = self.args.pan_min
                pan_max = self.args.pan_max
                tilt_min = self.args.tilt_min
                tilt_max = self.args.tilt_max
                self._pan_current = max(pan_min, min(pan_max, pc))
                self._tilt_current = max(tilt_min, min(tilt_max, tc))

                # If calibrating, calibration_phase sets targets directly
                if self._calibrating:
                    self._run_calibration_frame()

                self._write_servo_hardware()

            elapsed = time.monotonic() - t0
            sleep_for = dt - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

    def _write_servo_hardware(self) -> None:
        if self._pca is None:
            return
        pan_deg = self._pan_current + 90.0
        tilt_deg = self._tilt_current + 90.0
        try:
            self._pca.set_servo_angle(self.args.pan_pin, pan_deg)
            self._pca.set_servo_angle(self.args.tilt_pin, tilt_deg)
        except Exception as e:
            logger.warning(f"Servo write error: {e}")

    # ── Calibration ──────────────────────────

    def trigger_calibration(self) -> None:
        """Begin the calibration sequence."""
        if self._calibrating:
            logger.warning("Already calibrating.")
            return
        self._calibrating = True
        self._calibration_phase = "start"
        self._cal_start_time = time.monotonic()
        logger.info("=== CALIBRATION STARTED ===")
        logger.info("Servos will sweep to limits. Make sure mount has clearance!")

    def _run_calibration_frame(self) -> None:
        """State-machine driven, called inside motion_loop lock."""
        now = time.monotonic()
        elapsed = now - self._cal_start_time
        phase = self._calibration_phase

        if phase == "start":
            logger.info("[CAL] Phase: PAN_SWEEP LEFT")
            self._pan_target = self.args.pan_min
            self._tilt_target = 0.0
            self._calibration_phase = "pan_left"
            self._phase_start = now

        elif phase == "pan_left":
            if elapsed > 2.0:
                logger.info("[CAL] Phase: PAN_SWEEP RIGHT")
                self._pan_target = self.args.pan_max
                self._calibration_phase = "pan_right"

        elif phase == "pan_right":
            if elapsed > 4.0:
                logger.info("[CAL] Phase: RETURN CENTER")
                self._pan_target = 0.0
                self._tilt_target = 0.0
                self._calibration_phase = "pan_center"

        elif phase == "pan_center":
            if elapsed > 5.5:
                logger.info("[CAL] Phase: TILT_SWEEP UP")
                self._tilt_target = self.args.tilt_max
                self._calibration_phase = "tilt_up"

        elif phase == "tilt_up":
            if elapsed > 7.0:
                logger.info("[CAL] Phase: TILT_SWEEP DOWN")
                self._tilt_target = self.args.tilt_min
                self._calibration_phase = "tilt_down"

        elif phase == "tilt_down":
            if elapsed > 8.5:
                logger.info("[CAL] Phase: HORIZON LOCK")
                self._tilt_target = 0.0
                self._pan_target = 0.0
                self._calibration_phase = "horizon"

        elif phase == "horizon":
            if elapsed > 10.0:
                logger.info("[CAL] Phase: AUDIO-VISUAL CONFIRMATION")
                self._calibration_phase = "confirm"
                # Publish / say something
                logger.info("🎤 'Calibration complete. I am looking at you.'")
                # In a real system you'd trigger TTS here
                # For now, just log it

        elif phase == "confirm":
            if elapsed > 11.0:
                logger.info("[CAL] === CALIBRATION COMPLETE ===")
                self._calibrating = False
                self._calibration_phase = "idle"
                self._track_mode = "tracking"
                self._pan_target = 0.0
                self._tilt_target = 0.0

    # ── Display Thread ───────────────────────

    def _display_loop(self) -> None:
        """OpenCV window with HUD overlay."""
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

            # Draw crosshair
            cx, cy = fw // 2, fh // 2
            cv2.line(frame, (cx - 20, cy), (cx + 20, cy), color, 1)
            cv2.line(frame, (cx, cy - 20), (cx, cy + 20), color, 1)

            # HUD
            with self._lock:
                hud = [
                    f"PAN: {self._pan_current:+.1f}°",
                    f"TILT: {self._tilt_current:+.1f}°",
                    f"TRACK: {self._track_mode.upper()}",
                    f"CAL: {self._calibration_phase if self._calibrating else 'IDLE'}",
                ]
            y = 20
            for line in hud:
                cv2.putText(frame, line, (10, y), font, 0.5, color, 1)
                y += 20

            # Draw tracking target if available
            if self._track_mode == "tracking":
                tx = int(cx - self._pan_current * 3)
                ty = int(cy - self._tilt_current * 3)
                cv2.circle(frame, (tx, ty), 8, (0, 0, 255), 2)

            cv2.imshow("Teela", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                self._running = False
                break

        cv2.destroyAllWindows()

    # ── Servo Test ───────────────────────────

    def servo_sweep(self) -> None:
        """Immediate sweep test — useful before running full system."""
        if self._pca is None:
            logger.error("No servo hardware. Install PCA9685 and check I2C.")
            return
        logger.info("Servo sweep test starting...")
        for angle in range(0, 181, 30):
            self._pca.set_servo_angle(self.args.pan_pin, angle)
            time.sleep(0.4)
        for angle in range(180, -1, -30):
            self._pca.set_servo_angle(self.args.pan_pin, angle)
            time.sleep(0.4)
        self._pca.set_servo_angle(self.args.pan_pin, 90)
        logger.info("Sweep complete.")

    # ── Interactive CLI ──────────────────────

    def interactive(self) -> None:
        """Blocking. Reads stdin for single-char commands."""
        print("\n═══════════════════════════════════════")
        print("  Teela Master Controller")
        print("═══════════════════════════════════════")
        print("  c  →  Calibrate (servo sweep)")
        print("  t  →  Toggle face tracking")
        print("  s  →  Sweep servos (test only)")
        print("  q  →  Quit")
        print("═══════════════════════════════════════\n")

        try:
            import tty, termios
            old = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        except Exception:
            old = None  # Will fall back to input()

        try:
            while self._running:
                if old:
                    import select
                    if select.select([sys.stdin], [], [], 0.2)[0]:
                        ch = sys.stdin.read(1)
                    else:
                        continue
                else:
                    ch = input().strip().lower()

                if ch == "c":
                    self.trigger_calibration()
                elif ch == "t":
                    with self._track_lock:
                        if self._track_mode == "tracking":
                            self._track_mode = "idle"
                            logger.info("Tracking: OFF")
                        else:
                            self._track_mode = "tracking"
                            logger.info("Tracking: ON")
                elif ch == "s":
                    self.servo_sweep()
                elif ch == "q":
                    self._running = False
        finally:
            if old:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)


# ── CLI ──────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TeelaMaster — Unified Robot Controller")
    parser.add_argument("--mode", choices=["csi", "usb", "mock", "auto"], default="auto",
                        help="Camera mode")
    parser.add_argument("--sensor-id", type=int, default=0)
    parser.add_argument("--usb-device", type=int, default=0)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--display", action="store_true", help="Show OpenCV window")
    parser.add_argument("--calibrate", action="store_true", help="Run calibration immediately")
    parser.add_argument("--servo-test", action="store_true", help="Only sweep servos and exit")
    parser.add_argument("--pan-pin", type=int, default=0)
    parser.add_argument("--tilt-pin", type=int, default=1)
    parser.add_argument("--pan-min", type=float, default=-80.0)
    parser.add_argument("--pan-max", type=float, default=+80.0)
    parser.add_argument("--tilt-min", type=float, default=-45.0)
    parser.add_argument("--tilt-max", type=float, default=+45.0)
    parser.add_argument("--servo-speed", type=float, default=0.15,
                        help="Tracking response speed (0-1, lower=slower)")
    parser.add_argument("--disable-servos", action="store_true")
    parser.add_argument("--require-servos", action="store_true")
    args = parser.parse_args()

    master = TeelaMaster(args)

    # Quick servo-only test
    if args.servo_test:
        try:
            master._init_servo_hardware()
            master.servo_sweep()
        except Exception as e:
            logger.error(f"Servo test failed: {e}")
            sys.exit(1)
        finally:
            if master._pca:
                master._pca.deinit()
        sys.exit(0)

    # Normal startup
    master.start()

    if args.calibrate:
        time.sleep(1.0)
        master.trigger_calibration()

    print("\nPress 'c' to calibrate, 't' to toggle tracking, 'q' to quit.\n")

    try:
        master.interactive()
    except KeyboardInterrupt:
        pass
    finally:
        master.stop()
        logger.info("Goodbye.")


if __name__ == "__main__":
    main()
