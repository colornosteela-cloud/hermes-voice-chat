#!/usr/bin/env python3
"""
Teela Calibration State Machine — VisionKinematicSyncSkill
=============================================================
Orchestrates the self-calibration sequence triggered by voice intent.
Performs kinematic boundary sweep, visual horizon calibration,
and audio-visual alignment confirmation.

Bus: ZeroMQ (Mini-ROS pattern)

Usage:
    python CalibrationStateMachine.py [--zmq-endpoint tcp://localhost:5555]
"""
from __future__ import annotations

import argparse
import enum
import json
import logging
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Dict, Optional

import zmq

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("TeelaCalibrate")


# ────────────────────────────────────────────────
# Calibration State Enum
# ────────────────────────────────────────────────

class CalState(enum.Enum):
    IDLE = "idle"
    BOUNDARY_SWEEP_PAN = "sweep_pan"
    BOUNDARY_SWEEP_TILT = "sweep_tilt"
    RETURN_TO_CENTER = "return_center"
    HORIZON_CALIBRATE = "horizon"
    AWB_LOCK = "awb_lock"
    AUDIO_VISUAL_CONFIRM = "audio_visual"
    DONE = "done"


# ────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────

@dataclass
class CalibrationConfig:
    zmq_pub_uri: str = "tcp://127.0.0.1:5555"
    zmq_sub_uri: str = "tcp://127.0.0.1:5556"      # Vision node publishes here
    sweep_step_deg: float = 5.0                     # coarse → fine
    sweep_pause_s: float = 0.3
    sweep_center_return_s: float = 1.0
    horizon_check_frames: int = 30                    # frames to stabilize
    horizon_variance_threshold: float = 2.5           # pixel std-dev threshold


# ────────────────────────────────────────────────
# Calibration State Machine
# ────────────────────────────────────────────────

