"""
Aditi — Hindi EMI Collection Voice Bot
───────────────────────────────────────
STT : Sarvam Saaras v3   (WebSocket, PCM-16 LE @ 8 kHz)
LLM : Sarvam-M           (classification fallback only — thinking model)
TTS : Sarvam Bulbul v3   (HTTP-streaming, µ-law @ 8 kHz → Twilio)

Workflow: 7-condition EMI collection flow
  Root → Opening greeting
    ├─ C1  Death in family        → empathy close
    ├─ C2  Partial payment offer  → capture amount + future date
    ├─ C3  Future payment promise → capture & validate date
    ├─ C4  Full payment today     → payment instructions
    ├─ C5  Refusal / unable       → ask reason (×2), credit warning, callback time
    ├─ C6  Already paid           → capture date + mode, confirm
    └─ C7  Busy / callback        → capture callback time, confirm

All bot lines are scripted — zero LLM latency on the hot path.
LLM (Sarvam-M) is called only when the rule-based classifier returns None.
"""
from __future__ import annotations

import asyncio
import audioop
import base64
import concurrent.futures
import dataclasses
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np
import noisereduce as nr

import httpx
import webrtcvad
import websockets
import websockets.exceptions as ws_exc
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from openai import AsyncOpenAI
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Connect, VoiceResponse

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("aditi")


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
def _req(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


SARVAM_API_KEY      = _req("SARVAM_API_KEY")
TWILIO_ACCOUNT_SID  = _req("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = _req("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = _req("TWILIO_PHONE_NUMBER")
NGROK_URL           = _req("NGROK_URL").rstrip("/")

SARVAM_STT_WS_BASE  = os.getenv("SARVAM_STT_WS_BASE",  "wss://api.sarvam.ai/speech-to-text/ws")
SARVAM_STT_MODEL    = os.getenv("SARVAM_STT_MODEL",    "saaras:v3")
SARVAM_STT_LANGUAGE = os.getenv("SARVAM_STT_LANGUAGE", "hi-IN")
SARVAM_LLM_BASE_URL = os.getenv("SARVAM_LLM_BASE_URL", "https://api.sarvam.ai/v1")
LLM_MODEL           = os.getenv("LLM_MODEL",           "gpt-4.1-mini")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY",      "")
SARVAM_VOICE        = os.getenv("SARVAM_VOICE",        "simran")
PORT                = int(os.getenv("PORT",             "5050"))
TRANSCRIPTS_DIR     = os.getenv("TRANSCRIPTS_DIR",     "transcripts")
MAKE_CALL_API_KEY   = os.getenv("MAKE_CALL_API_KEY",   "")

HANGUP_GRACE_SEC     = float(os.getenv("HANGUP_GRACE_SEC",     "1.5"))
SILENCE_TIMEOUT_SEC  = float(os.getenv("SILENCE_TIMEOUT_SEC",  "20.0"))
TTS_PACE             = float(os.getenv("TTS_PACE",             "1.1"))
# Minimum seconds into a TTS play before barge-in is honoured.
# Prevents pickup-tone / Twilio-connect audio from aborting the opening.
BARGE_IN_GUARD_SEC   = float(os.getenv("BARGE_IN_GUARD_SEC",   "1.5"))

# Noise gate — WebRTC VAD silences non-speech frames before they reach STT
# VAD_MODE: 0 = least aggressive, 3 = most aggressive noise suppression
VAD_MODE            = int(os.getenv("VAD_MODE",            "2"))
VAD_HANGOVER_MS     = int(os.getenv("VAD_HANGOVER_MS",     "500"))  # keep audio N ms after speech ends — longer for Hindi pauses
VAD_ENABLED         = os.getenv("VAD_ENABLED", "true").lower() not in ("0", "false", "no")

# Spectral noise cancellation — noisereduce (runs in thread pool, ~20 ms / 80 ms chunk)
DENOISE_ENABLED     = os.getenv("DENOISE_ENABLED",     "true").lower() not in ("0", "false", "no")
DENOISE_STRENGTH    = float(os.getenv("DENOISE_STRENGTH",    "0.88"))  # 0.0–1.0; higher = more aggressive
DENOISE_PROFILE_SEC = float(os.getenv("DENOISE_PROFILE_SEC", "2.0"))   # seconds for stationary noise profiling
DENOISE_STATIONARY  = os.getenv("DENOISE_STATIONARY", "false").lower() not in ("0", "false", "no")

# How long (seconds) the FSM waits after the first transcript arrives before responding.
# Drains any follow-up STT fragments that arrive during this window so the bot
# acts on the complete utterance instead of the first short chunk.
POST_UTTERANCE_PAUSE_SEC = float(os.getenv("POST_UTTERANCE_PAUSE_SEC", "1.5"))

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

SARVAM_TTS_STREAM_URL = "https://api.sarvam.ai/text-to-speech/stream"
SARVAM_TTS_REST_URL   = "https://api.sarvam.ai/text-to-speech"


# ─────────────────────────────────────────────────────────────────────────────
# Spectral noise cancellation
# ─────────────────────────────────────────────────────────────────────────────

# First-order IIR highpass: removes low-freq rumble (traffic, AC, wind) below ~300 Hz.
# alpha = RC / (RC + dt),  RC = 1/(2π·300),  dt = 1/8000  →  alpha ≈ 0.810
_HP_ALPHA = 0.810

# RMS energy threshold: frames below this are treated as silence for noise profiling.
# Prevents "hello" at call-answer from poisoning the noise reference.
_PROFILE_ENERGY_LIMIT = 0.025

class _StreamDenoiser:
    """
    Per-call noise canceller.

    Pipeline (runs entirely in thread pool via feed_sync):
      1. IIR highpass @ ~300 Hz — removes traffic rumble, AC hum, wind.
      2a. Non-stationary (default): rolling noise estimate adapts to changing
          background (traffic, TV, crowds) using MMSE Wiener filter.
      2b. Stationary (opt-in): build noise profile from silent frames, then
          apply spectral subtraction — best for constant hum/fan noise.
      3. Output delayed ~80 ms (one chunk behind).
    """
    CHUNK_SAMP = 640  # 80 ms @ 8 kHz

    def __init__(self, sr: int = 8000, prop_decrease: float = 0.88,
                 profile_sec: float = 2.0, stationary: bool = False) -> None:
        self._sr         = sr
        self._prop       = prop_decrease
        self._stationary = stationary
        self._target     = int(sr * profile_sec)
        # Stationary-mode profiling state
        self._pbuf:    list[np.ndarray] = []
        self._profiled = 0
        self._noise:   np.ndarray | None = None
        # Shared processing buffers
        self._inbuf  = np.zeros(0, dtype=np.float32)
        self._outbuf = np.zeros(0, dtype=np.float32)
        # IIR highpass filter state
        self._hp_px  = 0.0
        self._hp_py  = 0.0

    def _highpass(self, x: np.ndarray) -> np.ndarray:
        """Remove low-frequency noise below ~300 Hz (traffic, AC, wind)."""
        a = _HP_ALPHA
        y = np.empty_like(x)
        px, py = self._hp_px, self._hp_py
        for i in range(len(x)):
            v = a * (py + x[i] - px)
            px, py = x[i], v
            y[i] = v
        self._hp_px, self._hp_py = px, py
        return y

    def _denoise(self, seg: np.ndarray) -> np.ndarray:
        # Suppress divide-by-zero / invalid warnings from noisereduce on silent frames
        with np.errstate(divide="ignore", invalid="ignore"):
            if self._stationary:
                out = nr.reduce_noise(
                    y=seg, y_noise=self._noise, sr=self._sr,
                    stationary=True, prop_decrease=self._prop,
                    n_fft=512, n_jobs=1,
                )
            else:
                out = nr.reduce_noise(
                    y=seg, sr=self._sr,
                    stationary=False, prop_decrease=self._prop,
                    n_fft=512, n_jobs=1,
                    time_constant_s=1.5,
                )
        # Replace NaN / Inf that noisereduce can produce on pure-silence frames
        return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)

    def _emit(self, n: int, fallback: np.ndarray) -> bytes:
        out = self._outbuf[:n] if self._outbuf.shape[0] >= n else fallback
        if self._outbuf.shape[0] >= n:
            self._outbuf = self._outbuf[n:]
        out = np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)
        return (np.clip(out, -1.0, 1.0) * 32768).astype(np.int16).tobytes()

    def feed_sync(self, pcm16: bytes) -> bytes:
        raw   = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        chunk = self._highpass(raw)  # always apply highpass first

        # ── Stationary: phase 1 — build noise profile from silent frames ──────
        if self._stationary and self._noise is None:
            self._profiled += chunk.shape[0]
            if float(np.sqrt(np.mean(chunk ** 2))) < _PROFILE_ENERGY_LIMIT:
                self._pbuf.append(chunk.copy())
            enough  = sum(a.shape[0] for a in self._pbuf) >= self._target
            timeout = self._profiled >= self._target * 3
            if enough or timeout:
                self._noise = (np.concatenate(self._pbuf) if self._pbuf
                               else np.zeros(self.CHUNK_SAMP, dtype=np.float32))
                self._pbuf.clear()
            # Return highpass-filtered audio while profiling
            return (np.clip(chunk, -1.0, 1.0) * 32768).astype(np.int16).tobytes()

        # ── Phase 2: accumulate → denoise → emit ─────────────────────────────
        self._inbuf = np.concatenate([self._inbuf, chunk])
        if self._inbuf.shape[0] >= self.CHUNK_SAMP:
            seg         = self._inbuf[:self.CHUNK_SAMP]
            self._inbuf = self._inbuf[self.CHUNK_SAMP:]
            try:
                self._outbuf = np.concatenate([self._outbuf, self._denoise(seg)])
            except Exception:
                self._outbuf = np.concatenate([self._outbuf, seg])

        return self._emit(chunk.shape[0], chunk)


