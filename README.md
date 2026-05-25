# 🎙️ Hermes Voice Chat v1.0.0

A **standalone voice-only chat interface** for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — no typing needed. Hold the mic button, speak naturally, and hear AI responses through an emotionally intelligent voice.

Built for **ARM64/Jetson** devices, works on any Linux machine with a USB microphone and speaker.

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🎤 **Voice Input** | Click & hold to record, release to send |
| 🗣️ **Emotional Voice Output** | Hume Octave TTS — speaks with genuine feeling |
| 🧠 **Local LLM** | Connects to Ollama for fully private inference |
| 🧪 **Mic Test Button** | Built-in test to verify microphone works |
| 📱 **Mobile Friendly** | Touch-friendly UI |
| 🔒 **Privacy First** | Speech-to-Text runs locally via faster-whisper |

---

## 🚀 Quick Start

```bash
# 1. Clone and setup
git clone https://github.com/colornosteela-cloud/hermes-voice-chat.git
cd hermes-voice-chat
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your HUME_API_KEY and HUME_VOICE_ID

# 3. Start
python src/main.py

# 4. Open browser
# http://localhost:8765
```

---

## 📋 Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running locally
- [Hume AI](https://hume.ai) API key (free tier)
- USB Microphone + Speaker
- Modern browser (Chrome 90+, Firefox 88+)

---

## 🔧 Configuration

All settings in `.env`:

| Variable | Required | Description |
|----------|----------|-------------|
| `HUME_API_KEY` | ✅ | Hume AI API key |
| `HUME_VOICE_ID` | ✅ | Voice ID from Hume dashboard |
| `OLLAMA_BASE_URL` | — | Ollama URL (default: `http://127.0.0.1:11434`) |
| `OLLAMA_MODEL` | — | Model name (default: `kimi-k2.6:cloud`) |
| `WHISPER_MODEL` | — | Whisper model: `tiny`, `base`, `small` (default: `base`) |

---

## 🖥️ Usage

1. Open `http://localhost:8765`
2. Click **"🧪 Test Microphone"** — speak for 3 seconds
3. If you hear your voice played back, the mic works! ✅
4. Click & **hold** the big glowing orb button
5. **Speak** — "Hello Hermes, how are you?"
6. **Release** to send
7. Wait 1-3 seconds — hear the emotional voice reply!

---

## 🐛 Troubleshooting

**"Microphone access denied"** — Use `http://localhost:8765` (not IP). Browsers block mic on non-localhost HTTP.

**"No audio detected"** — Use Test Mic button first. Speak louder. Check USB mic is selected in browser.

**No sound from speaker** — Check `alsamixer` or `pavucontrol`. Test with `speaker-test -t sine -f 1000`.

---

## 🗂️ Changelog

### v1.0.0 (2026-05-25)

#### 🎉 Initial Release

**Features:**
- 🎤 **Voice-only chat** — No typing needed, just hold and speak
- 🗣️ **Emotional TTS** via Hume AI Octave — LLM-powered voice with genuine feeling
- 🧠 **Local STT** via faster-whisper — Speech-to-Text runs entirely on-device
- 🔗 **Local LLM** via Ollama — OpenAI-compatible API, fully private inference
- 🧪 **Mic Test Button** — Record 3 seconds and play back to verify your microphone
- 📱 **Mobile/Touch Friendly** — Works on phones, tablets, and desktops
- 🌊 **Live Waveform Visualization** — 15-bar animated waveform while recording
- 🌈 **Dark Gradient UI** — Space-purple theme with glowing orb animations
- 🎹 **Keyboard Shortcuts** — Hold `Space` to talk
- 🔧 **Audio Resampling** — Browser records at ~48kHz, automatically resampled to 16kHz for Whisper
- 📡 **Network Access** — CORS enabled, access from any device on your LAN
- 🔊 **USB Speaker Support** — Audio plays through default ALSA output

**Tech Stack:**
- **Backend:** FastAPI (Python) — serves static files + API endpoints
- **Frontend:** Pure HTML/CSS/JS (no build step) — single file, zero dependencies
- **Audio Capture:** Web Audio API (AudioWorklet with ScriptProcessor fallback)
- **WAV Encoding:** Browser-side — Float32 → Int16 → WAV blob, no ffmpeg needed
- **STT:** faster-whisper (`base` model) — runs on CPU, ~150MB download
- **LLM:** Ollama via OpenAI-compatible `/v1/chat/completions` API
- **TTS:** Hume AI Octave REST API — emotional, context-aware voice

**Compatibility:**
- ✅ ARM64 / Jetson Nano / Jetson Orin
- ✅ x86_64 / Desktop PCs
- ✅ Chrome 90+, Firefox 88+, Safari 15+
- ✅ Touch devices (phones, tablets)

---

## 📦 Files

```
hermes-voice-chat/
├── src/
│   ├── main.py          # FastAPI backend (249 lines)
│   └── index.html       # Voice-only web UI (400+ lines)
├── .env.example         # Configuration template
├── requirements.txt     # Dependencies
├── README.md            # This file
└── LICENSE              # MIT License
```

---

## 🙏 Credits

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research
- [Hume AI Octave](https://hume.ai) for emotional TTS
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for local STT
- [Ollama](https://ollama.com) for local LLM inference

---

## 📜 License

MIT License

---

Made with ❤️ by [Roni](https://github.com/colornosteela-cloud)
