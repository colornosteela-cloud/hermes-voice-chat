#!/usr/bin/env python3
"""
Teela Actuation Node — VisionKinematicSyncSkill
================================================
Pan/Tilt servo control for 2-axis robotic head.
Handles relative + absolute movement with eased interpolation.

Hardware: PCA9685 16-ch PWM via I2C (Jetson GPIO)
Bus:      ZeroMQ (Mini-ROS pattern)

Usage:
    python ActuationNode.py [--zmq-endpoint tcp://localhost:5555] [--pan-pin 0] [--tilt-pin 1]
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
from typing import Callable, Dict, Optional

import zmq

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("TeelaActuation")


# ────────────────────────────────────────────────
# Interpolation Utilities
# ────────────────────────────────────────────────

def ease_out_cubic(t: float) -> float:
    """Easing: fast start, gentle landing. t in [0,1]."""
    return 1 - pow(1 - t, 3)

def ease_in_out_sine(t: float) -> float:
    """Smooth S-curve easing. t in [0,1]."""
    return -(math.cos(math.pi * t) - 1) / 2


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


# ────────────────────────────────────────────────
# Servo Config
# ────────────────────────────────────────────────

@dataclass
class ServoConfig:
    # Hardware
    i2c_address: int = 0x40
    pca_frequency: int = 50          # Hz — standard servo PWM freq
    pan_pin: int = 0
    tilt_pin: int = 1
    # Limits (degrees) — clamped for mechanical safety
    pan_min_deg: float = -90.0
    pan_max_deg: float = 90.0
    tilt_min_deg: float = -45.0
    tilt_max_deg: float = 45.0
    # Smoothing
    default_ease_fn: Callable[[float], float] = ease_out_cubic
    min_step_time_s: float = 0.016   # ~60 Hz inner loop
    # ZMQ
    zmq_sub_uri: str = "tcp://127.0.0.1:5555"
    zmq_pub_uri: str = "tcp://127.0.0.1:5557"


# ────────────────────────────────────────────────
# Fake Servo (for dev / headless testing)
# ────────────────────────────────────────────────

class FakeServo:
    """Silently mirrors what a real PCA9685 servo would do."""
    def __init__(self, pin: int, name: str):
        super().__setattr__("pin", pin)
        super().__setattr__("name", name)
        super().__setattr__("angle", 0.0)

    def setPulse(self, micros: int):
        # Map 1000-2000 µs → -90..+90 roughly; just store for debug
        val = (micros - 1500) / 500 * 45
        super().__setattr__("angle", val)
    
    def __setattr__(self, name, value):
        if name == "angle":
            super().__setattr__("angle", value)
            logger.debug(f"[{self.name}] set to {value:.1f}°")
        else:
            super().__setattr__(name, value)

    def __setitem__(self, key, value):
        """Allow kit.servo[n].angle = x style assignment."""
        if key == "angle":
            self.__setattr__("angle", value)
        else:
            super().__setattr__(key, value)

# ────────────────────────────────────────────────
# Actuation Node
# ────────────────────────────────────────────────

class ActuationNode:
    """Subscribes to motor commands on ZMQ and executes smooth servo motion."""

    def __init__(self, cfg: ServoConfig):
        self.cfg = cfg
        self._running = False
        self._lock = threading.RLock()

        # Current target + actual position tracking
        self._pan_target: float = 0.0
        self._tilt_target: float = 0.0
        self._pan_current: float = 0.0
        self._tilt_current: float = 0.0
        self._move_start_time: float = 0.0
        self._move_duration_s: float = 0.0
        self._move_start_pan: float = 0.0
        self._move_start_tilt: float = 0.0
        self._easing_fn: Callable[[float], float] = cfg.default_ease_fn
        self._active_move: bool = False

        # Hardware init
        self._pan_servo: Optional[object] = None
        self._tilt_servo: Optional[object] = None
        self._init_hardware()

        # ZMQ
        self._ctx = zmq.Context.instance()
        self._sub = self._ctx.socket(zmq.SUB)
        self._sub.connect(cfg.zmq_sub_uri)
        self._sub.setsockopt_string(zmq.SUBSCRIBE, "/motor/move_absolute")
        self._sub.setsockopt_string(zmq.SUBSCRIBE, "/motor/move_relative")
        self._sub.setsockopt_string(zmq.SUBSCRIBE, "/vision/tracking")
        self._sub.setsockopt_string(zmq.SUBSCRIBE, "/sys/cmd")

        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.setsockopt(zmq.SNDHWM, 10)
        self._pub.bind(cfg.zmq_pub_uri)

        # Threads
        self._zmq_thread: Optional[threading.Thread] = None
        self._motion_thread: Optional[threading.Thread] = None

        logger.info(
            f"ActuationNode init: pan_pin={cfg.pan_pin} tilt_pin={cfg.tilt_pin} "
            f"safe_pan=[{cfg.pan_min_deg},{cfg.pan_max_deg}] "
            f"safe_tilt=[{cfg.tilt_min_deg},{cfg.tilt_max_deg}]"
        )

    # ── Public API ───────────────────────────────

    def start(self) -> None:
        self._running = True
        self._motion_thread = threading.Thread(target=self._motion_loop, daemon=True)
        self._motion_thread.start()
        self._zmq_thread = threading.Thread(target=self._zmq_listener_loop, daemon=True)
        self._zmq_thread.start()
        logger.info("ActuationNode started.")

    def stop(self) -> None:
        self._running = False
        for t in (self._motion_thread, self._zmq_thread):
            if t:
                t.join(timeout=2.0)
        self._sub.close()
        self._pub.close()
        if hasattr(self, "_pca") and self._pca:
            self._pca.deinit()
        logger.info("ActuationNode stopped.")

    @property
    def current_position(self) -> Dict[str, float]:
        with self._lock:
            return {"pan": self._pan_current, "tilt": self._tilt_current}

    # ── Hardware Init ────────────────────────────

    def _init_hardware(self) -> None:
        """Initialize PCA9685 via direct I2C driver (bypasses broken Jetson.GPIO)."""
        try:
            from utils.pca9685_driver import PCA9685
            self._pca = PCA9685(bus=7, address=0x40, freq=50)
            self._real_hardware = True
            logger.info("PCA9685 servo driver initialized via direct I2C (smbus2).")
        except Exception as e:
            logger.warning(f"PCA9685 direct I2C failed: {e}. Using FakeServo.")
            self._pca = None
            self._real_hardware = False

    # ── Motion Thread (interpolation loop) ───────

    def _motion_loop(self) -> None:
        """Inner control loop ~60 Hz. Interpolates between current and target."""
        while self._running:
            t_start = time.monotonic()

            with self._lock:
                if self._active_move:
                    elapsed = time.monotonic() - self._move_start_time
                    progress = min(1.0, elapsed / self._move_duration_s)
                    eased = self._easing_fn(progress)

                    new_pan = lerp(self._move_start_pan, self._pan_target, eased)
                    new_tilt = lerp(self._move_start_tilt, self._tilt_target, eased)

                    self._pan_current = new_pan
                    self._tilt_current = new_tilt

                    # Clamp to physical limits
                    self._pan_current = max(self.cfg.pan_min_deg, min(self.cfg.pan_max_deg, self._pan_current))
                    self._tilt_current = max(self.cfg.tilt_min_deg, min(self.cfg.tilt_max_deg, self._tilt_current))

                    self._write_hardware()

                    if progress >= 1.0:
                        self._active_move = False
                        self._publish_position()
                else:
                    # Idle hold — tiny dead-band write to prevent servo jitter
                    pass

            # Maintain loop rate
            elapsed = time.monotonic() - t_start
            sleep_for = max(0, self.cfg.min_step_time_s - elapsed)
            time.sleep(sleep_for)

    def _write_hardware(self) -> None:
        """Send current pan/tilt angles to PCA9685."""
        if not self._real_hardware or self._pca is None:
            return
        # Map logical -90..+90 → servo-native 0..180
        pan_native = self._pan_current + 90.0
        tilt_native = self._tilt_current + 90.0
        self._pca.set_servo_angle(self.cfg.pan_pin, pan_native)
        self._pca.set_servo_angle(self.cfg.tilt_pin, tilt_native)
        logger.debug(f"Servo write: pan={pan_native:.1f}° tilt={tilt_native:.1f}°")

    def _logical_to_native(self, deg: float) -> float:
        """Map logical range (-90..+90) to servo-native (0..180)."""
        return clamp(deg + 90.0, 0.0, 180.0)

    # ── ZMQ Listener ─────────────────────────────

    def _zmq_listener_loop(self) -> None:
        poller = zmq.Poller()
        poller.register(self._sub, zmq.POLLIN)
        try:
            while self._running:
                socks = dict(poller.poll(timeout=100))
                if self._sub not in socks:
                    continue
                topic, payload = self._sub.recv_multipart()
                self._handle_message(topic.decode(), payload)
        except zmq.ZMQError as e:
            if self._running:
                logger.error(f"ZMQ listener error: {e}")

    def _handle_message(self, topic: str, payload: bytes) -> None:
        try:
            msg = json.loads(payload.decode())
        except Exception:
            logger.warning(f"Malformed JSON on {topic}")
            return

        if topic == "/motor/move_absolute":
            self._cmd_absolute(msg)
        elif topic == "/motor/move_relative":
            self._cmd_relative(msg)
        elif topic == "/vision/tracking":
            self._cmd_tracking(msg)
        elif topic == "/sys/cmd" and msg.get("cmd") == "home":
            self._go_home()

    # ── Command Implementations ────────────────

    def _cmd_absolute(self, msg: dict) -> None:
        pan = msg.get("pan", self._pan_current)
        tilt = msg.get("tilt", self._tilt_current)
        duration_ms = msg.get("duration_ms", 500)
        ease_name = msg.get("ease", "cubic")
        self._begin_move(pan, tilt, duration_ms / 1000.0, ease_name)

    def _cmd_relative(self, msg: dict) -> None:
        d_pan = msg.get("d_pan", 0.0)
        d_tilt = msg.get("d_tilt", 0.0)
        duration_ms = msg.get("duration_ms", 200)
        self._begin_move(
            self._pan_current + d_pan,
            self._tilt_current + d_tilt,
            duration_ms / 1000.0,
            msg.get("ease", "cubic"),
        )

    def _cmd_tracking(self, msg: dict) -> None:
        """Vision node publishes pixel offsets; we convert to small
        relative servo corrections using a proportional tracking gain."""
        if msg.get("target") == "none":
            return
        offset_x = msg.get("offset_x", 0.0)
        offset_y = msg.get("offset_y", 0.0)
        conf = msg.get("confidence", 0.0)
        if conf < 0.3:
            return  # Ignore low-confidence noise

        # Proportional gain tuned for fov — pan/tilt correction per normalized pixel offset
        K_pan = 35.0   # degrees per unit offset
        K_tilt = 25.0
        max_delta = 8.0  # clamp single-frame step to prevent overshoot

        d_pan = clamp(-offset_x * K_pan, -max_delta, max_delta)
        d_tilt = clamp(-offset_y * K_tilt, -max_delta, max_delta)

        self._begin_move(
            self._pan_current + d_pan,
            self._tilt_current + d_tilt,
            0.15,   # 150 ms snap-to tracking
            "sine",
        )

    def _go_home(self) -> None:
        self._begin_move(0.0, 0.0, 1.0, "cubic")

    def _begin_move(self, pan: float, tilt: float, duration_s: float, ease: str) -> None:
        with self._lock:
            self._move_start_pan = self._pan_current
            self._move_start_tilt = self._tilt_current
            self._pan_target = pan
            self._tilt_target = tilt
            self._move_start_time = time.monotonic()
            self._move_duration_s = max(duration_s, 0.05)
            self._easing_fn = self._resolve_ease(ease)
            self._active_move = True
        logger.debug(f"Move: pan={pan:.1f}° tilt={tilt:.1f}° dur={duration_s:.2f}s ease={ease}")

    def _resolve_ease(self, name: str) -> Callable[[float], float]:
        return {"sine": ease_in_out_sine}.get(name, ease_out_cubic)

    def _publish_position(self) -> None:
        payload = {
            "timestamp": time.time(),
            "pan": round(self._pan_current, 2),
            "tilt": round(self._tilt_current, 2),
            "reached": True,
        }
        self._pub.send_multipart([b"/motor/position", json.dumps(payload).encode()])


# ────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Teela Actuation Node")
    parser.add_argument("--zmq-sub", default="tcp://127.0.0.1:5555", help="ZMQ sub endpoint")
    parser.add_argument("--zmq-pub", default="tcp://127.0.0.1:5557", help="ZMQ pub endpoint")
    parser.add_argument("--pan-pin", type=int, default=0, help="PCA9685 channel for pan")
    parser.add_argument("--tilt-pin", type=int, default=1, help="PCA9685 channel for tilt")
    parser.add_argument("--pan-min", type=float, default=-90.0)
    parser.add_argument("--pan-max", type=float, default=90.0)
    parser.add_argument("--tilt-min", type=float, default=-45.0)
    parser.add_argument("--tilt-max", type=float, default=45.0)
    args = parser.parse_args()

    cfg = ServoConfig(
        zmq_sub_uri=args.zmq_sub,
        zmq_pub_uri=args.zmq_pub,
        pan_pin=args.pan_pin,
        tilt_pin=args.tilt_pin,
        pan_min_deg=args.pan_min,
        pan_max_deg=args.pan_max,
        tilt_min_deg=args.tilt_min,
        tilt_max_deg=args.tilt_max,
    )

    node = ActuationNode(cfg)
    node.start()
    node._go_home()

    try:
        import signal
        signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
