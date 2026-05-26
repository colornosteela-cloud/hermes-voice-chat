#!/usr/bin/env python3
"""
Teela Vision Node — VisionKinematicSyncSkill
==============================================
CSI camera capture via GStreamer on NVIDIA Jetson,
face/object tracking, and ZMQ bus integration.

Hardware: Jetson Nano/Orin + CSI IMX219/IMX477
Bus:      ZeroMQ (Mini-ROS pattern)

Usage:
    python VisionNode.py [--zmq-endpoint tcp://localhost:5555] [--display]
"""
from __future__ import annotations

import argparse
import logging
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Optional

import cv2
import numpy as np
import zmq
import json
from cv2.dnn import DNN_TARGET_CUDA

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("TeelaVision")


# ────────────────────────────────────────────────
# ZMQ Message Schemas (shared with other nodes)
# ────────────────────────────────────────────────

TRACKING_SCHEMA = {
    "topic": "/vision/tracking",
    "fields": {
        "timestamp": float,         # Unix timestamp
        "target": str,              # e.g. "face", "none"
        "offset_x": float,          # Normalized -1..1, negative = left
        "offset_y": float,          # Normalized -1..1, negative = up
        "confidence": float,        # 0.0 .. 1.0
        "frame_w": int,             # Native frame width
        "frame_h": int,             # Native frame height
    },
}

STATUS_SCHEMA = {
    "topic": "/sys/status",
    "fields": {"state": str, "source": str},
}


# ────────────────────────────────────────────────
# Hardware Configuration
# ────────────────────────────────────────────────

@dataclass
class VisionConfig:
    """Tunable camera + ZMQ parameters."""
    # CSI GStreamer pipeline for Jetson
    capture_width: int = 1280
    capture_height: int = 720
    display_width: int = 640
    display_height: int = 360
    framerate: int = 30
    flip_method: int = 0          # 0=none, 2=rotate180 (depends on mount)
    # ZMQ
    zmq_pub_uri: str = "tcp://127.0.0.1:5556"
    zmq_sub_uri: str = "tcp://127.0.0.1:5555"
    # Tracking
    face_cascade_path: str = "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"
    track_lock_timeout_s: float = 2.0
    # Performance
    publish_stride: int = 3       # Publish every Nth frame (reduce bus load)


# ────────────────────────────────────────────────
# GStreamer Pipeline Builder (Jetson optimized)
# ────────────────────────────────────────────────

def jetson_csi_pipeline(cfg: VisionConfig) -> str:
    """Return a GStreamer pipeline string for Jetson CSI cameras.

    Uses nvarguscamerasrc for zero-copy NV12 → RGBA conversion
    via the Jetson ISP/VIC hardware pipeline.
    """
    return (
        f"nvarguscamerasrc ! "
        f"video/x-raw(memory:NVMM), width={cfg.capture_width}, "
        f"height={cfg.capture_height}, format=NV12, "
        f"framerate={cfg.framerate}/1 ! "
        f"nvvidconv flip-method={cfg.flip_method} ! "
        f"video/x-raw, width={cfg.display_width}, "
        f"height={cfg.display_height}, format=BGRx ! "
        f"videoconvert ! video/x-raw, format=BGR ! "
        f"appsink drop=True max-buffers=1"
    )


# ────────────────────────────────────────────────
# Vision Node (Producer + Consumer)
# ────────────────────────────────────────────────