# ─────────────────────────────────────────────────────────────────────────────
# Shared clients
# ─────────────────────────────────────────────────────────────────────────────
_http = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=4.0, read=15.0, write=5.0, pool=5.0),
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)
_oai     = AsyncOpenAI(api_key=SARVAM_API_KEY, base_url=SARVAM_LLM_BASE_URL)  # Sarvam (STT/TTS auth, unused for LLM now)
_oai_llm = AsyncOpenAI(api_key=OPENAI_API_KEY)                                # OpenAI GPT-4.1-mini for classification


# ─────────────────────────────────────────────────────────────────────────────
# ── Input variables for each outbound call ───────────────────────────────────
# Pass these fields in the POST /make-call body.  .env fallbacks for testing.
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_CUSTOMER: dict[str, str] = {
    # ── Core caller inputs (required per call) ────────────────────────────────
    "customer_name":       os.getenv("DEFAULT_CUSTOMER_NAME",       "Rahul"),
    "phone_number":        os.getenv("DEFAULT_PHONE_NUMBER",        ""),            # number being dialed
    "loan_id":             os.getenv("DEFAULT_LOAN_ID",             "EH12345"),
    "emi_overdue_amt":     os.getenv("DEFAULT_EMI_OVERDUE_AMT",     "8,500"),       # overdue amount (display)
    "emi_overdue_amt_int": os.getenv("DEFAULT_EMI_OVERDUE_AMT_INT", "8500"),        # integer for arithmetic
    "emi_overdue_date":    os.getenv("DEFAULT_EMI_OVERDUE_DATE",    "5 March 2026"),# due date of overdue EMI

    # ── Supporting fields ─────────────────────────────────────────────────────
    "lender":              os.getenv("DEFAULT_LENDER",              "Easy Home Finance"),
    "loan_type":           os.getenv("DEFAULT_LOAN_TYPE",           "Home Loan"),
    "min_partial":         os.getenv("DEFAULT_MIN_PARTIAL",         "1,500"),
    "min_partial_int":     os.getenv("DEFAULT_MIN_PARTIAL_INT",     "1500"),
    "payment_deadline":    os.getenv("DEFAULT_PAYMENT_DEADLINE",    ""),            # filled by _build_default_ctx()

    # ── Backward-compat aliases so existing scripts keep resolving ────────────
    "emi_amount":          os.getenv("DEFAULT_EMI_OVERDUE_AMT",     "8,500"),
    "emi_amount_int":      os.getenv("DEFAULT_EMI_OVERDUE_AMT_INT", "8500"),
    "emi_due_date":        os.getenv("DEFAULT_EMI_OVERDUE_DATE",    "5 March 2026"),
}

# Mandatory closing — used at end of C2 C3 C4 C5
_MANDATORY_CLOSING = (
    "भुगतान पूरा करने के लिए आपको भेजे गए सुरक्षित लिंक का उपयोग करें। "
    "कृपया {payment_deadline} तक शेष राशि चुकाने की कोशिश करें "
    "ताकि आपका क्रेडिट स्कोर सुरक्षित रहे। "
    "आपके सहयोग के लिए धन्यवाद, और आपका दिन शुभ हो।"
)


# ─────────────────────────────────────────────────────────────────────────────
# Scripted responses (Hinglish)
# ─────────────────────────────────────────────────────────────────────────────
_SCRIPTS: dict[str, str] = {
    # ── Opening ──────────────────────────────────────────────────────────────
    "opening": (
        "नमस्ते {customer_name} जी, मैं Aditi बोल रही हूँ Easy Home Finance से। "
        "आपकी {emi_amount} रुपये की EMI, जो {emi_due_date} को देय थी, अभी तक बाकी है। "
        "कृपया बताइए आप कब तक भुगतान कर पाएंगे?"
    ),

    # ── C1: Death ────────────────────────────────────────────────────────────
    "death_response": (
        "मुझे आपके नुकसान के लिए बहुत दुख है। "
        "इस कठिन समय में हम आपके साथ हैं। "
        "हमारी टीम का एक सदस्य जल्द ही आपसे व्यक्तिगत रूप से संपर्क करेगा। "
        "धन्यवाद।"
    ),

    # ── C2: Partial payment ──────────────────────────────────────────────────
    "offer_partial": (
        "मैं समझती हूँ कि आप अभी आर्थिक कठिनाई में हैं। "
        "क्या आप आज आंशिक भुगतान कर सकते हैं? "
        "न्यूनतम {min_partial} रुपये होना चाहिए।"
    ),
    "partial_ask_amount": (
        "धन्यवाद। आप आज कितनी राशि का भुगतान कर सकते हैं? "
        "न्यूनतम {min_partial} रुपये होना चाहिए।"
    ),
    "partial_amount_too_low": (
        "यह राशि न्यूनतम से कम है। "
        "कृपया {min_partial} रुपये या उससे अधिक बताइए।"
    ),
    "partial_amount_unclear": (
        "मुझे राशि समझ नहीं आई। "
        "कृपया रुपये में बताइए, जैसे दो हजार या तीन हजार।"
    ),
    "partial_ask_remaining_date": (
        "धन्यवाद। आज {partial_amount} रुपये के भुगतान के बाद "
        "{remaining_balance} रुपये शेष रहेंगे। "
        "आप यह बाकी राशि कब तक चुकाएंगे?"
    ),
    "partial_date_retry": (
        "यह तारीख मान्य नहीं लगी। "
        "कृपया एक भविष्य की तारीख बताइए जो नब्बे दिनों के अंदर हो।"
    ),
    "date_beyond_90": (
        "हम 90 दिनों से अधिक का समय नहीं दे सकते। "
        "कृपया अगले 90 दिनों के भीतर एक तारीख बताइए।"
    ),
    "partial_confirm": (
        "ठीक है, हमने आपकी भुगतान जानकारी नोट कर ली है। "
        + _MANDATORY_CLOSING
    ),

    # ── C3: Future promise ────────────────────────────────────────────────────
    "future_ask_date": (
        "धन्यवाद। आप भुगतान किस तारीख तक कर पाएंगे?"
    ),
    "future_date_retry": (
        "यह तारीख मान्य नहीं लगी। "
        "कृपया एक भविष्य की तारीख बताइए जो नब्बे दिनों के अंदर हो।"
    ),
    "future_date_beyond_90": (
        "हम 90 दिनों से अधिक का समय नहीं दे सकते। "
        "कृपया अगले 90 दिनों के भीतर एक तारीख चुनें, जैसे अगले महीने या दो महीने बाद।"
    ),
    "future_confirm": (
        "ठीक है, हमने आपकी भुगतान तारीख {payment_date} नोट कर ली है। "
        + _MANDATORY_CLOSING
    ),

    # ── C4: Full payment today ────────────────────────────────────────────────
    "full_payment_today": (
        "धन्यवाद। कृपया SMS के माध्यम से भेजे गए सुरक्षित लिंक का उपयोग करके "
        "या अपनी नजदीकी शाखा में जाकर भुगतान पूरा करें। "
        + _MANDATORY_CLOSING
    ),

    # ── C5: Refusal / unable to pay ───────────────────────────────────────────
    "refusal_ask_reason": (
        "समझ गई। क्या आप बता सकते हैं कि आप भुगतान क्यों नहीं कर पा रहे हैं?"
    ),
    "refusal_escalate": (
        "मैं समझती हूँ। "
        "क्या कोई विशेष कारण है जिसकी वजह से आप अभी भुगतान नहीं कर पा रहे?"
    ),
    "refusal_credit_warn": (
        "ठीक है, हमने आपकी बात नोट कर ली है। "
        "कृपया ध्यान रखें कि बकाया EMI से जुर्माना और CIBIL स्कोर पर असर पड़ सकता है। "
        "आप चाहेंगे कि हम आपसे दोबारा कब संपर्क करें?"
    ),
    "refusal_close": (
        "ठीक है, हम आपसे {callback_time} संपर्क करेंगे। "
        "कृपया {payment_deadline} तक भुगतान करने की कोशिश करें — "
        "इससे आपका CIBIL स्कोर सुरक्षित रहेगा। "
        "आपका दिन शुभ हो।"
    ),

    # ── C6: Already paid ──────────────────────────────────────────────────────
    "already_paid_ask_date": (
        "कृपया बताइए कि आपने भुगतान किस तारीख को किया था?"
    ),
    "already_paid_date_invalid": (
        "यह भविष्य की तारीख है। "
        "कृपया वह सही तारीख बताइए जब आपने वास्तव में भुगतान किया था।"
    ),
    "already_paid_date_unclear": (
        "तारीख समझ नहीं आई। कृपया फिर से बताइए।"
    ),
    "already_paid_ask_mode": (
        "भुगतान किस माध्यम से किया गया था? "
        "जैसे UPI, Google Pay, नेट बैंकिंग, नकद, शाखा, या कोई अन्य?"
    ),
    "already_paid_confirm": (
        "धन्यवाद। हमने आपकी जानकारी प्राप्त कर ली है। "
        "हम इसे सत्यापित करके अपने रिकॉर्ड अपडेट कर देंगे। "
        "आपका दिन शुभ हो।"
    ),

    # ── C7: Busy / callback ───────────────────────────────────────────────────
    "callback_ask_time": (
        "कोई बात नहीं। आप बताइए कि मैं आपको कब कॉल करूँ?"
    ),
    "callback_time_unclear": (
        "समय समझ नहीं आया। कृपया फिर से बताइए, जैसे 'कल दोपहर' या 'तीन बजे'।"
    ),
    "callback_confirm": (
        "ठीक है, हम आपसे {callback_time} संपर्क करेंगे। "
        "कृपया कोशिश करें कि EMI जल्दी चुका दें ताकि जुर्माना न लगे।"
    ),

    # ── FAQ answers (inline, cross-state) ────────────────────────────────────
    "faq_emi_amount": (
        "आपकी EMI {emi_amount} रुपये है, जो {emi_due_date} को देय थी।"
    ),
    "faq_due_date": (
        "आपकी EMI की due date {emi_due_date} थी।"
    ),
    "faq_loan_id": (
        "आपका Loan ID {loan_id} है।"
    ),

    # ── Unclear handler ───────────────────────────────────────────────────────
    "unclear_sm1": (
        "माफी चाहती हूँ, आवाज़ साफ नहीं आई। "
        "क्या आप आज EMI भुगतान कर पाएंगे — हाँ या नहीं?"
    ),
    "unclear_sm2": (
        "एक बार और कोशिश करते हैं — "
        "बस हाँ या नहीं बोलिए, क्या आज EMI भर पाएंगे?"
    ),
    "unclear_close": (
        "ठीक है, लगता है अभी बात नहीं हो पा रही। "
        "हम जल्द दोबारा संपर्क करेंगे। "
        "धन्यवाद, आपका दिन शुभ हो।"
    ),
}

