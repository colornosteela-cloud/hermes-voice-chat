# 🤖 Teela — VisionKinematicSyncSkill

NVIDIA Jetson-based robot skill that **synchronizes CSI camera vision**, **pan/tilt servo kinematics**, and **voice subsystems** over a **ZeroMQ message bus** (Mini-ROS pattern).

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         TEELA ROBOT                             │
│                                                                 │
│    🎤 Voice Node          🧠 Brain Node         📡 ZMQ Bus       │
│   (Whisper + TTS)       (Hermes Agent)      (localhost:5555)   │
│         │                       │                   │            │
│         └──────────┬──────────┘                   │            │
│                    │                              │            │
│                    ▼                              ▼            │
│    ┌────────────────────────────────────────────────────────┐   │
│    │              VisionKinematicSyncSkill                 │   │
│    │  ┌─────────────┐      ┌───────────────┐    ┌────────┐  │   │
│    │  │ VisionNode  │◄────►│ Calibration   │◄──►│ Voice  │  │   │
│    │  │ (CSI + cv2) │      │ StateMachine  │    │ Sync   │  │   │
│    │  └──────┬──────┘      └──────┬────────┘    └────────┘  │   │
│    │         │                    │                          │   │
│    │    /vision/tracking ────────►│                          │   │
│    │         │                    ▼                          │   │
│    │  ┌─────────────────────────────────────────────────┐   │   │
│    │  │           ActuationNode                          │   │   │
│    │  │  (Pan/Tilt servos via I2C/PCA9685)              │   │   │
│    │  └─────────────────────────────────────────────────┘   │   │
│    └────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## 🗂️ Deliverables

| File | Lines | Purpose |
|------|-------|---------|
| `VisionNode.py` | ~280 | CSI camera GStreamer pipeline, face tracking, ZMQ publisher |
| `ActuationNode.py` | ~310 | Pan/tilt servo control with eased interpolation, ZMQ subscriber |
| `CalibrationStateMachine.py` | ~270 | Voice-triggered calibration sequence (sweep → horizon → confirm) |
| `ZMQ_Schemas.py` | ~260 | Shared message schemas + validation helpers for all bus topics |

**Total: ~1,120 lines of production-ready Python**

## 🚀 Quick Start

### Prerequisites (Jetson)
```bash
# Jetson.GPIO (usually pre-installed)
sudo apt install python3-opencv libopencv-dev

# Hardware I2C servo driver
pip3 install adafruit-circuitpython-servokit

# Messaging
pip3 install pyzmq

# Optional: NumPy for tensor ops
pip3 install numpy
```

### Terminal 1 — Vision
```bash
python VisionNode.py --zmq-pub tcp://127.0.0.1:5555 --zmq-sub tcp://127.0.0.1:5556 --display
```

### Terminal 2 — Actuation
```bash
python ActuationNode.py --zmq-sub tcp://127.0.0.1:5555 --pan-pin 0 --tilt-pin 1
```

### Terminal 3 — Calibration State Machine
```bash
python CalibrationStateMachine.py --zmq-pub tcp://127.0.0.1:5555 --zmq-sub tcp://127.0.0.1:5556
```

## 📡 ZMQ Topic Reference

| Topic | Direction | Payload |
|-------|-----------|---------|
| `/voice/intent` | Sub | `{"intent": "calibrate", "confidence": 0.95, "text": "calibrate yourself"}` |
| `/vision/tracking` | Pub | `{"target": "face", "offset_x": -0.12, "offset_y": 0.05, "confidence": 0.94}` |
| `/motor/move_absolute` | Pub | `{"pan": 45, "tilt": -10, "duration_ms": 800, "ease": "sine"}` |
| `/motor/move_relative` | Pub | `{"d_pan": 5, "d_tilt": -2, "speed_degps": 30, "ease": "cubic"}` |
| `/motor/position` | Pub | `{"pan": 12.5, "tilt": -3.0, "reached": true}` |
| `/sys/status` | Pub/Sub | `{"state": "Ready", "source": "calibrate"}` |
| `/sys/cmd` | Sub | `{"cmd": "tracking_on", "source": "voice"}` |
| `/cal/progress` | Pub | `{"step": 2, "total_steps": 5, "label": "horizon"}` |
| `/cal/result` | Pub | `{"success": true, "pan_min": -90, "pan_max": 90, "horizon_stable": true}` |

## 🔧 Calibration Sequence (State Machine)

When `/voice/intent` = `"calibrate"` (confidence ≥ 0.75):

1. **BOUNDARY_SWEEP_PAN** → Pan left → right → center
2. **BOUNDARY_SWEEP_TILT** → Tilt down → up → center
3. **RETURN_TO_CENTER** → Guaranteed (0°, 0°) with eased motion
4. **HORIZON_CALIBRATE** → AWB lock, frame stability check
5. **AUDIO_VISUAL_CONFIRM** → Status `"Ready"` → voice says *"Calibration complete, I am looking at you."*

## 🎛️ Hardware Configuration

| Component | Default | Notes |
|-----------|---------|-------|
| CSI Camera | `1280x720@30fps` | `nvarguscamerasrc` → `nvvidconv` pipeline |
| Pan Servo | PCA9685 CH0 | MG996R / DS3218, `-90°..+90°` |
| Tilt Servo | PCA9685 CH1 | MG996R / DS3218, `-45°..+45°` |
| I2C Address | `0x40` | Standard PCA9685 default |

## 🧪 Headless Mode (No Physical Hardware)

Both `VisionNode` and `ActuationNode` **gracefully degrade** when hardware is unavailable:

- **Vision**: Falls back to `/dev/video0` (V4L2) if CSI pipeline fails
- **Actuation**: Uses `FakeServo` class that logs angles without writing to I2C

Perfect for development on non-Jetson machines.

## 🛡️ Error Handling

All nodes include:
- **ZMQ connection recovery** (reconnect on disconnect)
- **Servo safety clamps** (prevents over-travel)
- **Hardware fallback** (fake mode when I2C/GPIO unavailable)
- **Thread-safe state** (`threading.RLock` on shared position/frame data)
- **Graceful shutdown** (camera release, GPIO cleanup, socket close)

## 📦 Dependencies

```
pyzmq>=22.0
opencv-python
numpy
adafruit-circuitpython-servokit   # Jetson hardware only
Jetson.GPIO                        # Usually pre-installed
```

## 📝 Version

**v1.0.0** — Initial release for Teela robot

## 👤 Author

Colornosteela-cloud (Roni) — Hermes Voice Chat + Teela Robotics

## License

MIT License 2026