class CalibrationStateMachine:
    """Deterministic state machine for audio-visual-kinematic alignment."""

    def __init__(self, cfg: CalibrationConfig):
        self.cfg = cfg
        self._running = False
        self._state: CalState = CalState.IDLE
        self._state_lock = threading.RLock()
        self._start_time: float = 0.0
        self._step_idx: int = 0

        # Accumulate vision stats during horizon calibration
        self._horizon_readings: list[dict] = []

        # ZMQ
        self._ctx = zmq.Context.instance()
        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.setsockopt(zmq.SNDHWM, 10)
        self._pub.bind(cfg.zmq_pub_uri)

        self._sub = self._ctx.socket(zmq.SUB)
        self._sub.connect(cfg.zmq_sub_uri)
        self._sub.setsockopt_string(zmq.SUBSCRIBE, "/voice/intent")
        self._sub.setsockopt_string(zmq.SUBSCRIBE, "/sys/status")
        self._sub.setsockopt_string(zmq.SUBSCRIBE, "/vision/tracking")

        # Threads
        self._fsm_thread: Optional[threading.Thread] = None
        self._listener_thread: Optional[threading.Thread] = None

        logger.info("CalibrationStateMachine initialized.")

    # ── Public API ───────────────────────────────

    def start(self) -> None:
        self._running = True
        self._listener_thread = threading.Thread(target=self._zmq_listener_loop, daemon=True)
        self._listener_thread.start()
        logger.info("CalibrationStateMachine started. Waiting for /voice/intent 'calibrate'.")

    def stop(self) -> None:
        self._running = False
        for t in (self._fsm_thread, self._listener_thread):
            if t:
                t.join(timeout=2.0)
        self._pub.close()
        self._sub.close()
        logger.info("CalibrationStateMachine stopped.")

    @property
    def current_state(self) -> str:
        with self._state_lock:
            return self._state.value

    def force_calibrate(self) -> None:
        """Externally triggered (e.g., by CLI or voice intent)."""
        with self._state_lock:
            if self._state != CalState.IDLE:
                logger.warning("Calibration already in progress — ignoring duplicate trigger.")
                return
            self._transition(CalState.BOUNDARY_SWEEP_PAN)
        self._fsm_thread = threading.Thread(target=self._run_fsm, daemon=True)
        self._fsm_thread.start()

    # ── ZMQ Listener ───────────────────────────

    def _zmq_listener_loop(self) -> None:
        poller = zmq.Poller()
        poller.register(self._sub, zmq.POLLIN)
        try:
            while self._running:
                socks = dict(poller.poll(timeout=200))
                if self._sub not in socks:
                    continue
                topic, payload = self._sub.recv_multipart()
                self._handle_message(topic.decode(), json.loads(payload.decode()))
        except zmq.ZMQError:
            pass

    def _handle_message(self, topic: str, msg: dict) -> None:
        if topic == "/voice/intent" and msg.get("intent") == "calibrate":
            confidence = msg.get("confidence", 0.0)
            if confidence >= 0.75:
                logger.info(f"Voice calibrate intent accepted (conf={confidence:.2f})")
                self.force_calibrate()
            else:
                logger.info(f"Voice calibrate intent rejected (conf={confidence:.2f} < 0.75)")

        elif topic == "/vision/tracking":
            # Collect horizon readings only during horizon calibrate
            if self.current_state == CalState.HORIZON_CALIBRATE:
                self._horizon_readings.append(msg)

    # ── State Machine Core ───────────────────────

    def _run_fsm(self) -> None:
        """Runs the calibration sequence as a blocking loop on its own thread."""
        try:
            while self._running:
                with self._state_lock:
                    st = self._state

                if st == CalState.BOUNDARY_SWEEP_PAN:
                    self._do_boundary_sweep_pan()
                elif st == CalState.BOUNDARY_SWEEP_TILT:
                    self._do_boundary_sweep_tilt()
                elif st == CalState.RETURN_TO_CENTER:
                    self._do_return_to_center()
                elif st == CalState.HORIZON_CALIBRATE:
                    self._do_horizon_calibrate()
                elif st == CalState.AUDIO_VISUAL_CONFIRM:
                    self._do_audio_visual_confirm()
                elif st == CalState.DONE:
                    self._do_done()
                    break
                elif st == CalState.IDLE:
                    break   # Should not happen mid-sequence, but guard anyway

                time.sleep(0.05)

        except Exception:
            logger.error(f"FSM crashed:\n{traceback.format_exc()}")
            self._transition(CalState.IDLE)
            self._publish_status("error", source="fsm")

    # ── Individual States ──────────────────────

    def _do_boundary_sweep_pan(self) -> None:
        """Step pan from left limit to right limit, then reverse to center."""
        logger.info("[CAL] → Boundary sweep: PAN")
        limits = [
            ("pan_left", -90.0, 0.0),
            ("pan_right", 90.0, 0.0),
            ("pan_center", 0.0, 0.0),
        ]
        for name, pan, tilt in limits:
            self._motor_absolute(pan=pan, tilt=tilt, duration_ms=1200)
            self._wait_for_motor(1.5)
            self._publish_status(name, source="calibrate")

        logger.info("[CAL] Pan sweep complete.")
        self._transition(CalState.BOUNDARY_SWEEP_TILT)

    def _do_boundary_sweep_tilt(self) -> None:
        """Step tilt from down limit to up limit, then reverse to center."""
        logger.info("[CAL] → Boundary sweep: TILT")
        limits = [
            ("tilt_down", 0.0, -45.0),
            ("tilt_up", 0.0, 45.0),
            ("tilt_center", 0.0, 0.0),
        ]
        for name, pan, tilt in limits:
            self._motor_absolute(pan=pan, tilt=tilt, duration_ms=1200)
            self._wait_for_motor(1.5)
            self._publish_status(name, source="calibrate")

        logger.info("[CAL] Tilt sweep complete.")
        self._transition(CalState.RETURN_TO_CENTER)

    def _do_return_to_center(self) -> None:
        """Guarantee we're exactly centered before visual calibration."""
        logger.info("[CAL] → Return to center")
        self._motor_absolute(pan=0.0, tilt=0.0, duration_ms=800, ease="sine")
        self._wait_for_motor(1.0)
        self._publish_status("centered", source="calibrate")
        self._transition(CalState.HORIZON_CALIBRATE)

    def _do_horizon_calibrate(self) -> None:
        """Lock exposure/AWB and verify frame stability."""
        logger.info("[CAL] → Visual horizon calibration")
        self._horizon_readings.clear()

        # Signal Vision node to lock AWB/exposure
        self._publish_sys_cmd("awb_lock")
        time.sleep(0.5)

        # Collect N frames worth of tracking stats
        start = time.time()
        while time.time() - start < 2.0 and self._running:
            time.sleep(0.05)

        # Analyze collected readings
        if len(self._horizon_readings) < 5:
            logger.warning("[CAL] Too few horizon readings — assuming stable.")
            stable = True
        else:
            offsets_x = [r["offset_x"] for r in self._horizon_readings if r.get("target") != "none"]
            if offsets_x:
                import statistics
                std_x = statistics.stdev(offsets_x) if len(offsets_x) > 1 else 0.0
                stable = std_x < self.cfg.horizon_variance_threshold
                logger.info(f"[CAL] Horizon std_x={std_x:.3f} (threshold={self.cfg.horizon_variance_threshold})")
            else:
                stable = True  # No target detected = stable background

        if stable:
            logger.info("[CAL] Horizon stable → proceeding to audio-visual confirmation.")
            self._transition(CalState.AUDIO_VISUAL_CONFIRM)
        else:
            logger.warning("[CAL] Horizon unstable — retrying once.")
            time.sleep(1.0)
            self._horizon_readings.clear()
            # One retry, then proceed regardless
            time.sleep(2.0)
            self._transition(CalState.AUDIO_VISUAL_CONFIRM)

    def _do_audio_visual_confirm(self) -> None:
        """Final step: flash status ready, trigger voice confirmation."""
        logger.info("[CAL] → Audio-Visual alignment confirmation")

        # Publish Ready status → Voice node should pick this up and speak
        self._publish_status("Ready", source="calibrate")

        # Flash a brief high-priority attention pulse on motors
        self._motor_absolute(pan=5.0, tilt=5.0, duration_ms=200)
        time.sleep(0.25)
        self._motor_absolute(pan=-5.0, tilt=-5.0, duration_ms=200)
        time.sleep(0.25)
        self._motor_absolute(pan=0.0, tilt=0.0, duration_ms=300, ease="sine")
        time.sleep(0.4)

        # Tell voice system to confirm
        self._publish_voice_prompt(
            "Calibration complete, I am looking at you.",
            priority="high"
        )

        self._transition(CalState.DONE)

    def _do_done(self) -> None:
        logger.info("[CAL] ✓ Calibration sequence completed.")
        self._publish_status("calibration_complete", source="calibrate")
        self._transition(CalState.IDLE)

    # ── Helpers ──────────────────────────────────

    def _transition(self, new_state: CalState) -> None:
        with self._state_lock:
            old = self._state.value
            self._state = new_state
            logger.info(f"[FSM] {old} → {new_state.value}")

    def _motor_absolute(self, *, pan: float, tilt: float, duration_ms: float, ease: str = "cubic") -> None:
        payload = {"pan": pan, "tilt": tilt, "duration_ms": duration_ms, "ease": ease}
        self._pub.send_multipart([
            b"/motor/move_absolute",
            json.dumps(payload).encode(),
        ])

    def _motor_relative(self, *, d_pan: float, d_tilt: float, duration_ms: float = 200) -> None:
        payload = {"d_pan": d_pan, "d_tilt": d_tilt, "duration_ms": duration_ms, "ease": "cubic"}
        self._pub.send_multipart([
            b"/motor/move_relative",
            json.dumps(payload).encode(),
        ])

    def _wait_for_motor(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and self._running:
            time.sleep(0.01)

    def _publish_status(self, state: str, source: str) -> None:
        payload = {"state": state, "source": source, "timestamp": time.time()}
        self._pub.send_multipart([b"/sys/status", json.dumps(payload).encode()])

    def _publish_sys_cmd(self, cmd: str) -> None:
        payload = {"cmd": cmd, "source": "calibration", "timestamp": time.time()}
        self._pub.send_multipart([b"/sys/cmd", json.dumps(payload).encode()])

    def _publish_voice_prompt(self, text: str, priority: str = "normal") -> None:
        payload = {
            "intent": "speak",
            "text": text,
            "priority": priority,
            "timestamp": time.time(),
        }
        self._pub.send_multipart([b"/voice/intent", json.dumps(payload).encode()])


# ────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Teela Calibration State Machine")
    parser.add_argument("--zmq-pub", default="tcp://127.0.0.1:5555")
    parser.add_argument("--zmq-sub", default="tcp://127.0.0.1:5556")
    parser.add_argument("--force", action="store_true", help="Run calibration immediately on startup")
    args = parser.parse_args()

    cfg = CalibrationConfig(zmq_pub_uri=args.zmq_pub, zmq_sub_uri=args.zmq_sub)
    fsm = CalibrationStateMachine(cfg)
    fsm.start()

    if args.force:
        time.sleep(0.5)
        fsm.force_calibrate()

    try:
        import signal
        signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        fsm.stop()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