# Terminal states — speak and hang up
_TERMINAL: set[str] = {
    "death_response", "partial_confirm", "future_confirm",
    "full_payment_today", "refusal_close", "already_paid_confirm",
    "callback_confirm", "unclear_close",
}

# States where barge-in is suppressed — terminal + opening (opening must play in full)
_BARGE_IN_LOCKED: set[str] = _TERMINAL | {"opening"}

# States that advance to "opening" after speaking (no user input needed)
_AUTO_ADVANCE: dict[str, str] = {
    "unclear_sm1": "opening",
    "unclear_sm2": "opening",
}


# ─────────────────────────────────────────────────────────────────────────────
# Date & amount utilities
# ─────────────────────────────────────────────────────────────────────────────
_MONTH_MAP: dict[str, int] = {
    "jan": 1, "january": 1, "janvari": 1,
    "feb": 2, "february": 2, "farvari": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5, "mai": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "sitambar": 9,
    "oct": 10, "october": 10, "aktubar": 10,
    "nov": 11, "november": 11, "navambar": 11,
    "dec": 12, "december": 12, "disambar": 12,
}


def _parse_date(text: str) -> _date | None:
    """Extract a date from a Hindi/Hinglish utterance. Returns None if unparseable."""
    t = text.lower().strip()
    today = _date.today()

    # Devanagari → Roman so the regexes below match both scripts
    t = (t.replace("आज", "aaj")
          .replace("कल", "kal")
          .replace("परसों", "parso").replace("परसो", "parso")
          .replace("अगले साल", "agle saal").replace("अगले वर्ष", "agle saal")
          .replace("अगले हफ़्ते", "agle hafte")
          .replace("अगले हफ्ते", "agle hafte")
          .replace("इस हफ्ते", "is hafte").replace("इस हफ़्ते", "is hafte")
          .replace("अगले महीने", "agle mahine")
          .replace("महीने में", "mahine mein").replace("महीने बाद", "mahine baad")
          .replace("हफ़्ते में", "hafte mein").replace("हफ्ते में", "hafte mein")
          .replace("दिनों में", "dinon mein").replace("दिन में", "din mein")
          # Hindi month names
          .replace("जनवरी", "january")
          .replace("फरवरी", "february").replace("फ़रवरी", "february")
          .replace("मार्च", "march")
          .replace("अप्रैल", "april")
          .replace("मई", "may")
          .replace("जून", "june")
          .replace("जुलाई", "july")
          .replace("अगस्त", "august")
          .replace("सितंबर", "september").replace("सितम्बर", "september")
          .replace("अक्टूबर", "october").replace("अक्तूबर", "october")
          .replace("नवंबर", "november").replace("नवम्बर", "november")
          .replace("दिसंबर", "december").replace("दिसम्बर", "december"))

    if re.search(r"\baaj\b|today", t):
        return today
    if re.search(r"\bkal\b|tomorrow", t):
        return today + timedelta(days=1)
    if re.search(r"\bparso\b|parson\b", t):
        return today + timedelta(days=2)
    if re.search(r"agle\s+saal|next\s+year", t):
        return today + timedelta(days=366)  # caller will cap to 90
    if re.search(r"agle\s+hafte|next\s+week", t):
        return today + timedelta(days=7)
    if re.search(r"agle\s+mahine|next\s+month|mahine\s+(?:mein|baad)", t):
        return today + timedelta(days=30)

    # "2-3 din" → take first
    m = re.search(r"(\d+)\s*[-–]\s*\d+\s*din", t)
    if m:
        return today + timedelta(days=int(m.group(1)))

    # "X din" / "X days"
    m = re.search(r"(\d+)\s*(?:din\b|days?\b)", t)
    if m:
        return today + timedelta(days=int(m.group(1)))

    # "15 March" / "March 15" (with optional year)
    for name, num in _MONTH_MAP.items():
        m = re.search(rf"(\d{{1,2}})\s+{name}(?:\s+(\d{{4}}))?", t)
        if m:
            day, year = int(m.group(1)), int(m.group(2)) if m.group(2) else today.year
            try:
                d = _date(year, num, day)
                if d < today and not m.group(2):
                    d = _date(year + 1, num, day)
                return d
            except ValueError:
                return None
        m = re.search(rf"{name}\s+(\d{{1,2}})(?:\s+(\d{{4}}))?", t)
        if m:
            day, year = int(m.group(1)), int(m.group(2)) if m.group(2) else today.year
            try:
                d = _date(year, num, day)
                if d < today and not m.group(2):
                    d = _date(year + 1, num, day)
                return d
            except ValueError:
                return None

    # Bare number 1–31 → treat as day of current/next month
    m = re.search(r"\b(\d{1,2})\b", t)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            try:
                d = _date(today.year, today.month, day)
                if d <= today:
                    nm = today.month % 12 + 1
                    ny = today.year + (1 if today.month == 12 else 0)
                    d  = _date(ny, nm, day)
                return d
            except ValueError:
                pass

    return None


def _parse_amount(text: str) -> int | None:
    """Extract a rupee amount (integer) from Hindi/Hinglish speech."""
    t = (text.lower()
         .replace(",", "")
         .replace("₹", " ")
         .replace("rs.", " ")
         .replace("rs ", " ")
         .replace("रुपये", " ").replace("रुपए", " ").replace("रूपये", " ")
         .replace("rupaye", " ").replace("rupees", " ").replace("rupiya", " ")
         # Devanagari → Roman so the regexes below match both scripts
         .replace("हज़ार", "hazaar").replace("हजार", "hazaar")
         .replace("सौ", "sau").replace("सो", "sau")
         .replace("लाख", "lakh"))

    # "X hazaar Y sau" → X*1000 + Y*100
    m = re.search(r"(\d+)\s*(?:hazaar|hazar|hajar|thousand)\s*(?:(\d+)\s*(?:sau|so|hundred))?", t)
    if m:
        return int(m.group(1)) * 1000 + (int(m.group(2)) * 100 if m.group(2) else 0)

    # "X lakh" → X*100000
    m = re.search(r"(\d+)\s*lakh", t)
    if m:
        return int(m.group(1)) * 100_000

    # "X sau" → X*100
    m = re.search(r"(\d+)\s*(?:sau|so|hundred)", t)
    if m:
        return int(m.group(1)) * 100

    # Any 3–6 digit number
    m = re.search(r"\b(\d{3,6})\b", t)
    if m:
        return int(m.group(1))

    return None


def _fmt_date(d: _date) -> str:
    return d.strftime("%-d %B %Y")


def _build_default_ctx() -> dict[str, str]:
    ctx = dict(DEFAULT_CUSTOMER)
    if not ctx.get("payment_deadline"):
        ctx["payment_deadline"] = _fmt_date(_date.today() + timedelta(days=7))
    # Keep backward-compat aliases in sync with primary input fields
    if ctx.get("emi_overdue_amt"):
        ctx.setdefault("emi_amount",     ctx["emi_overdue_amt"])
    if ctx.get("emi_overdue_amt_int"):
        ctx.setdefault("emi_amount_int", ctx["emi_overdue_amt_int"])
    if ctx.get("emi_overdue_date"):
        ctx.setdefault("emi_due_date",   ctx["emi_overdue_date"])
    return ctx


_TRAILING_VERBS = re.compile(
    r"\s*(?:करना|करूंगा|करूँगा|कर दूंगा|कर दूँगा|कर दें|कर दीजिए|कर दीजिएगा"
    r"|कीजिएगा|कीजिए|करिएगा|करिए|कर लीजिए|कर लीजिएगा"
    r"|हो जाएगा|हो जायेगा|बाद में|ठीक है|okay|ok)[।.,\s]*$",
    re.IGNORECASE,
)

def _clean_callback_time(text: str) -> str:
    """Strip trailing filler verbs from a callback-time utterance."""
    cleaned = _TRAILING_VERBS.sub("", text.strip()).strip("।., ")
    return cleaned if cleaned else text[:60]


