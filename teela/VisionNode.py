#!/usr/bin/env python3
"""
Teela Vision Node — VisionKinematicSyncSkill v2
================================================
CSI camera capture via GStreamer on NVIDIA Jetson,
USB webcam fallback, headless mock mode for development,
face/object tracking, and ZMQ bus integration.

Hardware: Jetson Nano/Orin + CSI IMX219/IMX477 / USB camera
Bus:      ZeroMQ (Mini-ROS pattern)

Usage:
    # Auto-detect best camera (CSI → USB → Mock)
    python VisionNode.py --display

    # Force mock mode (no camera needed — synthetic moving target)
    python VisionNode.py --mode mock --display

    # USB webcam (use /dev/video1)
    python VisionNode.py --mode usb --usb-device 1 --display

    # CSI with specific sensor ID
    python VisionNode.py --mode csi --sensor-id 0 --display
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
import zmq

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("TeelaVision")


# ────────────────────────────────────────────────
# ZMQ Message Schemas
# ────────────────────────────────────────────────

TOPIC_TRACKING = b"/vision/tracking"
TOPIC_STATUS = b"/sys/status"


# ────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────

@dataclass
class VisionConfig:
    """Tunable camera + ZMQ parameters."""
    # Camera
    capture_width: int = 1280
    capture_height: int = 720
    display_width: int = 640
    display_height: int = 360
    framerate: int = 30
    flip_method: int = 0
    sensor_id: int = 0
    usb_device: int = 0
    # ZMQ
    zmq_pub_uri: str = "tcp://127.0.0.1:5556"
    zmq_sub_uri: str = "tcp://127.0.0.1:5555"
    # Tracking
    face_cascade_path: str = "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"
    track_lock_timeout_s: float = 2.0
    publish_stride: int = 3


# ────────────────────────────────────────────────
# Mock Camera (development / headless)
# ────────────────────────────────────────────────

class MockCamera:
    """Synthetic frame generator. Creates a moving colored rectangle
    ("face") that bounces around the frame for tracking algorithm testing."""

    def __init__(self, width: int = 640, height: int = 360, fps: int = 30):
        self.width = width
        self.height = height
        self.fps = fps
        self._x = width // 2
        self._y = height // 2
        self._vx = 3.0
        self._vy = 2.5
        self._face_w = 80
        self._face_h = 100
        self._open = True

    def isOpened(self) -> bool:
        return self._open

    def release(self) -> None:
        self._open = False
        logger.info("MockCamera released.")

    def read(self) -> tuple[bool, Optional[np.ndarray]]:
        if not self._open:
            return False, None
        
        # Create frame
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[:, :] = (40, 40, 50)  # Dark background

        # Move "face" rectangle
        self._x += self._vx
        self._y += self._vy

        if self._x <= self._face_w // 2 or self._x >= self.width - self._face_w // 2:
            self._vx *= -1
        if self._y <= self._face_h // 2 or self._y >= self.height - self._face_h // 2:
            self._vy *= -1

        # Draw face-like rectangle
        x1 = int(self._x - self._face_w // 2)
        y1 = int(self._y - self._face_h // 2)
        x2 = int(self._x + self._face_w // 2)
        y2 = int(self._y + self._face_h // 2)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (180, 210, 240), -1)  # filled
        cv2.rectangle(frame, (x1, y1), (x2, y2), (140, 180, 220), 2)   # border
        # "eyes"
        eye_y = int(y1 + self._face_h * 0.35)
        cv2.circle(frame, (int(x1 + self._face_w * 0.3), eye_y), 5, (30, 30, 30), -1)
        cv2.circle(frame, (int(x1 + self._face_w * 0.7), eye_y), 5, (30, 30, 30), -1)

        # Delay to match FPS
        time.sleep(1.0 / self.fps)
        return True, frame


# ────────────────────────────────────────────────
# GStreamer Pipeline Builder
# ────────────────────────────────────────────────

def jetson_csi_pipeline(cfg: VisionConfig) -> str:
    """Optimized GStreamer pipeline for Jetson CSI cameras using nvarguscamerasrc."""
    return (
        f"nvarguscamerasrc sensor-id={cfg.sensor_id} ! "
        f"video/x-raw(memory:NVMM), width={cfg.capture_width}, "
        f"height={cfg.capture_height}, format=NV12, "
        f"framerate={cfg.framerate}/1 ! "
        f"nvvidconv flip-method={cfg.flip_method} ! "
        f"video/x-raw, width={cfg.display_width}, "
        f"height={cfg.display_height}, format=BGRx ! "
        f"videoconvert ! video/x-raw, format=BGR ! "
        f"appsink drop=True max-buffers=1"
    )


def usb_v4l2_pipeline(cfg: VisionConfig) -> str:
    """Standard V4L2 pipeline for USB webcams."""
    return (
        f"v4l2src device=/dev/video{cfg.usb_device} ! "
        f"video/x-raw, width={cfg.display_width}, height={cfg.display_height}, "
        f"format=YUY2, framerate={cfg.framerate}/1 ! "
        f"videoconvert ! video/x-raw, format=BGR ! "
        f"appsink drop=True max-buffers=1"
    )


# ────────────────────────────────────────────────
# Vision Node
# ────────────────────────────────────────────────

class VisionNode:
    """Captures frames, runs face tracking, publishes corrections to ZMQ bus."""

    def __init__(self, cfg: VisionConfig, mode: str = "auto"):
        self.cfg = cfg
        self._mode = mode
        self._running = False
        self._track_mode: str = "idle"
        self._last_track_time: float = 0.0
        self._frame: Optional[np.ndarray] = None
        self._lock = threading.RLock()

        # Face detector
        self._face_cascade = cv2.CascadeClassifier(cfg.face_cascade_path)
        if self._face_cascade.empty():
            logger.warning("Haar cascade not found at configured path; face detection disabled.")

        # ZMQ
        self._ctx = zmq.Context.instance()
        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.setsockopt(zmq.SNDHWM, 10)
        self._pub.bind(cfg.zmq_pub_uri)

        self._sub = self._ctx.socket(zmq.SUB)
        self._sub.connect(cfg.zmq_sub_uri)
        self._sub.setsockopt_string(zmq.SUBSCRIBE, "/voice/intent")
        self._sub.setsockopt_string(zmq.SUBSCRIBE, "/sys/cmd")

        self._capture_thread: Optional[threading.Thread] = None
        self._zmq_thread: Optional[threading.Thread] = None

        logger.info(f"VisionNode init: mode={mode} pub={cfg.zmq_pub_uri} sub={cfg.zmq_sub_uri}")

    # ── Public API ───────────────────────────────

    def start(self) -> None:
        self._running = True
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        self._zmq_thread = threading.Thread(target=self._zmq_listener_loop, daemon=True)
        self._zmq_thread.start()
        logger.info("VisionNode started.")

    def stop(self) -> None:
        self._running = False
        for t in (self._capture_thread, self._zmq_thread):
            if t:
                t.join(timeout=2.0)
        self._pub.close()
        self._sub.close()
        logger.info("VisionNode stopped.")

    @property
    def latest_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            return self._frame.copy() if self._frame is not None else None

    def set_track_mode(self, mode: str) -> None:
        assert mode in {"idle", "tracking", "calibrating"}
        self._track_mode = mode
        logger.info(f"Track mode set → {mode}")

    # ── Capture Thread ───────────────────────────

    def _capture_loop(self) -> None:
        cap = self._open_camera()
        if cap is None:
            logger.error("Camera failed to open. VisionNode cannot run.")
            return

        frame_counter = 0
        try:
            while self._running:
                ret, frame = cap.read()
                if not ret or frame is None:
                    logger.warning("Frame read failed — retrying in 100 ms.")
                    time.sleep(0.1)
                    continue

                with self._lock:
                    self._frame = frame

                frame_counter += 1

                if self._track_mode == "tracking" and not self._face_cascade.empty():
                    target = self._track_face(frame)
                    if target and frame_counter % self.cfg.publish_stride == 0:
                        self._publishTracking(target)

                if time.time() - self._last_track_time > self.cfg.track_lock_timeout_s:
                    target = None
                    if 'target' in locals():
                        target = locals().get('target')
                    if target is None:
                        self._publishNoTarget()

        except Exception:
            logger.error(f"Capture loop crashed:\n{traceback.format_exc()}")
        finally:
            cap.release()
            logger.info("Camera released.")

    # ── ZMQ Listener ─────────────────────────────

    def _zmq_listener_loop(self) -> None:
        poller = zmq.Poller()
        poller.register(self._sub, zmq.POLLIN)
        try:
            while self._running:
                socks = dict(poller.poll(timeout=200))
                if self._sub not in socks:
                    continue
                topic, payload = self._sub.recv_multipart()
                self._handleMessage(topic.decode(), payload)
        except zmq.ZMQError as e:
            if self._running:
                logger.error(f"ZMQ error: {e}")

    def _handleMessage(self, topic: str, payload: bytes) -> None:
        try:
            msg = json.loads(payload.decode())
        except Exception:
            logger.warning(f"Malformed JSON on {topic}")
            return

        if topic == "/voice/intent" and msg.get("intent") == "calibrate":
            logger.info("Voice intent → calibrate received on VisionNode")
            self._publishStatus("calibrating", source="voice")
            self.set_track_mode("calibrating")

        elif topic == "/sys/cmd":
            cmd = msg.get("cmd")
            if cmd == "tracking_on":
                self.set_track_mode("tracking")
                self._publishStatus("tracking", source="system")
            elif cmd == "tracking_off":
                self.set_track_mode("idle")
                self._publishStatus("idle", source="system")
            elif cmd == "awb_lock":
                logger.info("AWB lock requested (camera-dependent implementation)")

    # ── Tracking ─────────────────────────────────

    def _track_face(self, frame: np.ndarray) -> Optional[dict]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self._face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40)
        )
        if len(faces) == 0:
            return None

        best = max(faces, key=lambda fc: fc[2] * fc[3])
        x, y, w, h = best
        cx, cy = x + w // 2, y + h // 2
        fh, fw = frame.shape[:2]

        offset_x = (cx / fw - 0.5) * 2.0
        offset_y = (cy / fh - 0.5) * 2.0
        area_ratio = (w * h) / (fw * fh)
        confidence = min(1.0, area_ratio * 10 + 0.3)

        self._last_track_time = time.time()

        return {
            "timestamp": time.time(),
            "target": "face",
            "offset_x": round(offset_x, 3),
            "offset_y": round(offset_y, 3),
            "confidence": round(confidence, 3),
            "frame_w": fw,
            "frame_h": fh,
        }

    # ── Publishing ───────────────────────────────

    def _publishTracking(self, target: dict) -> None:
        self._pub.send_multipart([TOPIC_TRACKING, json.dumps(target).encode()])

    def _publishNoTarget(self) -> None:
        payload = {
            "timestamp": time.time(), "target": "none",
            "offset_x": 0.0, "offset_y": 0.0, "confidence": 0.0,
            "frame_w": self.cfg.display_width, "frame_h": self.cfg.display_height
        }
        self._pub.send_multipart([TOPIC_TRACKING, json.dumps(payload).encode()])

    def _publishStatus(self, state: str, source: str) -> None:
        payload = {"state": state, "source": source, "timestamp": time.time()}
        self._pub.send_multipart([TOPIC_STATUS, json.dumps(payload).encode()])

    # ── Camera Selection ─────────────────────────

    def _open_camera(self):
        """Returns a cv2.VideoCapture, MockCamera, or None."""
        if self._mode == "mock":
            logger.info("Using MOCK camera (synthetic bouncing target).")
            return MockCamera(self.cfg.display_width, self.cfg.display_height, self.cfg.framerate)

        if self._mode in ("csi", "auto"):
            cap = self._try_csi()
            if cap is not None:
                return cap
            if self._mode == "csi":
                logger.error("CSI mode requested but nvarguscamerasrc failed.")
                return None

        if self._mode in ("usb", "auto"):
            cap = self._try_usb()
            if cap is not None:
                return cap
            if self._mode == "usb":
                logger.error(f"USB mode requested but /dev/video{self.cfg.usb_device} not available.")
                return None

        if self._mode == "auto":
            logger.warning("No real camera found. Falling back to MOCK mode for development.")
            return MockCamera(self.cfg.display_width, self.cfg.display_height, self.cfg.framerate)

        return None

    def _try_csi(self) -> Optional[cv2.VideoCapture]:
        gst = jetson_csi_pipeline(self.cfg)
        logger.info(f"Trying CSI pipeline: {gst[:80]}...")
        cap = cv2.VideoCapture(gst, cv2.CAP_GSTREAMER)
        if cap.isOpened():
            # Verify by reading one frame
            ret, _ = cap.read()
            if ret:
                logger.info(f"CSI camera active: {self.cfg.capture_width}x{self.cfg.capture_height}@{self.cfg.framerate}fps")
                return cap
            else:
                logger.warning("CSI pipeline opened but cannot read frames.")
                cap.release()
        else:
            logger.warning("CSI pipeline could not open (nvarguscamerasrc not available or no sensor).")
        return None

    def _try_usb(self) -> Optional[cv2.VideoCapture]:
        dev_path = f"/dev/video{self.cfg.usb_device}"
        if not (cv2.os.path.exists(dev_path) if hasattr(cv2, 'os') else __import__('os').path.exists(dev_path)):
            return None

        gst = usb_v4l2_pipeline(self.cfg)
        logger.info(f"Trying USB pipeline: {gst[:80]}...")
        cap = cv2.VideoCapture(gst, cv2.CAP_GSTREAMER)
        if not cap.isOpened():
            # Fallback to direct V4L2 backend
            cap = cv2.VideoCapture(self.cfg.usb_device)

        if cap.isOpened():
            ret, _ = cap.read()
            if ret:
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                logger.info(f"USB camera active: {w}x{h}")
                return cap
            cap.release()
        logger.warning(f"USB camera /dev/video{self.cfg.usb_device} not accessible.")
        return None


# ────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Teela Vision Node v2")
    parser.add_argument("--mode", choices=["csi", "usb", "mock", "auto"], default="auto",
                        help="Camera mode: csi=Jetson CSI, usb=V4L2 webcam, mock=synthetic, auto=detect")
    parser.add_argument("--zmq-pub", default="tcp://127.0.0.1:5556", help="ZMQ pub endpoint")
    parser.add_argument("--zmq-sub", default="tcp://127.0.0.1:5555", help="ZMQ sub endpoint")
    parser.add_argument("--sensor-id", type=int, default=0, help="CSI sensor ID (0=CAM0, 1=CAM1)")
    parser.add_argument("--usb-device", type=int, default=0, help="USB video device number (/dev/videoN)")
    parser.add_argument("--width", type=int, default=1280, help="Capture width")
    parser.add_argument("--height", type=int, default=720, help="Capture height")
    parser.add_argument("--display", action="store_true", help="Show OpenCV window (debug)")
    args = parser.parse_args()

    cfg = VisionConfig(
        capture_width=args.width,
        capture_height=args.height,
        zmq_pub_uri=args.zmq_pub,
        zmq_sub_uri=args.zmq_sub,
        sensor_id=args.sensor_id,
        usb_device=args.usb_device,
    )

    node = VisionNode(cfg, mode=args.mode)
    node.start()
    node.set_track_mode("tracking")

    logger.info(
        f"VisionNode running. Mode: {args.mode}  "
        f"Display: {'ON' if args.display else 'OFF'}  "
        f"Track: tracking"
    )

    try:
        if args.display:
            cv2.namedWindow("Teela Vision", cv2.WINDOW_AUTOSIZE)
            while True:
                frame = node.latest_frame
                if frame is not None:
                    cv2.imshow("Teela Vision", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
        else:
            import signal
            signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()
        if args.display:
            cv2.destroyAllWindows()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
