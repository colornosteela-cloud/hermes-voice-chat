#!/usr/bin/env python3
"""
Teela ZMQ Message Schemas — VisionKinematicSyncSkill
=======================================================
Shared JSON schemas for all ZeroMQ topics between Voice, Vision,
Motor, and Calibration nodes.

These are documentation + validation helpers. Every message on the
bus is a multipart ZMQ frame: [topic_bytes, json_payload_bytes].

Usage:
    from ZMQ_Schemas import TOPICS, validate_tracking_msg
    msg = {"offset_x": -0.12, "offset_y": 0.05, ...}
    if validate_tracking_msg(msg):
        pub.send_multipart([TOPICS["VISION_TRACKING"], json.dumps(msg).encode()])
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple


# ────────────────────────────────────────────────
# Topic Constants (shared across all nodes)
# ────────────────────────────────────────────────

TOPICS = {
    # ── Voice ──────────────────────────
    "VOICE_INTENT": b"/voice/intent",
    "VOICE_TRANSCRIPT": b"/voice/transcript",
    "VOICE_TTS_STATUS": b"/voice/tts_status",

    # ── Vision ─────────────────────────
    "VISION_TRACKING": b"/vision/tracking",
    "VISION_FRAME": b"/vision/frame_meta",   # thumbnail / tensor metadata only
    "VISION_DETECT": b"/vision/detection",   # bounding boxes, classes

    # ── Motor / Actuation ───────────────
    "MOTOR_MOVE_ABS": b"/motor/move_absolute",
    "MOTOR_MOVE_REL": b"/motor/move_relative",
    "MOTOR_POSITION": b"/motor/position",
    "MOTOR_STOP": b"/motor/stop",

    # ── System ─────────────────────────
    "SYS_STATUS": b"/sys/status",
    "SYS_CMD": b"/sys/cmd",
    "SYS_TELEMETRY": b"/sys/telemetry",

    # ── Calibration ────────────────────
    "CAL_PROGRESS": b"/cal/progress",
    "CAL_RESULT": b"/cal/result",
}


# ────────────────────────────────────────────────
# Schema Definitions (type + constraints)
# ────────────────────────────────────────────────

VISION_TRACKING_SCHEMA = {
    "topic": "/vision/tracking",
    "description": "Published by VisionNode when a tracked target is detected.",
    "required": ["timestamp", "target", "offset_x", "offset_y", "confidence",
                 "frame_w", "frame_h"],
    "fields": {
        "timestamp": {"type": float, "desc": "Unix timestamp (seconds)"},
        "target": {"type": str, "desc": "e.g. 'face', 'none', 'object:<class_id>'"},
        "offset_x": {"type": float, "min": -1.0, "max": 1.0,
                     "desc": "Normalized horizontal offset from frame center (-1=left, +1=right)"},
        "offset_y": {"type": float, "min": -1.0, "max": 1.0,
                     "desc": "Normalized vertical offset from frame center (-1=up, +1=down)"},
        "confidence": {"type": float, "min": 0.0, "max": 1.0, "desc": "Detection confidence"},
        "frame_w": {"type": int, "desc": "Native frame width in pixels"},
        "frame_h": {"type": int, "desc": "Native frame height in pixels"},
    },
}

MOTOR_MOVE_ABS_SCHEMA = {
    "topic": "/motor/move_absolute",
    "description": "Command actuation node to move servos to absolute angles.",
    "required": ["pan", "tilt", "duration_ms"],
    "fields": {
        "pan": {"type": float, "min": -90.0, "max": 90.0,
                "desc": "Absolute pan angle in degrees"},
        "tilt": {"type": float, "min": -90.0, "max": 90.0,
                 "desc": "Absolute tilt angle in degrees"},
        "duration_ms": {"type": (int, float), "min": 50, "desc": "Motion duration in milliseconds"},
        "ease": {"type": str, "default": "cubic",
                 "desc": "Easing curve: 'cubic' | 'sine' | 'linear'", "required": False},
    },
}

MOTOR_MOVE_REL_SCHEMA = {
    "topic": "/motor/move_relative",
    "description": "Command incremental servo offset from current position.",
    "required": ["d_pan", "d_tilt"],
    "fields": {
        "d_pan": {"type": float,
                  "desc": "Relative pan delta in degrees (added to current)"},
        "d_tilt": {"type": float,
                   "desc": "Relative tilt delta in degrees (added to current)"},
        "duration_ms": {"type": (int, float), "default": 200,
                         "desc": "Motion duration in milliseconds", "required": False},
        "speed_degps": {"type": float, "default": 30.0,
                        "desc": "Maximum speed in deg/s", "required": False},
        "ease": {"type": str, "default": "cubic", "desc": "Easing curve", "required": False},
    },
}

MOTOR_POSITION_SCHEMA = {
    "topic": "/motor/position",
    "description": "Published by ActuationNode after a move completes.",
    "required": ["timestamp", "pan", "tilt", "reached"],
    "fields": {
        "timestamp": {"type": float, "desc": "Unix timestamp"},
        "pan": {"type": float, "desc": "Current pan angle (logical, not native servo)"},
        "tilt": {"type": float, "desc": "Current tilt angle (logical)"},
        "reached": {"type": bool, "desc": "True if the servo reached the target"},
    },
}

VOICE_INTENT_SCHEMA = {
    "topic": "/voice/intent",
    "description": "Published by Voice node when a speech intent is detected.",
    "required": ["intent", "confidence"],
    "fields": {
        "intent": {"type": str,
                   "desc": "Detected intent: 'calibrate' | 'track' | 'stop' | 'speak' | 'idle'"},
        "confidence": {"type": float, "min": 0.0, "max": 1.0,
                       "desc": "ASR / intent classifier confidence"},
        "text": {"type": str, "required": False, "desc": "Raw transcribed text"},
        "priority": {"type": str, "default": "normal",
                     "desc": "Execution priority: 'low' | 'normal' | 'high' | 'critical'", "required": False},
    },
}

SYS_STATUS_SCHEMA = {
    "topic": "/sys/status",
    "description": "Heartbeat and state broadcast from any node.",
    "required": ["state"],
    "fields": {
        "state": {"type": str,
                  "desc": "Current state: 'idle' | 'Ready' | 'tracking' | 'calibrating' | 'error' | 'pan_left' | ..."},
        "source": {"type": str, "default": "unknown",
                   "desc": "Publishing node: 'voice' | 'vision' | 'motor' | 'calibrate' | 'fsm'", "required": False},
        "timestamp": {"type": float, "required": False, "desc": "Unix timestamp"},
        "details": {"type": dict, "required": False, "desc": "Arbitrary extra metadata"},
    },
}

SYS_CMD_SCHEMA = {
    "topic": "/sys/cmd",
    "description": "Control commands broadcast to all nodes.",
    "required": ["cmd"],
    "fields": {
        "cmd": {"type": str,
                "desc": "Command: 'calibrate' | 'tracking_on' | 'tracking_off' | 'home' | 'awb_lock' | 'emergency_stop' | 'reset'"},
        "source": {"type": str, "default": "unknown", "desc": "Command originator"},
        "timestamp": {"type": float, "required": False},
    },
}

CAL_PROGRESS_SCHEMA = {
    "topic": "/cal/progress",
    "description": "Calibration step-by-step progress notifications.",
    "required": ["step", "total_steps", "label"],
    "fields": {
        "step": {"type": int, "desc": "Current step index (0-based)"},
        "total_steps": {"type": int, "desc": "Total number of steps"},
        "label": {"type": str, "desc": "Human-readable step name"},
        "timestamp": {"type": float, "required": False},
    },
}

CAL_RESULT_SCHEMA = {
    "topic": "/cal/result",
    "description": "Final calibration results (boundary map, camera params, etc).",
    "required": ["success"],
    "fields": {
        "success": {"type": bool},
        "pan_min": {"type": float, "required": False, "desc": "Measured pan min limit"},
        "pan_max": {"type": float, "required": False, "desc": "Measured pan max limit"},
        "tilt_min": {"type": float, "required": False, "desc": "Measured tilt min limit"},
        "tilt_max": {"type": float, "required": False, "desc": "Measured tilt max limit"},
        "horizon_stable": {"type": bool, "required": False},
        "error_msg": {"type": str, "required": False},
        "timestamp": {"type": float, "required": False},
    },
}


# ────────────────────────────────────────────────
# Validation Helpers
# ────────────────────────────────────────────────

class SchemaValidationError(ValueError):
    """Raised when a message fails schema validation."""
    pass


def _check_type(key: str, value: Any, expected: type | Tuple[type, ...]) -> None:
    """Verify a value matches expected type(s)."""
    if isinstance(expected, tuple):
        types = expected
    else:
        types = (expected,)
    if not any(isinstance(value, t) for t in types):
        raise SchemaValidationError(
            f"Field '{key}': expected {types}, got {type(value).__name__}"
        )


def _check_range(key: str, value: Any, schema_field: dict) -> None:
    """Verify numeric ranges if min/max are specified."""
    if not isinstance(value, (int, float)):
        return
    if "min" in schema_field and value < schema_field["min"]:
        raise SchemaValidationError(
            f"Field '{key}': {value} < minimum {schema_field['min']}"
        )
    if "max" in schema_field and value > schema_field["max"]:
        raise SchemaValidationError(
            f"Field '{key}': {value} > maximum {schema_field['max']}"
        )


def validate(msg: dict, schema: dict) -> List[str]:
    """
    Validate a message dict against a schema definition.

    Returns an empty list if valid, otherwise a list of error strings.
    Non-fatal: won't raise on missing optional fields.
    """
    errors: List[str] = []

    # Check required fields
    required = schema.get("required", [])
    for key in required:
        if key not in msg:
            errors.append(f"Missing required field '{key}'")

    # Check types and ranges for present fields
    fields = schema.get("fields", {})
    for key, value in msg.items():
        if key not in fields:
            errors.append(f"Unknown field '{key}'")
            continue
        field_spec = fields[key]
        try:
            _check_type(key, value, field_spec.get("type", object))
        except SchemaValidationError as e:
            errors.append(str(e))
            continue
        try:
            _check_range(key, value, field_spec)
        except SchemaValidationError as e:
            errors.append(str(e))

    return errors


def validate_tracking_msg(msg: dict) -> List[str]:
    return validate(msg, VISION_TRACKING_SCHEMA)


def validate_motor_abs_msg(msg: dict) -> List[str]:
    return validate(msg, MOTOR_MOVE_ABS_SCHEMA)


def validate_motor_rel_msg(msg: dict) -> List[str]:
    return validate(msg, MOTOR_MOVE_REL_SCHEMA)


def validate_voice_intent_msg(msg: dict) -> List[str]:
    return validate(msg, VOICE_INTENT_SCHEMA)


def validate_sys_status_msg(msg: dict) -> List[str]:
    return validate(msg, SYS_STATUS_SCHEMA)


def validate_sys_cmd_msg(msg: dict) -> List[str]:
    return validate(msg, SYS_CMD_SCHEMA)


# ────────────────────────────────────────────────
# Builder Convenience Functions
# ────────────────────────────────────────────────

def build_tracking_msg(
    offset_x: float,
    offset_y: float,
    confidence: float,
    frame_w: int,
    frame_h: int,
    target: str = "face",
) -> dict:
    return {
        "timestamp": time.time(),
        "target": target,
        "offset_x": offset_x,
        "offset_y": offset_y,
        "confidence": confidence,
        "frame_w": frame_w,
        "frame_h": frame_h,
    }


def build_motor_abs_msg(
    pan: float,
    tilt: float,
    duration_ms: float = 500,
    ease: str = "cubic",
) -> dict:
    return {"pan": pan, "tilt": tilt, "duration_ms": duration_ms, "ease": ease}


def build_motor_rel_msg(
    d_pan: float,
    d_tilt: float,
    duration_ms: float = 200,
    speed_degps: float = 30.0,
    ease: str = "cubic",
) -> dict:
    return {
        "d_pan": d_pan,
        "d_tilt": d_tilt,
        "duration_ms": duration_ms,
        "speed_degps": speed_degps,
        "ease": ease,
    }


def build_voice_intent_msg(
    intent: str,
    confidence: float,
    text: Optional[str] = None,
    priority: str = "normal",
) -> dict:
    msg = {"intent": intent, "confidence": confidence, "priority": priority}
    if text is not None:
        msg["text"] = text
    return msg


def build_sys_status_msg(state: str, source: str = "unknown", details: Optional[dict] = None) -> dict:
    msg = {"state": state, "source": source, "timestamp": time.time()}
    if details is not None:
        msg["details"] = details
    return msg


def build_sys_cmd_msg(cmd: str, source: str = "unknown") -> dict:
    return {"cmd": cmd, "source": source, "timestamp": time.time()}


# ────────────────────────────────────────────────
# Example / Self-Test
# ────────────────────────────────────────────────

if __name__ == "__main__":
    import time

    print("=== Teela ZMQ Schema Validation Self-Test ===\n")

    # Valid tracking message
    t_msg = build_tracking_msg(
        offset_x=-0.12, offset_y=0.05, confidence=0.94,
        frame_w=640, frame_h=360
    )
    errs = validate_tracking_msg(t_msg)
    print(f"tracking_msg: {'✅ OK' if not errs else errs}")
    print(f"  {json.dumps(t_msg, indent=2)}\n")

    # Invalid: offset_x out of range
    bad = build_tracking_msg(
        offset_x=1.5, offset_y=0.0, confidence=0.5,
        frame_w=640, frame_h=360
    )
    errs = validate_tracking_msg(bad)
    print(f"bad tracking_msg: {errs}\n")

    # Motor absolute
    m_msg = build_motor_abs_msg(pan=45.0, tilt=-10.0, duration_ms=800, ease="sine")
    errs = validate_motor_abs_msg(m_msg)
    print(f"motor_abs_msg: {'✅ OK' if not errs else errs}")

    # Voice intent
    v_msg = build_voice_intent_msg("calibrate", 0.95, text="calibrate yourself")
    errs = validate_voice_intent_msg(v_msg)
    print(f"voice_intent_msg: {'✅ OK' if not errs else errs}")

    print("\n=== All Topics ===")
    for name, topic in TOPICS.items():
        print(f"  {name:20s} → {topic.decode()}")

    print("\n=== Test Complete ===")