# Bare acknowledgements that are NOT a time/date — reject these as callback times.
_BARE_ACK = re.compile(
    r"^(haan?|han|ha|nahi?|nai|okay?|ok|theek\s*hai|bilkul|zaroor|"
    r"ji+|sahi\s*hai|acha|accha|"
    r"हाँ?|हां|नहीं|नहीं|ठीक\s*है|बिल्कुल|जरूर|जी+|अच्छा|सही\s*है)[।.,!?\s]*$",
    re.IGNORECASE | re.UNICODE,
)

# Recognisable time/date words — at least one must be present for a valid callback time.
_TIME_WORD = re.compile(
    r"\d"                           # any digit (3 baje, 15 tarikh …)
    r"|kal\b|parso\b|agle\b|hafte?\b|mahine?\b|ghante?\b|baje?\b|"
    r"shaam\b|raat\b|subah\b|dopahar\b|savere\b|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"somvar|mangal|budhvar|gurvar|shukra|shanivar|ravivar|"
    r"week|month|morning|evening|night|afternoon|hour|minute|"
    r"कल|परसों|अगले|हफ्ते?|महीने?|घंटे?|बजे?|"
    r"शाम|रात|सुबह|दोपहर|सवेरे|"
    r"सोमवार|मंगलवार|बुधवार|गुरुवार|शुक्रवार|शनिवार|रविवार|"
    r"दिन\b|सप्ताह|तारीख",
    re.IGNORECASE | re.UNICODE,
)


def _is_callback_time(text: str) -> bool:
    """Return True only if the utterance plausibly contains a time/date reference."""
    t = text.strip()
    if not t or bool(_BARE_ACK.match(t)):
        return False
    return bool(_TIME_WORD.search(t))


# ─────────────────────────────────────────────────────────────────────────────
# Classification — pure LLM (GPT-4.1-mini)
# ─────────────────────────────────────────────────────────────────────────────


_LLM_CLASSIFY_PROMPTS: dict[str, str] = {
    "opening": """\
Classify a Hindi/Hinglish customer reply in an EMI collection call.
Agent asked: "Aapka EMI pending hai, kab tak kar paayenge?"
Customer said: "{utterance}"

LABEL DEFINITIONS — read carefully before choosing:
GOTO_PAY_NOW              — agrees/consents without mentioning a future date ("हाँ", "okay", "करूंगा", "ठीक है", "हाँ हाँ", "bilkul", "ji" alone)
GOTO_PARTIAL              — explicitly offers to pay only part of the amount today
GOTO_FUTURE_PROMISE       — names a specific future time ("kal", "agle hafte", "10 tarikh", "salary ke baad", "thoda waqt do", "kuch din de do")
GOTO_ALREADY_PAID         — claims payment was already made
GOTO_CALLBACK             — busy RIGHT NOW, cannot talk, asks to call back ("busy", "baad mein", "bahar hoon", "abhi time nahi", "driving", "abhi nahi kar paunga" = temporal not financial)
GOTO_DEATH                — death of borrower or close family member
GOTO_FINANCIAL_DIFFICULTY — financially unable to pay ("paise nahi", "mushkil hai", bare "nahi" with no other context)
GOTO_REFUSAL              — aggressive flat refusal ("nahi dunga", "band karo", "court mein jao", explicit threats)
MARK_UNCLEAR              — completely unintelligible noise

KEY DISAMBIGUATION RULES:
- "करूंगा" / "kar dunga" alone (no date) = GOTO_PAY_NOW, NOT future_promise
- "थोड़ा वक्त दीजिए" / "kuch din do" = GOTO_FUTURE_PROMISE (asking for time, not inability)
- "अभी time नहीं" / "अभी नहीं कर पाऊंगा" = GOTO_CALLBACK (temporal — busy now, not broke)
- Bare "नहीं" with no aggression = GOTO_FINANCIAL_DIFFICULTY (benefit of doubt, not refusal)
- GOTO_REFUSAL only for explicit aggression / legal threats / "nahi dunga" with finality

Output ONLY the label, nothing else.""",

    "offer_partial": """\
The agent offered partial payment. The customer said: "{utterance}"

Output EXACTLY ONE:
GOTO_PARTIAL_YES — accepts partial payment offer
GOTO_CALLBACK    — busy, outside, wants to be called back later
GOTO_PARTIAL_NO  — declines for any other reason""",

    "refusal_ask_reason": """\
The agent asked: "Aap payment kyun nahi kar pa rahe?"
The customer said: "{utterance}"

Output EXACTLY ONE:
GOT_REASON     — gave any reason (even vague)
STILL_REFUSING — refuses to give any reason""",

    "refusal_escalate": """\
The agent asked again: "Koi specific wajah?"
The customer said: "{utterance}"

Output EXACTLY ONE:
GOT_REASON     — gave any reason
STILL_REFUSING — still refuses""",
}

_LLM_KEYWORD_MAP: dict[str, str] = {
    "GOTO_PAY_NOW":              "goto_pay_now",
    "GOTO_PARTIAL":              "goto_partial",
    "GOTO_FUTURE_PROMISE":       "goto_future_promise",
    "GOTO_ALREADY_PAID":         "goto_already_paid",
    "GOTO_CALLBACK":             "goto_callback",
    "GOTO_DEATH":                "goto_death",
    "GOTO_FINANCIAL_DIFFICULTY": "goto_financial_difficulty",
    "GOTO_REFUSAL":              "goto_refusal",
    "GOTO_PARTIAL_YES":          "goto_partial_yes",
    "GOTO_PARTIAL_NO":           "goto_partial_no",
    "GOT_REASON":                "got_reason",
    "STILL_REFUSING":            "still_refusing",
    "MARK_UNCLEAR":              "mark_unclear",
}


async def classify(utterance: str, state: str) -> str:
    """GPT-4.1-mini primary classifier. Keyword heuristic kept but not used."""
    prompt_template = _LLM_CLASSIFY_PROMPTS.get(state)
    if not prompt_template:
        log.info("CLASSIFY[no-prompt] %s → mark_unclear", state)
        return "mark_unclear"

    try:
        resp = await _oai_llm.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt_template.format(utterance=utterance)}],
            temperature=0,
            max_tokens=32,
        )
        raw = (resp.choices[0].message.content or "").strip().upper()
        log.info("CLASSIFY[llm] raw=%r", raw[:100])
        for kw in sorted(_LLM_KEYWORD_MAP, key=len, reverse=True):
            if kw in raw:
                result = _LLM_KEYWORD_MAP[kw]
                log.info("CLASSIFY[llm] %s → %s", state, result)
                return result
    except Exception as exc:
        log.error("LLM classify error: %s", exc)

    log.info("CLASSIFY[default] %s → mark_unclear", state)
    return "mark_unclear"


async def llm_extract_amount(utterance: str) -> int | None:
    """
    LLM fallback for amount extraction — catches Hindi word-numerals that
    regex misses ("teen hazaar", "paanch sau", "do lakh", etc.).
    """
    try:
        resp = await _oai_llm.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": (
                "Extract the rupee amount (integer) from this Hindi/Hinglish text.\n"
                "Examples: 'teen hazaar' → 3000, 'paanch sau' → 500, '₹4,500' → 4500\n"
                "Return ONLY the integer. If no amount present, write: NONE\n\n"
                f"Text: {utterance}\nAmount:"
            )}],
            temperature=0,
            max_tokens=15,
        )
        r = (resp.choices[0].message.content or "").strip()
        if not r or "NONE" in r.upper():
            return None
        m = re.search(r"\d+", r.replace(",", ""))
        result = int(m.group()) if m else None
        log.info("LLM_AMOUNT %r → %s", utterance[:50], result)
        return result
    except Exception as exc:
        log.warning("llm_extract_amount error: %s", exc)
        return None


async def extract_callback_time(utterance: str) -> str:
    """
    Use LLM to pull a clean, speakable callback-time phrase from the user's utterance.
    Falls back to _clean_callback_time() on any error.
    """
    try:
        resp = await _oai_llm.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": (
                "Extract ONLY the callback time/day from this Hindi/Hinglish text as a short phrase.\n"
                "Good examples: 'आज रात 9 बजे', 'कल शाम 5 बजे', 'शनिवार को', 'अगले हफ्ते', 'कल'\n"
                "Do NOT include verbs like 'call karo', 'sampark karo', 'kijiye', 'dijiye' etc.\n"
                "Output ONLY the time phrase, nothing else. If completely unclear write: जल्द ही\n\n"
                f"Text: {utterance}\nTime phrase:"
            )}],
            temperature=0,
            max_tokens=25,
        )
        result = (resp.choices[0].message.content or "").strip().strip('।.,\'"')
        log.info("EXTRACT_TIME[llm] %r → %r", utterance[:60], result)
        return result if result else "जल्द ही"
    except Exception as exc:
        log.warning("extract_callback_time LLM error: %s", exc)
        return _clean_callback_time(utterance)


# ─────────────────────────────────────────────────────────────────────────────
# FAQ detection
# ─────────────────────────────────────────────────────────────────────────────

# Quick keyword gate — only call LLM when these words appear, saves latency
_FAQ_TRIGGER_RE = re.compile(
    r"kitni|kitna|कितनी|कितना|कितने|"
    r"due\s*date|emi\s*kab|kab\s*deni|"
    r"loan\s*id|account\s*number|loan\s*number|"
    r"EMI\s*hai|emi\s*amount|emi\s*kitni",
    re.IGNORECASE | re.UNICODE,
)


