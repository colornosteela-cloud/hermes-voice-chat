# 🎙️ Hermes Voice Chat + 🤖 Teela Robotics

**v2.0.0** | [Roni](https://github.com/colornosteela-cloud)

A unified conversational robotics platform built for **NVIDIA Jetson** — voice-enabled AI chat **plus** real-time vision-servo synchronization for embodied agents.

---

## 🗂️ Two Subsystems, One Platform

This repository contains **two complementary systems** that share a ZeroMQ messaging backbone:

| Subsystem | Location | What it does |
|-----------|----------|--------------|
| 🎙️ **Hermes Voice Chat** | `src/` | Voice-only chat interface — hold to talk, AI replies with emotional voice |
| 🤖 **Teela Robotics** | `teela/` | Vision-kinematic-servo sync — face tracking, pan/tilt head, calibration |

---

## 🎙️ Subsystem 1: Voice Chat

A **standalone voice-only chat interface** for [Hermes Agent](https://github.com/NousResearch/hermes-agent). No typing needed — just hold and speak.

### ✨ Features

| Feature | Description |
|---------|-------------|
| 🎤 **Voice Input** | Click & hold to record, release to send |
| 🗣️ **Emotional TTS** | Hume Octave — genuine emotional voice |
| 🧠 **Local LLM** | Ollama for fully private inference |
| 🧪 **Mic Test** | Built-in 3-second echo test |
| 📱 **Mobile Friendly** | Touch-friendly dark UI |
| 🔒 **Privacy First** | STT via faster-whisper (on-device) |

### 🚀 Quick Start

```bash
# Setup
git clone https://github.com/colornosteela-cloud/hermes-voice-chat.git
cd hermes-voice-chat
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with HUME_API_KEY, OLLAMA_BASE_URL, etc.

# Launch
python src/main.py
# Open http://localhost:8765
```

### 📁 Files

```
src/
├── main.py          # FastAPI backend (249 lines)
└── index.html       # Voice-only web UI (400+ lines)
```

---

## 🤖 Subsystem 2: Teela Robotics

Real-time **vision-kinematic synchronization** for a 2-axis robotic head. Teela tracks faces with a CSI camera, centers them via smooth servo motion, and runs a full calibration routine on voice command.

### ✨ Features

| Feature | Description |
|---------|-------------|
| 📷 **CSI Vision** | Jetson `nvarguscamerasrc` GStreamer pipeline, face tracking |
| 🔧 **Pan/Tilt Servos** | PCA9685 I2C PWM with eased cubic/sine interpolation |
| 🎯 **Visual Tracking** | Proportional correction from pixel offsets to servo angles |
| 🗣️ **Voice Calibration** | Say *"calibrate"* → full audio-visual-kinematic alignment |
| 🛡️ **Headless Mode** | FakeServo + V4L2 fallback for dev without hardware |
| 📡 **ZMQ Bus** | Mini-ROS pattern: `/voice/intent`, `/vision/tracking`, `/motor/*` |

### 🚀 Quick Start (3 terminals)

```bash
# Terminal 1 — Vision pipeline
python teela/VisionNode.py --zmq-pub tcp://127.0.0.1:5555 --display

# Terminal 2 — Servo actuation
python teela/ActuationNode.py --zmq-sub tcp://127.0.0.1:5555 --pan-pin 0 --tilt-pin 1

# Terminal 3 — Calibration orchestrator
python teela/CalibrationStateMachine.py --zmq-pub tcp://127.0.0.1:5555 --zmq-sub tcp://127.0.0.1:5556
```

### 📁 Files

```
teela/
├── VisionNode.py              # CSI capture, face tracking, ZMQ publisher
├── ActuationNode.py           # Pan/tilt servo control with easing
├── CalibrationStateMachine.py # Voice-triggered calibration sequence
├── ZMQ_Schemas.py             # Shared message schemas + validators
└── README.md                  # Detailed Teela architecture docs
```

### 🎯 Calibration Sequence

1. **Kinematic Boundary Sweep** — Pan ±90°, Tilt ±45°
2. **Return to Center** — Guaranteed (0°, 0°)
3. **Visual Horizon** — AWB lock, frame stability check
4. **Audio-Visual Confirm** — Status `"Ready"` → voice: *"Calibration complete, I am looking at you."*

---

## 🔌 Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    HERMES VOICE CHAT                            │
│                    + TEELA ROBOTICS                             │
│                                                                 │
│    🎤 Voice Chat Frontend                                      │
│    (Browser @ localhost:8765)                                    │
│         │                                                       │
│         │  HTTP + WebRTC → FastAPI                              │
│         ▼                                                       │
│    ┌──────────┐   ┌──────────┐    ┌──────────┐   ┌────────┐  │
│    │  API     │──►│  Hermes  │◄──►│  Ollama  │   │  Hume  │  │
│    │  Server  │   │  Agent   │    │  (LLM)   │   │  (TTS) │  │
│    └────┬─────┘   └──────────┘    └──────────┘   └────────┘  │
│         │                                                       │
│         │  ZMQ pub/sub bus                                      │
│         ▼                                                       │
│    ┌────────────────────────────────────────────────────────┐  │
│    │              TEELA VISION + KINEMATICS                 │  │
│    │  ┌─────────────┐      ┌───────────────┐    ┌────────┐  │  │
│    │  │ VisionNode  │◄────►│ Calibration   │◄──►│ Voice  │  │  │
│    │  │ (CSI + cv2) │      │ StateMachine  │    │ Sync   │  │  │
│    │  └──────┬──────┘      └──────┬────────┘    └────────┘  │  │
│    │         │                    │                          │  │
│    │    /vision/tracking ────────►│                          │  │
│    │         │                    ▼                          │  │
│    │  ┌─────────────────────────────────────────────────┐   │  │
│    │  │           ActuationNode                        │   │  │
│    │  │  (Pan/Tilt servos via I2C/PCA9685)            │   │  │
│    │  └─────────────────────────────────────────────────┘   │  │
│    └────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 📦 Full File Tree

```
hermes-voice-chat/
├── src/
│   ├── main.py              # Voice Chat FastAPI backend
│   └── index.html           # Voice Chat browser UI
├── teela/
│   ├── VisionNode.py        # CSI camera + face tracking
│   ├── ActuationNode.py     # Pan/tilt servo control
│   ├── CalibrationStateMachine.py  # Voice-triggered calibration
│   ├── ZMQ_Schemas.py       # Bus message schemas
│   └── README.md            # Teela subsystem docs
├── .env.example             # Config template (both systems)
├── requirements.txt         # Python dependencies
├── README.md                # This file
└── LICENSE                  # MIT License
```

**Total: ~1,900 lines of production code**

---

## 🔧 Unified Dependencies

```
fastapi
uvicorn
httpx
python-dotenv
numpy
faster-whisper
pyzmq
opencv-python
adafruit-circuitpython-servokit   # Teela hardware only
```

---

## 📋 Requirements

| Component | Requirement |
|-----------|-------------|
| **OS** | Linux (Jetson Nano/Orin, x86_64, ARM64 SBC) |
| **Python** | 3.11+ |
| **Voice Chat** | USB mic + speaker, Chrome/Firefox |
| **Teela** | CSI camera (IMX219/IMX477), PCA9685 servo driver, 2x MG996R |
| **AI Backend** | Ollama running locally |
| **TTS** | Hume AI API key (free tier) |

---

## 🐛 Troubleshooting

### Voice Chat
- **"Mic access denied"** — Use `http://localhost:8765` (browsers block mic on non-localhost HTTP)
- **"No audio detected"** — Test first with **🧪 Test Microphone**
- **"No sound"** — Check `alsamixer`, test with `speaker-test -t sine -f 1000`

### Teela Robotics
- **"CSI pipeline failed"** — VisionNode auto-falls back to `/dev/video0` (V4L2)
- **"No PCA9685 found"** — ActuationNode runs in FakeServo mode (logs angles, no physical motion)
- **Calibration won't trigger** — Requires voice intent `"calibrate"` with confidence ≥ 0.75

---

## 🗂️ Changelog

### v2.0.0 (2026-05-25) — Teela Robotics Integration

#### 🤖 New: VisionKinematicSyncSkill

**Four new production modules:**
- **`teela/VisionNode.py`** (~280 lines) — GStreamer `nvarguscamerasrc` CSI pipeline, OpenCV face tracking in dedicated thread, publishes normalized pixel offsets to ZMQ `/vision/tracking`
- **`teela/ActuationNode.py`** (~310 lines) — PCA9685 PWM servo control via `adafruit_servokit`, fake-servo headless fallback, ease-out-cubic + ease-in-out-sine interpolation, proportional tracking correction from vision offsets
- **`teela/CalibrationStateMachine.py`** (~270 lines) — Deterministic 7-state FSM triggered by `/voice/intent` "calibrate". Executes: pan sweep → tilt sweep → return center → horizon calibration → audio-visual confirmation
- **`teela/ZMQ_Schemas.py`** (~260 lines) — Shared JSON message schemas with type/range validation, builder functions, and self-test for all bus topics

**Hardware targets:**
- Jetson Nano / Jetson Orin with IMX219/IMX477 CSI camera
- PCA9685 16-channel I2C PWM servo driver
- 2-axis pan/tilt kit (MG996R / DS3218 servos)

**Bus topics:** `/voice/intent`, `/vision/tracking`, `/vision/frame_meta`, `/motor/move_absolute`, `/motor/move_relative`, `/motor/position`, `/sys/status`, `/sys/cmd`, `/cal/progress`, `/cal/result`

### v1.0.0 (2026-05-25) — Initial Voice Chat Release

**Features:**
- 🎤 Voice-only chat — hold to talk, release to send
- 🗣️ Hume AI Octave emotional TTS
- 🧠 Local STT via faster-whisper (`base` model)
- 🔗 Ollama via OpenAI-compatible `/v1/chat/completions`
- 🧪 Built-in mic test button
- 🌊 Live waveform visualization (15-bar)
- 🌈 Space-purple dark gradient UI
- 🎹 Spacebar keyboard shortcut
- 🔧 Browser-side resampling (48kHz → 16kHz) + WAV encoding
- 📡 CORS enabled for LAN access
- 🔊 USB speaker support via ALSA

**Tech Stack:** FastAPI + pure HTML/CSS/JS frontend + faster-whisper + Ollama + Hume API

**Compatibility:** ARM64/Jetson, x86_64, Chrome 90+, Firefox 88+, touch devices

---

## 🙏 Credits

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research
- [Hume AI Octave](https://hume.ai) for emotional TTS
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for local STT
- [Ollama](https://ollama.com) for local LLM inference
- Teela robotics built on [Jetson.GPIO](https://github.com/NVIDIA/jetson-gpio) + [Adafruit ServoKit](https://github.com/adafruit/Adafruit_CircuitPython_ServoKit)

---

## 📜 License

MIT License 2026

---

Made with ❤️ by [Roni](https://github.com/colornosteela-cloud)
