"""
Hermes Voice Chat — FastAPI Backend
====================================
Receives WAV audio from browser, transcribes with faster-whisper,
chats with Ollama, generates TTS with Hume.

Usage:
    cd /home/roni/workspace/voice-chat
    source /home/roni/hermes-agent/venv/bin/activate
    python main.py

Then open: http://YOUR_JETSON_IP:8765
"""
import os
import sys
import io
import tempfile
import asyncio
import struct
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv("/home/roni/.hermes/.env")

import httpx
import uvicorn
import numpy as np
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ── Configuration ──────────────────────────────────────────────
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
# Ensure /v1 path is present
if not OLLAMA_URL.rstrip('/').endswith('/v1'):
    OLLAMA_URL = OLLAMA_URL.rstrip('/') + '/v1'
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "kimi-k2.6:cloud")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "ollama")
HUME_API_KEY = os.getenv("HUME_API_KEY", "")
HUME_VOICE_ID = os.getenv("HUME_VOICE_ID", "e9625302-0e04-4c7e-90fa-c5aa1cbbc162")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
AUDIO_DIR = Path("/tmp/voice_chat_audio")

# ── Whisper ───────────────────────────────────────────────────
whisper_model = None

def load_whisper():
    """Lazy-load faster-whisper model."""
    global whisper_model
    if whisper_model is not None:
        return whisper_model
    from faster_whisper import WhisperModel
    print(f"[Whisper] Loading '{WHISPER_MODEL}' model...")
    whisper_model = WhisperModel(
        WHISPER_MODEL,
        device="cpu",
        compute_type="int8",
        download_root="/home/roni/.cache/faster-whisper"
    )
    print("[Whisper] Ready.")
    return whisper_model

# ── Audio helpers ─────────────────────────────────────────────
def read_wav_bytes(wav_bytes: bytes) -> np.ndarray:
    """Parse raw WAV bytes to float32 numpy array [-1, 1]."""
    if wav_bytes[:4] != b'RIFF':
        raise ValueError("Not a valid WAV file (no RIFF header)")
    
    fmt_offset = wav_bytes.find(b'fmt ')
    if fmt_offset == -1:
        raise ValueError("No fmt chunk found")
    fmt_offset += 4
    audio_format = struct.unpack('<H', wav_bytes[fmt_offset+4:fmt_offset+6])[0]
    num_channels = struct.unpack('<H', wav_bytes[fmt_offset+6:fmt_offset+8])[0]
    bits_per_sample = struct.unpack('<H', wav_bytes[fmt_offset+14:fmt_offset+16])[0]
    
    data_offset = wav_bytes.find(b'data')
    if data_offset == -1:
        raise ValueError("No data chunk found")
    data_offset += 8
    
    if bits_per_sample == 16:
        dtype = np.int16
    elif bits_per_sample == 32:
        dtype = np.int32
    else:
        raise ValueError(f"Unsupported bits per sample: {bits_per_sample}")
    
    samples = np.frombuffer(wav_bytes[data_offset:], dtype=dtype)
    if num_channels > 1:
        samples = samples.reshape(-1, num_channels).mean(axis=1)
    
    return samples.astype(np.float32) / np.iinfo(dtype).max

async def transcribe_wav(wav_bytes: bytes) -> str:
    """Transcribe WAV bytes to text. Saves a copy for diagnostics."""
    model = load_whisper()
    
    # Save diagnostic copy
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    diag_path = AUDIO_DIR / f"input_{ts}.wav"
    diag_path.write_bytes(wav_bytes)
    print(f"[Whisper] Input saved to {diag_path} ({len(wav_bytes)} bytes)")
    
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        tmp_path = f.name
    
    try:
        segments, info = model.transcribe(tmp_path, beam_size=5)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        print(f"[Whisper] Lang: {info.language}, Text: '{text[:80]}...'")
        return text
    finally:
        os.unlink(tmp_path)

# ── LLM (Ollama) ──────────────────────────────────────────────
async def chat_with_ollama(user_text: str) -> str:
    """Send text to Ollama, get AI response."""
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {OLLAMA_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": user_text}],
                "stream": False,
                "max_tokens": 2048
            }
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