class VisionNode:
    """Captures CSI frames, runs face tracking, publishes corrections
    to the ZMQ bus, and listens for voice-triggered state changes."""

    def __init__(self, cfg: VisionConfig):
        self.cfg = cfg
        self._running = False
        self._mode: str = "idle"            # idle | tracking | calibrating
        self._last_track_time: float = 0.0
        self._frame: Optional[np.ndarray] = None
        self._lock = threading.RLock()

        # Face detector (Haar = fast, swap for DNN if accuracy needed)
        self._face_cascade = cv2.CascadeClassifier(cfg.face_cascade_path)
        if self._face_cascade.empty():
            logger.warning("Haar cascade not found; face detection disabled.")

        # ZMQ sockets (independent threads safe with zmq.Socket)
        self._ctx = zmq.Context.instance()
        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.setsockopt(zmq.SNDHWM, 10)
        self._pub.bind(cfg.zmq_pub_uri)

        self._sub = self._ctx.socket(zmq.SUB)
        self._sub.connect(cfg.zmq_sub_uri)
        self._sub.setsockopt_string(zmq.SUBSCRIBE, "/voice/intent")
        self._sub.setsockopt_string(zmq.SUBSCRIBE, "/sys/cmd")

        # Frame consumer thread
        self._capture_thread: Optional[threading.Thread] = None
        self._zmq_thread: Optional[threading.Thread] = None

        logger.info(f"VisionNode init: pub={cfg.zmq_pub_uri} sub={cfg.zmq_sub_uri}")

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

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        assert mode in {"idle", "tracking", "calibrating"}
        self._mode = mode
        logger.info(f"Mode set → {mode}")

    # ── Capture Thread ───────────────────────────

    def _capture_loop(self) -> None:
        """Main acquisition + tracking loop. Runs on its own thread."""
        cap = self._open_camera()
        if cap is None:
            logger.error("Camera failed to open. VisionNode capture thread exiting.")
            return

        frame_counter = 0
        try:
            while self._running:
                ret, frame = cap.read()
                if not ret or frame is None:
                    logger.warning("Frame read failed — retrying in 100 ms.")
                    time.sleep(0.1)
                    continue

                # Update shared frame
                with self._lock:
                    self._frame = frame

                frame_counter += 1

                # Tracking logic (only if not in calibrating)
                if self._mode == "tracking" and not self._face_cascade.empty():
                    target = self._track_face(frame)
                    if target and frame_counter % self.cfg.publish_stride == 0:
                        self._publish_tracking(target)

                # If no recent track → target lost
                if time.time() - self._last_track_time > self.cfg.track_lock_timeout_s:
                    if target is None:
                        self._publish_no_target()

        except Exception:
            logger.error(f"Capture loop crashed:\n{traceback.format_exc()}")
        finally:
            cap.release()
            logger.info("Camera released.")

    # ── ZMQ Listener Thread ──────────────────────

    def _zmq_listener_loop(self) -> None:
        """Listens for voice intents + system commands."""
        poller = zmq.Poller()
        poller.register(self._sub, zmq.POLLIN)
        try:
            while self._running:
                socks = dict(poller.poll(timeout=200))  # ms
                if self._sub not in socks:
                    continue
                topic, payload = self._sub.recv_multipart()
                topic = topic.decode()
                self._handle_message(topic, payload)
        except zmq.ZMQError as e:
            if self._running:
                logger.error(f"ZMQ error: {e}")

    def _handle_message(self, topic: str, payload: bytes) -> None:
        try:
            import json
            msg = json.loads(payload.decode())
        except Exception:
            logger.warning(f"Malformed JSON on {topic}")
            return

        if topic == "/voice/intent" and msg.get("intent") == "calibrate":
            logger.info("Voice intent → calibrate received on VisionNode")
            self._publish_status("calibrating", source="voice")
            self.set_mode("calibrating")

        elif topic == "/sys/cmd" and msg.get("cmd") == "tracking_on":
            self.set_mode("tracking")
            self._publish_status("tracking", source="system")

        elif topic == "/sys/cmd" and msg.get("cmd") == "tracking_off":
            self.set_mode("idle")
            self._publish_status("idle", source="system")

    # ── Tracking ─────────────────────────────────

    def _track_face(self, frame: np.ndarray) -> Optional[dict]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self._face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60)
        )
        if len(faces) == 0:
            return None

        # Pick the largest face (closest to camera)
        best = max(faces, key=lambda fc: fc[2] * fc[3])
        x, y, w, h = best
        cx, cy = x + w // 2, y + h // 2
        fh, fw = frame.shape[:2]

        # Normalize offsets (-1..1)
        offset_x = (cx / fw - 0.5) * 2.0
        offset_y = (cy / fh - 0.5) * 2.0
        area_ratio = (w * h) / (fw * fh)
        confidence = min(1.0, area_ratio * 10)

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

    def _publish_tracking(self, target: dict) -> None:
        self._pub.send_multipart([
            b"/vision/tracking",
            json.dumps(target).encode(),
        ])

    def _publish_no_target(self) -> None:
        payload = {"timestamp": time.time(), "target": "none",
                   "offset_x": 0.0, "offset_y": 0.0, "confidence": 0.0,
                   "frame_w": self.cfg.display_width, "frame_h": self.cfg.display_height}
        self._pub.send_multipart([b"/vision/tracking", json.dumps(payload).encode()])

    def _publish_status(self, state: str, source: str) -> None:
        payload = {"state": state, "source": source, "timestamp": time.time()}
        self._pub.send_multipart([b"/sys/status", json.dumps(payload).encode()])

    # ── Helpers ──────────────────────────────────

    def _open_camera(self) -> Optional[cv2.VideoCapture]:
        """Attempts CSI pipeline, falls back to V4L2 /dev/video0."""
        gst = jetson_csi_pipeline(self.cfg)
        logger.info(f"Opening GStreamer pipeline:\n  {gst}")
        cap = cv2.VideoCapture(gst, cv2.CAP_GSTREAMER)
        if cap.isOpened():
            logger.info(f"CSI camera opened: {self.cfg.capture_width}x{self.cfg.capture_height}@{self.cfg.framerate}fps")
            return cap

        # Fallback: standard V4L2 (e.g., USB webcam or IMX219 via V4L2)
        logger.warning("CSI pipeline failed. Falling back to /dev/video0 (V4L2).")
        cap = cv2.VideoCapture(0)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.display_width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.display_height)
            logger.info("Fallback V4L2 camera opened.")
            return cap

        logger.error("No camera available.")
        return None


# ────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────

def main():
    import json

    parser = argparse.ArgumentParser(description="Teela Vision Node")
    parser.add_argument("--zmq-pub", default="tcp://127.0.0.1:5556", help="ZMQ pub endpoint")
    parser.add_argument("--zmq-sub", default="tcp://127.0.0.1:5555", help="ZMQ sub endpoint")
    parser.add_argument("--width", type=int, default=1280, help="Capture width")
    parser.add_argument("--height", type=int, default=720, help="Capture height")
    parser.add_argument("--display", action="store_true", help="Show OpenCV window (debug)")
    args = parser.parse_args()

    cfg = VisionConfig(
        capture_width=args.width,
        capture_height=args.height,
        zmq_pub_uri=args.zmq_pub,
        zmq_sub_uri=args.zmq_sub,
    )

    node = VisionNode(cfg)
    node.start()
    node.set_mode("tracking")  # Default to tracking mode

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