async def check_faq(utterance: str) -> str | None:
    """Returns FAQ script key (faq_emi_amount / faq_due_date / faq_loan_id) or None."""
    try:
        resp = await _oai_llm.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": (
                "Is this Hindi/Hinglish utterance asking a factual question about their loan?\n"
                "EMI_AMOUNT — asking about EMI amount (कितनी EMI है, emi kitni hai)\n"
                "DUE_DATE   — asking about due date (due date kya hai, kab deni thi)\n"
                "LOAN_ID    — asking about loan ID or account number\n"
                "NONE       — not a factual FAQ question\n\n"
                f"Utterance: {utterance}\nAnswer:"
            )}],
            temperature=0,
            max_tokens=15,
        )
        raw = (resp.choices[0].message.content or "").strip().upper()
        if "EMI_AMOUNT" in raw:
            return "faq_emi_amount"
        if "DUE_DATE" in raw:
            return "faq_due_date"
        if "LOAN_ID" in raw:
            return "faq_loan_id"
        return None
    except Exception as exc:
        log.warning("check_faq error: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Call variable finalisation
# ─────────────────────────────────────────────────────────────────────────────

async def finalize_call_variables(final_state: str, ctx: dict[str, str]) -> dict:
    """
    LLM normalises all captured call variables into structured data.
    Returns a dict with any of: pay_later_date, cannot_pay_reason,
    target_date, call_back_time, already_paid_date, summary.
    """
    today_str = _date.today().isoformat()
    raw: dict[str, str] = {}

    if final_state in ("future_confirm", "partial_confirm"):
        if ctx.get("payment_date"):
            raw["pay_later_date"] = ctx["payment_date"]
    if final_state == "already_paid_confirm":
        if ctx.get("payment_date"):
            raw["already_paid_date"] = ctx["payment_date"]
    if ctx.get("callback_time"):
        raw["call_back_time"] = ctx["callback_time"]
        raw["target_date"]    = ctx["callback_time"]
    if ctx.get("refusal_reason"):
        raw["cannot_pay_reason"] = ctx["refusal_reason"]

    customer = ctx.get("customer_name", "customer")
    phone    = ctx.get("phone_number", "")
    loan_id  = ctx.get("loan_id", "")
    emi      = ctx.get("emi_overdue_amt") or ctx.get("emi_amount", "")
    emi_date = ctx.get("emi_overdue_date") or ctx.get("emi_due_date", "")

    try:
        prompt = (
            f"EMI collection call ended. Today: {today_str}\n"
            f"Customer: {customer}, Phone: {phone}, Loan ID: {loan_id}\n"
            f"Overdue EMI: ₹{emi} (due {emi_date}), Final state: {final_state}\n"
            f"Captured data (raw): {json.dumps(raw, ensure_ascii=False)}\n\n"
            "Output a JSON object with ONLY the applicable fields:\n"
            "- pay_later_date: ISO date YYYY-MM-DD when customer will pay\n"
            "- cannot_pay_reason: 5-10 word English phrase\n"
            "- target_date: ISO date YYYY-MM-DD (derive from callback time phrase)\n"
            "- call_back_time: clean callback phrase as-is\n"
            "- already_paid_date: ISO date YYYY-MM-DD\n"
            "- summary: one-line English outcome summary\n"
            "Output ONLY valid JSON, no extra text."
        )
        resp = await _oai_llm.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=200,
        )
        raw_out = (resp.choices[0].message.content or "").strip()
        m = re.search(r'\{.*?\}', raw_out, re.DOTALL)
        if m:
            result = json.loads(m.group())
            log.info("CALL_VARS %s", result)
            return result
    except Exception as exc:
        log.warning("finalize_call_variables error: %s", exc)

    return raw


# ─────────────────────────────────────────────────────────────────────────────
# TTS — Sarvam Bulbul v3
# ─────────────────────────────────────────────────────────────────────────────
def _tts_payload(text: str) -> dict:
    return {
        "text":                 text,
        "target_language_code": "hi-IN",
        "speaker":              SARVAM_VOICE,
        "model":                "bulbul:v3",
        "speech_sample_rate":   8000,
        "output_audio_codec":   "mulaw",
        "enable_preprocessing": True,
        "pace":                 TTS_PACE,
    }


def _tts_headers() -> dict[str, str]:
    return {"api-subscription-key": SARVAM_API_KEY, "Content-Type": "application/json"}


def _strip_wav_header(audio: bytes) -> bytes:
    if audio.startswith(b"RIFF"):
        idx = audio.find(b"data")
        if idx != -1:
            return audio[idx + 8:]
    return audio


async def tts_stream(
    text: str,
    on_chunk: Callable[[bytes], Awaitable[None]],
    abort: list[bool],
) -> bool:
    try:
        async with _http.stream(
            "POST", SARVAM_TTS_STREAM_URL,
            json=_tts_payload(text), headers=_tts_headers(),
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                log.error("TTS HTTP %d: %s", resp.status_code, body[:200])
                return False

            container: str | None = None
            wav_buf = b""
            wav_started = False
            got_audio = False

            async for chunk in resp.aiter_bytes(8192):
                if abort[0]:
                    return True
                if not chunk:
                    continue
                if container is None:
                    probe = chunk[:12]
                    if probe.startswith(b"RIFF") and b"WAVE" in probe:
                        container = "wav"
                    elif probe[:1] in (b"{", b"["):
                        container = "json"
                    else:
                        container = "raw"

                if container == "raw":
                    got_audio = True
                    await on_chunk(chunk)
                elif container == "json":
                    tail = await resp.aread()
                    try:
                        data = json.loads((chunk + tail).decode("utf-8", errors="ignore"))
                        b64  = (data.get("audios") or [None])[0] or data.get("audio")
                        if b64:
                            got_audio = True
                            await on_chunk(base64.b64decode(b64))
                    except Exception as exc:
                        log.warning("TTS JSON decode: %s", exc)
                    return got_audio
                else:
                    if not wav_started:
                        wav_buf += chunk
                        idx = wav_buf.find(b"data")
                        if idx == -1:
                            continue
                        start = idx + 8
                        audio = wav_buf[start:]
                        wav_buf = b""
                        wav_started = True
                        if audio:
                            got_audio = True
                            await on_chunk(audio)
                    else:
                        got_audio = True
                        await on_chunk(chunk)

        return got_audio
    except Exception as exc:
        log.error("TTS stream error: %s", exc)
        return False


async def tts_rest(text: str) -> bytes:
    r = await _http.post(SARVAM_TTS_REST_URL, json=_tts_payload(text), headers=_tts_headers())
    if r.status_code >= 400:
        raise RuntimeError(f"TTS REST {r.status_code}: {r.text[:200]}")
    data = r.json()
    b64  = (data.get("audios") or [None])[0] or data.get("audio")
    if not b64:
        raise RuntimeError("TTS REST: no audio in response")
    return base64.b64decode(b64)


# ─────────────────────────────────────────────────────────────────────────────
# Call session
# ─────────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class CallSession:
    ctx:              dict[str, str]   # customer data + dynamic per-turn values
    stream_sid:       str  = ""
    call_sid:         str  = ""
    state:            str  = "opening"
    done:             bool = False
    speaking:         bool  = False
    tts_started_at:   float = 0.0  # monotonic time when current TTS play started
    unclear_count:    int  = 0
    marks_out:        int  = 0
    last_queued:      str  = ""
    last_interim:     str  = ""
    transcript_path:  str  = ""
    # workflow state
    partial_attempts:   int  = 0   # retries in partial_ask_amount
    date_retries:       int  = 0   # retries in date-capture states
    refusal_attempts:   int  = 0   # escalation counter in refusal flow
    partial_offered:    bool = False  # ensure partial offered only once
    barge_in_active:    bool = False  # True while collecting post-barge-in utterance
    beyond_90_warned:   bool = False  # True after first "beyond 90 days" warning


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Aditi — Hindi EMI Collection Voice Bot")
_pending_ctx: dict[str, dict[str, str]] = {}


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse("<h3>Aditi — Sarvam STT · Sarvam-M LLM · Sarvam TTS</h3>")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "llm": LLM_MODEL, "voice": SARVAM_VOICE})


@app.post("/make-call")
async def make_call(
    request: Request,
    x_api_key: str = Header(default=""),
) -> JSONResponse:
    if MAKE_CALL_API_KEY and x_api_key != MAKE_CALL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Api-Key")
    body = await request.json()
    to   = (body.get("to") or "").strip()
    if not to:
        raise HTTPException(status_code=422, detail="`to` (phone number) is required")
    ctx  = {**_build_default_ctx(), **{k: str(v) for k, v in body.items() if k != "to"}}
    # Capture the dialed number into ctx so it's available in call summary
    ctx.setdefault("phone_number", to)
    # Keep aliases in sync if caller sent the new field names
    if ctx.get("emi_overdue_amt"):
        ctx["emi_amount"]     = ctx["emi_overdue_amt"]
    if ctx.get("emi_overdue_amt_int"):
        ctx["emi_amount_int"] = ctx["emi_overdue_amt_int"]
    if ctx.get("emi_overdue_date"):
        ctx["emi_due_date"]   = ctx["emi_overdue_date"]
    call = await asyncio.to_thread(
        lambda: TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).calls.create(
            url=f"{NGROK_URL}/outgoing-call", to=to, from_=TWILIO_PHONE_NUMBER,
        )
    )
    _pending_ctx[call.sid] = ctx
    log.info("Outbound call initiated — SID=%s to=%s", call.sid, to)
    return JSONResponse({"call_sid": call.sid})