# ── TTS (Hume) ─────────────────────────────────────────────────
async def generate_tts(text: str) -> Path:
    """Generate emotional TTS with Hume."""
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_path = AUDIO_DIR / f"hume_{timestamp}.mp3"
    
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.hume.ai/v0/tts/file",
            headers={"X-Hume-Api-Key": HUME_API_KEY, "Content-Type": "application/json"},
            json={
                "utterances": [{"text": text, "voice": {"id": HUME_VOICE_ID}}]
            }
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Hume TTS error {resp.status_code}: {resp.text[:200]}")
        out_path.write_bytes(resp.content)
        print(f"[Hume] {out_path.name} ({len(resp.content)} bytes)")
        return out_path

# ── FastAPI app ───────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("=" * 60)
    print("  Hermes Voice Chat Server")
    print("=" * 60)
    print(f"  Open: http://localhost:8765")
    print(f"  LLM:  {OLLAMA_MODEL} @ {OLLAMA_URL}")
    print(f"  STT:  faster-whisper ({WHISPER_MODEL})")
    print(f"  TTS:  Hume Octave ({'OK' if HUME_API_KEY else 'NO KEY'})")
    print("=" * 60)
    try:
        load_whisper()
    except Exception as e:
        print(f"  Whisper preload failed: {e}")
    yield
    print("\n[Server] Goodbye!")

app = FastAPI(title="Hermes Voice Chat", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(content=(Path(__file__).parent / "index.html").read_text())

@app.post("/api/chat")
async def api_chat(audio: UploadFile = File(...)):
    raw_bytes = b""
    try:
        raw_bytes = await audio.read()
        print(f"[API] Received {len(raw_bytes)} bytes from browser")
        
        # Validate it is actually a WAV
        if len(raw_bytes) < 44 or raw_bytes[:4] != b'RIFF':
            print(f"[API] WARNING: Not a valid WAV (first 4 bytes: {raw_bytes[:4]})")
            # Save it anyway for inspection
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            bad_path = AUDIO_DIR / f"bad_format_{ts}.bin"
            bad_path.write_bytes(raw_bytes)
            print(f"[API] Saved bad format to {bad_path}")
        else:
            # Parse WAV header for diagnostics
            try:
                fmt_off = raw_bytes.find(b'fmt ')
                if fmt_off != -1:
                    sr = struct.unpack('<I', raw_bytes[fmt_off+12:fmt_off+16])[0]
                    bits = struct.unpack('<H', raw_bytes[fmt_off+14:fmt_off+16])[0]
                    ch = struct.unpack('<H', raw_bytes[fmt_off+10:fmt_off+12])[0]
                    print(f"[API] WAV: {sr}Hz, {ch}ch, {bits}bit, {len(raw_bytes)} bytes")
            except Exception:
                pass
        
        transcript = await transcribe_wav(raw_bytes)
        print(f"[API] Transcript: '{transcript}'")
        if not transcript:
            return JSONResponse({
                "transcript": "",
                "text": "I didn't catch that. Could you speak louder?",
                "audio_url": None
            })
        
        ai_text = await chat_with_ollama(transcript)
        audio_path = await generate_tts(ai_text)
        audio_url = f"/api/audio/{audio_path.name}"
        
        return JSONResponse({
            "transcript": transcript,
            "text": ai_text,
            "audio_url": audio_url
        })
        
    except Exception as e:
        print(f"[API] ERROR: {e}")
        import traceback
        traceback.print_exc()
        # Save the problematic audio for debugging
        if raw_bytes:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            bad_path = AUDIO_DIR / f"error_{ts}.wav"
            bad_path.write_bytes(raw_bytes)
            print(f"[API] Saved error audio to {bad_path}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/audio/{filename}")
async def serve_audio(filename: str):
    path = AUDIO_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="audio/mpeg")
# Health check
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "ollama": OLLAMA_URL,
        "model": OLLAMA_MODEL,
        "whisper": WHISPER_MODEL,
        "hume_ready": bool(HUME_API_KEY)
    }

