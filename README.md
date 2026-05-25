# 🎙️ Hermes Voice Chat

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
git clone https://github.com/YOUR_USERNAME/hermes-voice-chat.git
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

## 📋 Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running locally
- [Hume AI](https://hume.ai) API key (free tier)
- USB Microphone + Speaker
- Modern browser (Chrome 90+, Firefox 88+)

## 🔧 Configuration

All settings in `.env`:

| Variable | Required | Description |
|----------|----------|-------------|
| `HUME_API_KEY` | ✅ | Hume AI API key |
| `HUME_VOICE_ID` | ✅ | Voice ID from Hume dashboard |
| `OLLAMA_BASE_URL` | — | Ollama URL (default: `http://127.0.0.1:11434`) |
| `OLLAMA_MODEL` | — | Model name (default: `kimi-k2.6:cloud`) |
| `WHISPER_MODEL` | — | Whisper model: `tiny`, `base`, `small` (default: `base`) |

## 🖥️ Usage

1. Open `http://localhost:8765`
2. Click **"🧪 Test Microphone"** — speak for 3 seconds
3. If you hear your voice played back, the mic works! ✅
4. Click & **hold** the big glowing orb button
5. **Speak** — "Hello Hermes, how are you?"
6. **Release** to send
7. Wait 1-3 seconds — hear the emotional voice reply!

## 🐛 Troubleshooting

**"Microphone access denied"** — Use `http://localhost:8765` (not IP). Browsers block mic on non-localhost HTTP.

**"No audio detected"** — Use Test Mic button first. Speak louder. Check USB mic is selected in browser.

**No sound from speaker** — Check `alsamixer` or `pavucontrol`. Test with `speaker-test -t sine -f 1000`.

## 📦 Files

```
hermes-voice-chat/
├── src/
│   ├── main.py          # FastAPI backend
│   └── index.html       # Voice-only web UI
├── .env.example
├── requirements.txt
├── README.md
└── LICENSE
```

## 🙏 Credits

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research
- [Hume AI Octave](https://hume.ai) for emotional TTS
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for local STT
- [Ollama](https://ollama.com) for local LLM inference

## 📜 License

MIT License

---

Made with ❤️ by Roni & Hermes