@app.api_route("/outgoing-call", methods=["GET", "POST"])
async def outgoing_call(request: Request) -> HTMLResponse:
    resp = VoiceResponse()
    resp.pause(length=1)
    conn = Connect()
    conn.stream(url=f"wss://{request.url.hostname}/media-stream")
    resp.append(conn)
    return HTMLResponse(str(resp), media_type="application/xml")


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket — main call handler
# ─────────────────────────────────────────────────────────────────────────────
@app.websocket("/media-stream")
async def media_stream(twilio_ws: WebSocket) -> None:
    await twilio_ws.accept()
    log.info("Twilio media stream connected")

    sess  = CallSession(ctx=_build_default_ctx())
    abort = [False]
    _fallback_task: list[asyncio.Task | None] = [None]
    utt_q: asyncio.Queue[str] = asyncio.Queue()
    drained = asyncio.Event()
    drained.set()

    # Per-call spectral denoiser (CPU-bound → runs in thread pool)
    _denoiser  = (
        _StreamDenoiser(
            prop_decrease=DENOISE_STRENGTH,
            profile_sec=DENOISE_PROFILE_SEC,
            stationary=DENOISE_STATIONARY,
        )
        if DENOISE_ENABLED else None
    )
    _denoise_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    _loop      = asyncio.get_event_loop()

    # Per-call WebRTC VAD noise gate — runs AFTER denoising
    # Each Twilio mulaw frame (160 bytes) → 320 bytes PCM16 = exactly 20 ms at 8 kHz
    _vad_inst          = webrtcvad.Vad(VAD_MODE) if VAD_ENABLED else None
    _vad_hangover_left = [0]  # frames remaining in hangover window
    _VAD_HANGOVER_FRAMES = max(1, VAD_HANGOVER_MS // 20)

    try:
        stt_ws = await websockets.connect(
            SARVAM_STT_WS_URL,
            extra_headers={"Api-Subscription-Key": SARVAM_API_KEY},
        )
    except Exception as exc:
        log.error("Cannot connect to Sarvam STT: %s", exc)
        await twilio_ws.close()
        return

    try:
        # ── Transcript logger ────────────────────────────────────────────────
        def record(event: str, **fields: Any) -> None:
            if not sess.transcript_path:
                ts  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
                sid = sess.call_sid.replace("/", "_") or "unknown"
                Path(TRANSCRIPTS_DIR).mkdir(parents=True, exist_ok=True)
                sess.transcript_path = f"{TRANSCRIPTS_DIR}/{ts}_{sid}.jsonl"
            row = {
                "ts":    datetime.now(timezone.utc).isoformat(timespec="milliseconds") + "Z",
                "event": event, "state": sess.state, "sid": sess.call_sid, **fields,
            }
            try:
                with open(sess.transcript_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError:
                pass

        # ── Push µ-law frames to Twilio ──────────────────────────────────────
        async def push(chunk: bytes) -> None:
            if sess.done or abort[0] or not sess.stream_sid:
                return
            try:
                for offset in range(0, len(chunk), 160):
                    if sess.done or abort[0]:
                        return
                    frame = chunk[offset:offset + 160]
                    if not frame:
                        return
                    await twilio_ws.send_json({
                        "event": "media", "streamSid": sess.stream_sid,
                        "media": {"payload": base64.b64encode(frame).decode()},
                    })
                    await asyncio.sleep(0)
            except Exception:
                pass

        async def send_mark() -> None:
            if sess.done or not sess.stream_sid:
                return
            drained.clear()
            sess.marks_out += 1
            try:
                await twilio_ws.send_json({
                    "event": "mark", "streamSid": sess.stream_sid,
                    "mark":  {"name": "tts_done"},
                })
            except Exception:
                pass

        async def send_clear() -> None:
            """Flush Twilio's playback buffer — stops the user hearing TTS tail after barge-in."""
            if sess.done or not sess.stream_sid:
                return
            try:
                await twilio_ws.send_json({
                    "event": "clear",
                    "streamSid": sess.stream_sid,
                })
                sess.marks_out = 0
                drained.set()
            except Exception:
                pass

        def _cancel_fallback() -> None:
            t = _fallback_task[0]
            if t and not t.done():
                t.cancel()
            _fallback_task[0] = None

        async def _queue_after_silence(text: str) -> None:
            """1.5 s silence timer — primary utterance trigger (END_SPEECH is unreliable)."""
            try:
                await asyncio.sleep(POST_UTTERANCE_PAUSE_SEC)
            except asyncio.CancelledError:
                return
            sess.barge_in_active = False
            if text and not sess.done and text != sess.last_queued:
                log.info("[USER] %s", text)
                sess.last_queued  = text
                sess.last_interim = ""
                record("user", text=text)
                await utt_q.put(text)

        # ── TTS ───────────────────────────────────────────────────────────────
        async def play_tts(text: str) -> None:
            text = text.strip()
            if not text or sess.done or not sess.stream_sid:
                return
            t0 = time.perf_counter()
            abort[0] = False
            sess.barge_in_active = False  # reset before each new TTS play
            sess.tts_started_at = time.monotonic()
            sess.speaking = True
            try:
                ok = await tts_stream(text, push, abort)
                if not ok and not sess.done:
                    log.warning("TTS stream failed — REST fallback")
                    audio = await tts_rest(text)
                    await push(_strip_wav_header(audio))
                if not sess.done:
                    await send_mark()
            except Exception as exc:
                log.error("play_tts error: %s", exc)
            finally:
                sess.speaking = False
            log.info("TTS %.0f ms | %.60s", (time.perf_counter() - t0) * 1000, text)

        class _SafeMap(dict):
            def __missing__(self, key: str) -> str:
                return f"{{{key}}}"

        async def speak(text: str) -> None:
            text = text.format_map(_SafeMap(sess.ctx))
            log.info("[ADITI] %s", text)
            record("bot", text=text)
            await play_tts(text)

        async def speak_state(state: str) -> None:
            script = _SCRIPTS.get(state, "")
            if script:
                await speak(script)

        # ── Hangup ───────────────────────────────────────────────────────────
        async def hangup(reason: str = "unknown") -> None:
            if sess.done:
                return
            sess.done = True
            abort[0]  = True
            log.info("Hangup: %s", reason)
            record("hangup", reason=reason)
            if sess.marks_out > 0:
                try:
                    await asyncio.wait_for(drained.wait(), timeout=7.0)
                except asyncio.TimeoutError:
                    log.warning("Hangup: audio drain timeout")
            # Normalise and persist call outcome variables
            try:
                call_vars = await finalize_call_variables(reason, sess.ctx)
                if call_vars:
                    record("call_summary", **call_vars)
            except Exception as exc:
                log.warning("call vars error: %s", exc)
            await asyncio.sleep(HANGUP_GRACE_SEC)
            if sess.call_sid:
                try:
                    _sid = sess.call_sid
                    await asyncio.to_thread(
                        lambda: TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                                .calls(_sid).update(status="completed")
                    )
                    log.info("Call %s terminated", sess.call_sid)
                except Exception as exc:
                    log.error("Twilio hangup error: %s", exc)
            for ws in (stt_ws, twilio_ws):
                try:
                    await ws.close()
                except Exception:
                    pass

        # ── Transition helper ────────────────────────────────────────────────
        async def go(state: str, *, reset_unclear: bool = True) -> None:
            """Speak the scripted line for state, hang up if terminal."""
            log.info("FSM %s → %s", sess.state, state)
            sess.state = state
            if reset_unclear:
                sess.unclear_count = 0
            await speak_state(state)
            if state in _TERMINAL:
                asyncio.create_task(hangup(state))

        # ── Twilio receiver ──────────────────────────────────────────────────
        async def recv_twilio() -> None:
            opened = False
            try:
                async for raw in twilio_ws.iter_text():
                    if sess.done:
                        break
                    data = json.loads(raw)
                    evt  = data.get("event")

                    if evt == "start":
                        sess.stream_sid = data["start"]["streamSid"]
                        sess.call_sid   = data["start"].get("callSid", "")
                        if sess.call_sid in _pending_ctx:
                            sess.ctx = _pending_ctx.pop(sess.call_sid)
                        log.info("Stream=%s Call=%s", sess.stream_sid, sess.call_sid)
                        record("call_start")
                        if not opened:
                            opened = True
                            asyncio.create_task(speak_state("opening"))

                    elif evt == "media":
                        if sess.done:
                            continue
                        raw_audio = base64.b64decode(data["media"]["payload"])
                        try:
                            pcm16 = audioop.ulaw2lin(raw_audio, 2)
                            # Step 1: spectral noise cancellation (in thread pool)
                            if _denoiser is not None:
                                pcm16 = await _loop.run_in_executor(
                                    _denoise_pool, _denoiser.feed_sync, pcm16
                                )
                            # Step 2: VAD gate — mute frames that contain no speech
                            if _vad_inst is not None and len(pcm16) == 320:
                                try:
                                    is_speech = _vad_inst.is_speech(pcm16, 8000)
                                except Exception:
                                    is_speech = True  # on error, pass through
                                if is_speech:
                                    _vad_hangover_left[0] = _VAD_HANGOVER_FRAMES
                                elif _vad_hangover_left[0] > 0:
                                    _vad_hangover_left[0] -= 1
                                else:
                                    pcm16 = b"\x00" * 320  # silence non-speech frame
                            await stt_ws.send(json.dumps({
                                "audio": {
                                    "data":        base64.b64encode(pcm16).decode(),
                                    "sample_rate": 8000,
                                    "encoding":    "audio/wav",
                                }
                            }))
                        except (ws_exc.ConnectionClosedOK, ws_exc.ConnectionClosed):
                            break
                        except Exception as exc:
                            log.error("STT send error: %s", exc)
                            break

                    elif evt == "mark":
                        if sess.marks_out > 0:
                            sess.marks_out -= 1
                        if sess.marks_out <= 0:
                            sess.marks_out = 0
                            drained.set()

            except WebSocketDisconnect:
                log.info("Twilio WS disconnected")
            except RuntimeError as exc:
                if "WebSocket is not connected" not in str(exc):
                    raise
            finally:
                try:
                    await stt_ws.close()
                except Exception:
                    pass

        # ── Sarvam STT receiver ──────────────────────────────────────────────
        async def recv_sarvam_stt() -> None:
            try:
                async for msg in stt_ws:
                    if sess.done:
                        break
                    if isinstance(msg, bytes):
                        continue
                    try:
                        frame = json.loads(msg)
                    except Exception:
                        continue

                    msg_type = str(frame.get("type", "")).lower()
                    inner    = frame.get("data") if isinstance(frame.get("data"), dict) else {}

                    if msg_type == "events":
                        signal = str(inner.get("signal_type", "")).upper()
                        if signal == "START_SPEECH" and sess.speaking:
                            _guard_elapsed = time.monotonic() - sess.tts_started_at
                            if sess.state in _BARGE_IN_LOCKED:
                                log.debug("Barge-in suppressed (locked state: %s)", sess.state)
                            elif _guard_elapsed < BARGE_IN_GUARD_SEC:
                                log.debug("Barge-in suppressed (guard %.0f ms)", _guard_elapsed * 1000)
                            else:
                                log.info("Barge-in detected — aborting TTS")
                                abort[0] = True
                                sess.barge_in_active = True
                                await send_clear()
                        elif signal == "END_SPEECH":
                            _cancel_fallback()
                            sess.barge_in_active = False
                            pending = sess.last_interim.strip()
                            if pending and not sess.done and pending != sess.last_queued:
                                log.info("[USER END_SPEECH] %s", pending)
                                sess.last_queued  = pending
                                sess.last_interim = ""
                                record("user", text=pending)
                                await utt_q.put(pending)
                        continue

                    if msg_type == "error":
                        log.error("STT error: %s", frame)
                        continue

                    transcript = (
                        inner.get("transcript")
                        or frame.get("transcript")
                        or frame.get("text")
                        or ""
                    ).strip()

                    is_final = bool(
                        msg_type == "data"
                        or frame.get("is_final")
                        or frame.get("speech_final")
                        or frame.get("final")
                    )

                    if not transcript:
                        continue

                    if is_final:
                        if sess.done or transcript == sess.last_queued:
                            continue
                        if sess.speaking:
                            _guard_elapsed = time.monotonic() - sess.tts_started_at
                            if sess.state in _BARGE_IN_LOCKED:
                                log.debug("Barge-in suppressed (locked state %s): %s", sess.state, transcript)
                                continue
                            elif _guard_elapsed < BARGE_IN_GUARD_SEC:
                                log.debug("Barge-in suppressed (guard %.0f ms): %s", _guard_elapsed * 1000, transcript)
                                continue
                            else:
                                # is_final arrived while speaking and guard expired — barge-in
                                abort[0] = True
                                await send_clear()
                                sess.barge_in_active = True
                                log.info("Barge-in detected via is_final")
                        # All paths: update last_interim and (re)start 1.5 s silence timer.
                        # Timer fires if END_SPEECH never arrives; END_SPEECH cancels it early.
                        if transcript != sess.last_interim:
                            sess.last_interim = transcript
                        _cancel_fallback()
                        _fallback_task[0] = asyncio.create_task(
                            _queue_after_silence(transcript)
                        )
                        if sess.barge_in_active:
                            log.info("Barge-in fragment (timer armed): %s", transcript)
                        else:
                            log.debug("[USER~final] %s", transcript)
                    else:
                        if transcript != sess.last_interim:
                            log.debug("[USER~] %s", transcript)
                            sess.last_interim = transcript

            except Exception as exc:
                if not sess.done:
                    log.error("STT recv error: %s", exc)
            finally:
                if not sess.done:
                    log.error("STT WebSocket closed unexpectedly — ending call")
                    asyncio.create_task(hangup("stt_failure"))

        # ── FSM ───────────────────────────────────────────────────────────────
        async def fsm() -> None:

            async def handle_unclear() -> bool:
                sess.unclear_count += 1
                target = (
                    "unclear_sm1"  if sess.unclear_count == 1 else
                    "unclear_sm2"  if sess.unclear_count == 2 else
                    "unclear_close"
                )
                old = sess.state
                sess.state = target
                log.info("FSM unclear %d: %s → %s", sess.unclear_count, old, target)
                record("unclear", target=target)
                await speak_state(target)
                if target == "unclear_close":
                    asyncio.create_task(hangup("no_response"))
                    return True
                sess.state = _AUTO_ADVANCE.get(target, "opening")
                return False

            async def reroute_intent(utterance: str) -> tuple[bool, bool]:
                """
                Called when a data-capture state gets something unexpected.
                Uses LLM to check if the user changed their mind.
                Returns (was_handled, should_break_loop).
                """
                intent = await classify(utterance, "opening")
                record("classify", action=intent, context="reroute")
                if intent == "goto_pay_now":
                    await go("full_payment_today")
                    return True, True
                if intent in ("goto_refusal", "goto_financial_difficulty"):
                    await go("refusal_ask_reason")
                    return True, False
                if intent == "goto_callback":
                    if _is_callback_time(utterance):
                        sess.ctx["callback_time"] = await extract_callback_time(utterance)
                        await go("callback_confirm")
                        return True, True
                    await go("callback_ask_time")
                    return True, False
                if intent == "goto_future_promise":
                    d = _parse_date(utterance)
                    _today = _date.today()
                    if d and d > _today:
                        sess.ctx["payment_date"] = _fmt_date(
                            min(d, _today + timedelta(days=90))
                        )
                        await go("future_confirm")
                        return True, True
                    await go("future_ask_date")
                    return True, False
                if intent == "goto_death":
                    await go("death_response")
                    return True, True
                if intent == "goto_already_paid":
                    await go("already_paid_ask_date")
                    return True, False
                return False, False

            while not sess.done:
                try:
                    utterance = await asyncio.wait_for(utt_q.get(), timeout=SILENCE_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    if sess.done:
                        break
                    log.info("FSM: %.0f s silence in state=%s", SILENCE_TIMEOUT_SEC, sess.state)
                    if await handle_unclear():
                        break
                    continue

                if sess.done:
                    break

                # Drain any follow-up fragments (e.g. late is_final for same sentence)
                while not utt_q.empty():
                    try:
                        utterance = utt_q.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                utterance = utterance.strip() or "[silence]"
                record("user_turn", text=utterance)
                log.debug("FSM state=%s utterance=%r", sess.state, utterance)

                # ── FAQ check (cross-state) ───────────────────────────────────
                # If user asks a factual question (EMI amount, due date, loan ID)
                # answer inline and re-prompt the current question, then loop.
                if utterance != "[silence]" and _FAQ_TRIGGER_RE.search(utterance):
                    faq_key = await check_faq(utterance)
                    if faq_key and faq_key in _SCRIPTS:
                        record("faq", key=faq_key)
                        log.info("FAQ detected: %s", faq_key)
                        faq_text = _SCRIPTS[faq_key]
                        # For states other than opening, combine answer + re-prompt
                        if sess.state not in ("opening",) and sess.state in _SCRIPTS:
                            combined = faq_text + " " + _SCRIPTS[sess.state]
                            await speak(combined)
                        else:
                            await speak(faq_text)
                        continue

                # ══════════════════════════════════════════════════════════════
                # ROOT: OPENING
                # ══════════════════════════════════════════════════════════════
                if sess.state == "opening":
                    action = await classify(utterance, "opening")
                    record("classify", action=action)

                    if action == "goto_pay_now":
                        await go("full_payment_today")
                        break

                    elif action == "goto_partial":
                        sess.partial_offered = True
                        await go("partial_ask_amount")

                    elif action == "goto_future_promise":
                        # Try to extract date from the same utterance (e.g. "अगले महीने")
                        d = _parse_date(utterance)
                        _today = _date.today()
                        if d and d > _today:
                            if d > _today + timedelta(days=90):
                                d = _today + timedelta(days=90)
                            sess.ctx["payment_date"] = _fmt_date(d)
                            await go("future_confirm")
                            break
                        await go("future_ask_date")

                    elif action == "goto_already_paid":
                        await go("already_paid_ask_date")

                    elif action == "goto_callback":
                        # If user already gave a time, extract it now so we skip callback_ask_time
                        if _is_callback_time(utterance):
                            sess.ctx["callback_time"] = await extract_callback_time(utterance)
                            await go("callback_confirm")
                            break
                        await go("callback_ask_time")

                    elif action == "goto_death":
                        await go("death_response")
                        break

                    elif action == "goto_financial_difficulty":
                        if not sess.partial_offered:
                            sess.partial_offered = True
                            await go("offer_partial")
                        else:
                            # partial already offered once — go to refusal flow
                            await go("refusal_ask_reason")

                    elif action == "goto_refusal":
                        await go("refusal_ask_reason")

                    elif action == "mark_unclear":
                        if await handle_unclear():
                            break

                # ══════════════════════════════════════════════════════════════
                # C2 BRIDGE: OFFER PARTIAL (one-time after financial difficulty)
                # ══════════════════════════════════════════════════════════════
                elif sess.state == "offer_partial":
                    action = await classify(utterance, "offer_partial")
                    record("classify", action=action)

                    if action == "goto_partial_yes":
                        await go("partial_ask_amount")
                    elif action == "goto_callback":
                        if _is_callback_time(utterance):
                            sess.ctx["callback_time"] = await extract_callback_time(utterance)
                            await go("callback_confirm")
                            break
                        await go("callback_ask_time")
                    else:
                        # declined partial → refusal flow
                        await go("refusal_ask_reason")

                # ══════════════════════════════════════════════════════════════
                # C2: PARTIAL PAYMENT — capture amount
                # ══════════════════════════════════════════════════════════════
                elif sess.state == "partial_ask_amount":
                    # 1. "आधा/half" → half EMI
                    _t_low = utterance.lower()
                    if any(k in _t_low for k in ["aadha", "aadhe", "aadhi", "आधा", "आधे", "half"]):
                        emi_int = int(re.sub(r"[^0-9]", "", sess.ctx.get("emi_amount_int", "8500")))
                        amount  = emi_int // 2
                    else:
                        # 2. Regex extraction
                        amount = _parse_amount(utterance)
                        # 3. LLM fallback (catches word-numerals regex misses)
                        if amount is None:
                            amount = await llm_extract_amount(utterance)

                    min_p = int(sess.ctx.get("min_partial_int", "1500"))

                    if amount is None:
                        # 4. LLM intent check — user may have changed their mind
                        handled, should_break = await reroute_intent(utterance)
                        if handled:
                            if should_break: break
                            continue
                        sess.partial_attempts += 1
                        if sess.partial_attempts >= 2:
                            await go("refusal_ask_reason")
                        else:
                            await speak_state("partial_amount_unclear")
                        continue

                    if amount < min_p:
                        sess.partial_attempts += 1
                        if sess.partial_attempts >= 2:
                            await go("refusal_ask_reason")
                        else:
                            await speak_state("partial_amount_too_low")
                        continue

                    emi_int = int(re.sub(r"[^0-9]", "", sess.ctx.get("emi_amount_int", "8500")))
                    remaining = max(0, emi_int - amount)
                    sess.ctx["partial_amount"]   = f"{amount:,}"
                    sess.ctx["remaining_balance"] = f"{remaining:,}"
                    sess.partial_attempts = 0
                    await go("partial_ask_remaining_date")

                # ── C2: capture remaining balance date ────────────────────────
                elif sess.state == "partial_ask_remaining_date":
                    d = _parse_date(utterance)
                    today = _date.today()

                    if d is None:
                        handled, should_break = await reroute_intent(utterance)
                        if handled:
                            if should_break: break
                            continue
                        sess.date_retries += 1
                        if sess.date_retries >= 2:
                            d = today + timedelta(days=7)
                        else:
                            await speak_state("partial_date_retry")
                            continue

                    if d < today:
                        d = today + timedelta(days=7)
                    if d > today + timedelta(days=90):
                        if not sess.beyond_90_warned:
                            sess.beyond_90_warned = True
                            await speak_state("date_beyond_90")
                            continue
                        # Second time — cap silently and accept
                        d = today + timedelta(days=90)

                    sess.ctx["payment_date"] = _fmt_date(d)
                    sess.date_retries = 0
                    sess.beyond_90_warned = False
                    await go("partial_confirm")
                    break

                # ══════════════════════════════════════════════════════════════
                # C3: FUTURE PAYMENT PROMISE — capture date
                # ══════════════════════════════════════════════════════════════
                elif sess.state == "future_ask_date":
                    d = _parse_date(utterance)
                    today = _date.today()

                    if d is None:
                        # LLM intent check first — user may have pivoted
                        handled, should_break = await reroute_intent(utterance)
                        if handled:
                            if should_break: break
                            continue
                        sess.date_retries += 1
                        if sess.date_retries >= 2:
                            d = today + timedelta(days=7)
                        else:
                            await speak_state("future_date_retry")
                            continue

                    if d < today:
                        d = today
                    if d > today + timedelta(days=90):
                        if not sess.beyond_90_warned:
                            sess.beyond_90_warned = True
                            await speak_state("future_date_beyond_90")
                            continue
                        # Second time — cap silently and accept
                        d = today + timedelta(days=90)

                    sess.ctx["payment_date"] = _fmt_date(d)
                    sess.date_retries = 0
                    sess.beyond_90_warned = False
                    await go("future_confirm")
                    break

                # ══════════════════════════════════════════════════════════════
                # C5: REFUSAL — ask reason (first attempt)
                # ══════════════════════════════════════════════════════════════
                elif sess.state == "refusal_ask_reason":
                    action = await classify(utterance, "refusal_ask_reason")
                    record("classify", action=action)

                    if action == "got_reason":
                        sess.ctx["refusal_reason"] = utterance[:120]
                        await go("refusal_credit_warn")

                    else:  # still_refusing
                        sess.refusal_attempts += 1
                        if sess.refusal_attempts >= 1:
                            await go("refusal_escalate")
                        else:
                            await speak_state("refusal_ask_reason")

                # ── C5: refusal escalate (second attempt) ─────────────────────
                elif sess.state == "refusal_escalate":
                    action = await classify(utterance, "refusal_escalate")
                    record("classify", action=action)

                    if action == "got_reason":
                        sess.ctx["refusal_reason"] = utterance[:120]
                    else:
                        sess.ctx.setdefault("refusal_reason", "Unspecified")

                    await go("refusal_credit_warn")

                # ── C5: credit warning + ask callback time ────────────────────
                elif sess.state == "refusal_credit_warn":
                    if _is_callback_time(utterance):
                        sess.ctx["callback_time"] = await extract_callback_time(utterance)
                    else:
                        # Bare ack / silence — default, don't store "हाँ" verbatim
                        sess.ctx["callback_time"] = "जल्द ही"
                    await go("refusal_close")
                    break

                # ══════════════════════════════════════════════════════════════
                # C6: ALREADY PAID — capture date
                # ══════════════════════════════════════════════════════════════
                elif sess.state == "already_paid_ask_date":
                    d = _parse_date(utterance)
                    today = _date.today()

                    if d is None:
                        # LLM intent check — user may have been mistaken or changed mind
                        handled, should_break = await reroute_intent(utterance)
                        if handled:
                            if should_break: break
                            continue
                        sess.date_retries += 1
                        if sess.date_retries >= 2:
                            sess.ctx["payment_date"] = "recently"
                            sess.date_retries = 0
                            await go("already_paid_ask_mode")
                        else:
                            await speak_state("already_paid_date_unclear")
                        continue

                    if d > today:
                        sess.date_retries += 1
                        if sess.date_retries >= 2:
                            sess.ctx["payment_date"] = "recently"
                            sess.date_retries = 0
                            await go("already_paid_ask_mode")
                        else:
                            await speak_state("already_paid_date_invalid")
                        continue

                    sess.ctx["payment_date"] = _fmt_date(d)
                    sess.date_retries = 0
                    await go("already_paid_ask_mode")

                # ── C6: capture payment mode ──────────────────────────────────
                elif sess.state == "already_paid_ask_mode":
                    # Accept any substantive response
                    sess.ctx["payment_mode"] = utterance[:80] if utterance != "[silence]" else "not specified"
                    await go("already_paid_confirm")
                    break

                # ══════════════════════════════════════════════════════════════
                # C7: CALLBACK — capture time
                # ══════════════════════════════════════════════════════════════
                elif sess.state == "callback_ask_time":
                    today = _date.today()

                    if _is_callback_time(utterance):
                        # LLM extracts a clean speakable phrase — preserves time of day
                        sess.ctx["callback_time"] = await extract_callback_time(utterance)
                        sess.date_retries = 0
                    else:
                        # No recognisable time — retry once, then default
                        sess.date_retries += 1
                        if sess.date_retries >= 2:
                            sess.ctx["callback_time"] = "जल्द ही"
                        else:
                            await speak_state("callback_time_unclear")
                            continue

                    await go("callback_confirm")
                    break

                # ══════════════════════════════════════════════════════════════
                # C5 continuation: credit warn auto-entered via speak + next utt
                # (refusal_credit_warn is not an auto-advance state; it waits for
                #  the callback time response in the loop above)
                # ══════════════════════════════════════════════════════════════

                else:
                    log.warning("FSM: unhandled state %s — skipping utterance", sess.state)

                if sess.done:
                    break

            log.info("FSM ended (state=%s)", sess.state)

        await asyncio.gather(recv_twilio(), recv_sarvam_stt(), fsm())

    finally:
        _denoise_pool.shutdown(wait=False)
        try:
            await stt_ws.close()
        except Exception:
            pass
        log.info("Media stream handler closed")


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────
def _place_call(to: str, ctx: dict[str, str]) -> str:
    call = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).calls.create(
        url=f"{NGROK_URL}/outgoing-call", to=to, from_=TWILIO_PHONE_NUMBER,
    )
    _pending_ctx[call.sid] = ctx
    log.info("Call SID: %s", call.sid)
    return call.sid


if __name__ == "__main__":
    import uvicorn

    to = sys.argv[1].strip() if len(sys.argv) > 1 else os.getenv("CALL_TO", "")
    if not to:
        print("Usage: python main.py <phone_number>")
        sys.exit(1)

    log.info("aditi — Dialing %s …", to)

    def _dial() -> None:
        time.sleep(2)
        try:
            _place_call(to, _build_default_ctx())
        except Exception as exc:
            log.error("Dial error: %s", exc)

    threading.Thread(target=_dial, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