# Debug: echo audio back so browser can verify recording works
@app.post("/api/debug/echo")
async def debug_echo(audio: UploadFile = File(...)):
    """Echo the uploaded audio back — useful for testing mic capture."""
    raw_bytes = await audio.read()
    print(f"[DebugEcho] Received {len(raw_bytes)} bytes, type={audio.content_type}")
    
    # Save to a temp file with timestamp
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    echo_path = AUDIO_DIR / f"echo_{timestamp}.wav"
    echo_path.write_bytes(raw_bytes)
    
    return JSONResponse({
        "received_bytes": len(raw_bytes),
        "content_type": audio.content_type,
        "filename": audio.filename,
        "echo_url": f"/api/audio/{echo_path.name}",
        "message": "Your recording has been saved. Try playing the echo_url to verify audio was captured."
    })

# ── Robot Control (Teela Integration) ───────────────────────────
# Simple servo controller that talks directly to PCA9685 over I2C

class RobotController:
    """Lightweight servo controller for voice-chat integration."""
    def __init__(self):
        self._pca = None
        self._limits = None
        self._pan_pin = 0
        self._tilt_pin = 1
        self._invert_pan = True
        self._pan_angle = 0.0
        self._tilt_angle = 0.0
        self._init()
    
    def _init(self):
        import json, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'teela'))
        from utils.pca9685_driver import PCA9685
        try:
            self._pca = PCA9685(bus=7, address=0x40, freq=50)
        except Exception as e:
            print(f"[Robot] PCA9685 not available: {e}")
            return
        # Load limits
        cfg_path = os.path.expanduser("~/.config/teela/calibration.json")
        if os.path.isfile(cfg_path):
            with open(cfg_path) as f:
                self._limits = json.load(f)
        # Center
        self.move(0.0, 0.0)
        print("[Robot] Servo controller ready.")
    
    def move(self, pan: float, tilt: float) -> dict:
        if self._pca is None:
            return {"error": "PCA9685 not connected"}
        # Clamp
        if self._limits:
            pan = max(self._limits.get("pan_min", -180), min(self._limits.get("pan_max", 90), pan))
            tilt = max(self._limits.get("tilt_min", -40), min(self._limits.get("tilt_max", 70), tilt))
        # Invert pan
        pan_cmd = -pan if self._invert_pan else pan
        self._pca.set_servo_angle(self._pan_pin, pan_cmd + 90.0)
        self._pca.set_servo_angle(self._tilt_pin, tilt + 90.0)
        self._pan_angle = pan
        self._tilt_angle = tilt
        return {"pan": pan, "tilt": tilt}
    
    def calibrate(self) -> dict:
        """Quick center + sweep."""
        if self._pca is None:
            return {"error": "PCA9685 not connected"}
        import time
        # Center
        self.move(0.0, 0.0)
        time.sleep(0.3)
        # Small sweep
        if self._limits:
            pan_range = [self._limits.get("pan_min", -60), self._limits.get("pan_max", 60)]
            for target in [pan_range[0], pan_range[1], 0.0]:
                self.move(target, 0.0)
                time.sleep(0.5)
        return {"status": "calibrated", "pan": self._pan_angle, "tilt": self._tilt_angle}
    
    def status(self) -> dict:
        return {
            "pan": self._pan_angle,
            "tilt": self._tilt_angle,
            "limits": self._limits,
            "connected": self._pca is not None
        }

# Singleton robot controller
_robot: RobotController | None = None

def get_robot() -> RobotController:
    global _robot
    if _robot is None:
        _robot = RobotController()
    return _robot

@app.post("/api/robot/move")
async def robot_move(data: dict):
    """Move servos to target angles. JSON body: {\"pan\": 0, \"tilt\": 0}"""
    robot = get_robot()
    result = robot.move(data.get("pan", 0.0), data.get("tilt", 0.0))
    return JSONResponse(result)

@app.post("/api/robot/calibrate")
async def robot_calibrate():
    """Run servo calibration sweep."""
    robot = get_robot()
    result = robot.calibrate()
    return JSONResponse(result)

@app.get("/api/robot/status")
async def robot_status():
    """Get current servo angles and limits."""
    robot = get_robot()
    return JSONResponse(robot.status())

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")
