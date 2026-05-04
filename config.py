"""
config.py — All environment variables and runtime constants for Aditi.
"""
from __future__ import annotations
import os
from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


# ── Plivo credentials ─────────────────────────────────────────────────────────
PLIVO_AUTH_ID     = _req("PLIVO_AUTH_ID")
PLIVO_AUTH_TOKEN  = _req("PLIVO_AUTH_TOKEN")
PLIVO_PHONE_NUMBER = _req("PLIVO_PHONE_NUMBER")

# ── Required credentials ──────────────────────────────────────────────────────
SARVAM_API_KEY = _req("SARVAM_API_KEY")
NGROK_URL      = _req("NGROK_URL").rstrip("/")

# ── Sarvam endpoints ──────────────────────────────────────────────────────────
SARVAM_STT_WS_BASE  = os.getenv("SARVAM_STT_WS_BASE",  "wss://api.sarvam.ai/speech-to-text/ws")
SARVAM_STT_MODEL    = os.getenv("SARVAM_STT_MODEL",     "saaras:v3")
SARVAM_STT_LANGUAGE = os.getenv("SARVAM_STT_LANGUAGE",  "hi-IN")
SARVAM_LLM_BASE_URL = os.getenv("SARVAM_LLM_BASE_URL",  "https://api.sarvam.ai/v1")
SARVAM_VOICE        = os.getenv("SARVAM_VOICE",         "simran")

SARVAM_TTS_STREAM_URL = "https://api.sarvam.ai/text-to-speech/stream"
SARVAM_TTS_REST_URL   = "https://api.sarvam.ai/text-to-speech"

SARVAM_STT_WS_URL = (
    f"{SARVAM_STT_WS_BASE}"
    f"?language-code={SARVAM_STT_LANGUAGE}"
    f"&model={SARVAM_STT_MODEL}"
    f"&mode=transcribe"
    f"&sample_rate=8000"
    f"&input_audio_codec=pcm_s16le"
    f"&vad_signals=true"
    f"&flush_signal=true"
)

# ── LLM ───────────────────────────────────────────────────────────────────────
LLM_MODEL      = os.getenv("LLM_MODEL",      "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# ── Server ────────────────────────────────────────────────────────────────────
PORT              = int(os.getenv("PORT",           "5050"))
TRANSCRIPTS_DIR   = os.getenv("TRANSCRIPTS_DIR",   "transcripts")
MAKE_CALL_API_KEY = os.getenv("MAKE_CALL_API_KEY",  "")

# ── Call behaviour tunables ───────────────────────────────────────────────────
HANGUP_GRACE_SEC         = float(os.getenv("HANGUP_GRACE_SEC",         "1.5"))
SILENCE_TIMEOUT_SEC      = float(os.getenv("SILENCE_TIMEOUT_SEC",      "20.0"))
TTS_PACE                 = float(os.getenv("TTS_PACE",                 "1.1"))
BARGE_IN_GUARD_SEC       = float(os.getenv("BARGE_IN_GUARD_SEC",       "1.5"))
# 0.3s safe because Sarvam END_SPEECH fires first on normal sentences
POST_UTTERANCE_PAUSE_SEC = float(os.getenv("POST_UTTERANCE_PAUSE_SEC", "0.3"))

# ── VAD (WebRTC noise gate) ───────────────────────────────────────────────────
VAD_MODE        = int(os.getenv("VAD_MODE",        "2"))
# 200ms = fast END_SPEECH; raise to 350 if trailing syllables get clipped
VAD_HANGOVER_MS = int(os.getenv("VAD_HANGOVER_MS", "200"))
VAD_ENABLED     = os.getenv("VAD_ENABLED", "true").lower() not in ("0", "false", "no")

# ── Spectral denoiser ─────────────────────────────────────────────────────────
DENOISE_ENABLED     = os.getenv("DENOISE_ENABLED",     "true").lower() not in ("0", "false", "no")
DENOISE_STRENGTH    = float(os.getenv("DENOISE_STRENGTH",    "0.88"))
DENOISE_PROFILE_SEC = float(os.getenv("DENOISE_PROFILE_SEC", "2.0"))
DENOISE_STATIONARY  = os.getenv("DENOISE_STATIONARY", "false").lower() not in ("0", "false", "no")
