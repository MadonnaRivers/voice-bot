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
LLM_MODEL           = os.getenv("SARVAM_LLM_MODEL",    "sarvam-105b")
SARVAM_VOICE        = os.getenv("SARVAM_VOICE",        "simran")
PORT                = int(os.getenv("PORT",             "5050"))
TRANSCRIPTS_DIR     = os.getenv("TRANSCRIPTS_DIR",     "transcripts")
MAKE_CALL_API_KEY   = os.getenv("MAKE_CALL_API_KEY",   "")

HANGUP_GRACE_SEC    = float(os.getenv("HANGUP_GRACE_SEC",    "1.5"))
SILENCE_TIMEOUT_SEC = float(os.getenv("SILENCE_TIMEOUT_SEC", "20.0"))
TTS_PACE            = float(os.getenv("TTS_PACE",            "1.1"))

# Noise gate — WebRTC VAD silences non-speech frames before they reach STT
# VAD_MODE: 0 = least aggressive, 3 = most aggressive noise suppression
VAD_MODE           = int(os.getenv("VAD_MODE",           "2"))
VAD_HANGOVER_MS    = int(os.getenv("VAD_HANGOVER_MS",    "300"))  # keep audio N ms after speech ends
VAD_ENABLED        = os.getenv("VAD_ENABLED", "true").lower() not in ("0", "false", "no")

# Spectral noise cancellation — noisereduce (runs in thread pool, ~17 ms / 80 ms chunk)
DENOISE_ENABLED    = os.getenv("DENOISE_ENABLED",    "true").lower() not in ("0", "false", "no")
DENOISE_STRENGTH   = float(os.getenv("DENOISE_STRENGTH",   "0.80"))  # 0.0–1.0
DENOISE_PROFILE_SEC = float(os.getenv("DENOISE_PROFILE_SEC", "1.5")) # seconds of silence at call start used as noise reference

# How long (seconds) the FSM waits after the first transcript arrives before responding.
# Drains any follow-up STT fragments that arrive during this window so the bot
# acts on the complete utterance instead of the first short chunk.
POST_UTTERANCE_PAUSE_SEC = float(os.getenv("POST_UTTERANCE_PAUSE_SEC", "1.8"))

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
class _StreamDenoiser:
    """
    Per-call spectral noise canceller.

    Pipeline:
      Phase 1 (first DENOISE_PROFILE_SEC seconds): collect audio as a noise
        reference — assumes the line is live but the customer hasn't spoken yet.
      Phase 2: process incoming PCM-16 LE in 80 ms chunks via noisereduce
        stationary subtraction; output with ~80 ms delay.

    feed_sync() is CPU-bound; call it via loop.run_in_executor to keep the
    asyncio event loop free.
    """
    CHUNK_SAMP = 640    # 80 ms at 8 kHz — good FFT resolution, fits in ~17 ms CPU

    def __init__(self, sr: int = 8000, prop_decrease: float = 0.80,
                 profile_sec: float = 1.5) -> None:
        self._sr     = sr
        self._prop   = prop_decrease
        self._target = int(sr * profile_sec)
        self._pbuf:   list[np.ndarray] = []
        self._noise:  np.ndarray | None = None
        self._inbuf  = np.zeros(0, dtype=np.float32)
        self._outbuf = np.zeros(0, dtype=np.float32)

    def feed_sync(self, pcm16: bytes) -> bytes:
        chunk = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0

        # ── Phase 1: noise profiling ──────────────────────────────────────────
        if self._noise is None:
            self._pbuf.append(chunk)
            if sum(a.shape[0] for a in self._pbuf) >= self._target:
                self._noise = np.concatenate(self._pbuf)
                self._pbuf.clear()
            return pcm16   # pass through while building profile

        # ── Phase 2: buffer → denoise → output ───────────────────────────────
        self._inbuf = np.concatenate([self._inbuf, chunk])

        if self._inbuf.shape[0] >= self.CHUNK_SAMP:
            seg         = self._inbuf[:self.CHUNK_SAMP]
            self._inbuf = self._inbuf[self.CHUNK_SAMP:]
            try:
                clean = nr.reduce_noise(
                    y=seg, y_noise=self._noise, sr=self._sr,
                    stationary=True, prop_decrease=self._prop,
                    n_fft=256, n_jobs=1,
                )
                self._outbuf = np.concatenate([self._outbuf, clean])
            except Exception:
                self._outbuf = np.concatenate([self._outbuf, seg])

        # Return one frame's worth of processed audio (~80 ms output delay)
        n = chunk.shape[0]
        if self._outbuf.shape[0] >= n:
            out          = self._outbuf[:n]
            self._outbuf = self._outbuf[n:]
            return (np.clip(out, -1.0, 1.0) * 32768).astype(np.int16).tobytes()

        return pcm16   # output buffer not yet primed — pass original through


# ─────────────────────────────────────────────────────────────────────────────
# Shared clients
# ─────────────────────────────────────────────────────────────────────────────
_http = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=4.0, read=15.0, write=5.0, pool=5.0),
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)
_oai = AsyncOpenAI(api_key=SARVAM_API_KEY, base_url=SARVAM_LLM_BASE_URL)


# ─────────────────────────────────────────────────────────────────────────────
# Default customer data — override per-call via POST /make-call body
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_CUSTOMER: dict[str, str] = {
    "customer_name":    os.getenv("DEFAULT_CUSTOMER_NAME",    "Rahul"),
    "lender":           os.getenv("DEFAULT_LENDER",           "Easy Home Finance"),
    "loan_type":        os.getenv("DEFAULT_LOAN_TYPE",        "Home Loan"),
    "loan_id":          os.getenv("DEFAULT_LOAN_ID",          "EH12345"),
    "emi_amount":       os.getenv("DEFAULT_EMI_AMOUNT",       "8,500"),
    "emi_amount_int":   os.getenv("DEFAULT_EMI_AMOUNT_INT",   "8500"),
    "emi_due_date":     os.getenv("DEFAULT_EMI_DUE_DATE",     "5 March 2026"),
    "min_partial":      os.getenv("DEFAULT_MIN_PARTIAL",      "1,500"),
    "min_partial_int":  os.getenv("DEFAULT_MIN_PARTIAL_INT",  "1500"),
    "payment_deadline": os.getenv("DEFAULT_PAYMENT_DEADLINE", "2026-03-20"),
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
        "ठीक है, हम आपसे {callback_time} पर संपर्क करेंगे। "
        + _MANDATORY_CLOSING
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
        "ठीक है, हम आपसे {callback_time} पर संपर्क करेंगे। "
        "कृपया कोशिश करें कि EMI जल्दी चुका दें ताकि जुर्माना न लगे।"
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
          .replace("अगले हफ़्ते", "agle hafte")
          .replace("अगले हफ्ते", "agle hafte")
          .replace("इस हफ्ते", "is hafte").replace("इस हफ़्ते", "is hafte")
          .replace("अगले महीने", "agle mahine")
          .replace("महीने में", "mahine mein").replace("महीने बाद", "mahine baad")
          .replace("हफ़्ते में", "hafte mein").replace("हफ्ते में", "hafte mein")
          .replace("दिनों में", "dinon mein").replace("दिन में", "din mein"))

    if re.search(r"\baaj\b|today", t):
        return today
    if re.search(r"\bkal\b|tomorrow", t):
        return today + timedelta(days=1)
    if re.search(r"\bparso\b|parson\b", t):
        return today + timedelta(days=2)
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


_TRAILING_VERBS = re.compile(
    r"\s*(?:करना|करूंगा|करूँगा|कर दूंगा|कर दूँगा|कर दें|कर दीजिए"
    r"|हो जाएगा|हो जायेगा|बाद में|ठीक है|okay|ok)[।.,\s]*$",
    re.IGNORECASE,
)

def _clean_callback_time(text: str) -> str:
    """Strip trailing filler verbs from a callback-time utterance."""
    cleaned = _TRAILING_VERBS.sub("", text.strip()).strip("।., ")
    return cleaned if cleaned else text[:60]


# ─────────────────────────────────────────────────────────────────────────────
# Classification — rule-based primary, Sarvam-M LLM fallback
# ─────────────────────────────────────────────────────────────────────────────

def _heuristic(utterance: str, state: str) -> str | None:
    """
    Keyword classifier. Returns a transition label or None (→ LLM fallback).
    Only called for states that require intent classification.
    """
    t = utterance.lower().strip()
    if not t or t == "[silence]":
        return "mark_unclear"

    # ── OPENING ──────────────────────────────────────────────────────────────
    if state == "opening":
        # C1: death
        if any(k in t for k in ["maut", "mara", "mar gaye", "expire", "wafat",
                                  "nahi rahe", "guzar gaye", "chale gaye", "निधन",
                                  "मौत", "गुज़र गए", "नहीं रहे"]):
            return "goto_death"

        # C6: already paid
        if any(k in t for k in ["kar diya", "de diya", "bhej diya", "ho gaya", "paid",
                                  "already", "pehle hi", "kar chuka", "kiya tha",
                                  "कर दिया", "दे दिया", "भेज दिया", "हो गया", "पहले ही"]):
            return "goto_already_paid"

        # C7: busy / callback
        if any(k in t for k in ["busy", "vyast", "baad mein", "later", "call back",
                                  "callback", "abhi time nahi", "phir karo",
                                  "बाद में", "अभी time नहीं", "व्यस्त"]):
            return "goto_callback"

        # C2: explicit partial payment offer
        if any(k in t for k in ["partial", "thoda", "kuch amount", "aadha", "half",
                                  "kuch de sakta", "thodi payment",
                                  "थोड़ा", "कुछ", "आधा"]):
            return "goto_partial"

        # C3: future date promise
        if any(k in t for k in ["tarikh", "date tak", "din mein", "hafte mein",
                                  "next week", "kal tak", "parso tak", "is hafte",
                                  "agle hafte", "agle mahine", "mahine mein",
                                  "तारीख", "दिन में", "हफ़्ते में", "कल तक",
                                  "अगले हफ़्ते", "अगले हफ्ते", "अगले महीने",
                                  "महीने में", "महीने बाद", "महीने तक",
                                  "हफ्ते में", "हफ्ते बाद", "हफ्ते तक"]):
            return "goto_future_promise"

        # C4: full payment right now / today
        if any(k in t for k in ["aaj karunga", "aaj kar deta", "abhi karta",
                                  "turant", "abhi pay", "immediately",
                                  "आज करूंगा", "आज कर देता", "अभी करता"]):
            return "goto_pay_now"

        # C5a: financial difficulty (unable) → offer partial first
        if any(k in t for k in ["nahi kar paunga", "nahi ho payega", "mushkil",
                                  "problem hai", "paise nahi", "takleef", "dikkat",
                                  "nahi kar pata", "नहीं कर पाऊंगा", "मुश्किल",
                                  "पैसे नहीं", "तकलीफ़"]):
            return "goto_financial_difficulty"

        # C5b: hard refusal (won't pay)
        if any(k in t for k in ["nahi karunga", "nahi dunga", "band karo", "mat karo",
                                  "mujhe matlab nahi", "nahi bhejna",
                                  "नहीं करूंगा", "नहीं दूंगा", "बंद करो"]):
            return "goto_refusal"

        # Generic yes → full payment
        if any(k in t for k in ["haan", "han", "ha ", "bilkul", "zaroor",
                                  "theek hai", "okay", "ok", "karunga", "kar dunga",
                                  "हाँ", "हां", "बिल्कुल", "ठीक है", "करूंगा"]):
            return "goto_pay_now"

        # Bare नहीं / nahi → financial difficulty (benefit of doubt)
        if "nahi" in t or "नहीं" in t:
            return "goto_financial_difficulty"

        return None  # ambiguous → LLM

    # ── OFFER PARTIAL (after financial difficulty) ────────────────────────────
    elif state == "offer_partial":
        if any(k in t for k in ["haan", "han", "bilkul", "theek hai", "okay", "karunga",
                                  "partial", "de sakta", "kar sakta", "हाँ", "ठीक है"]):
            return "goto_partial_yes"
        return "goto_partial_no"   # any other response = decline

    # ── REFUSAL: ask reason / escalate ───────────────────────────────────────
    elif state in ("refusal_ask_reason", "refusal_escalate"):
        # Clear continued refusal (won't give a reason)
        if any(k in t for k in ["nahi bataunga", "chodo", "band karo", "mat karo",
                                  "koi reason nahi", "nahi bolunga", "nahi chahiye",
                                  "नहीं बताऊंगा", "छोड़ो", "बंद करो"]):
            return "still_refusing"
        # Any other substantive reply = reason given
        words = [w for w in t.split() if w.strip(".,?!।")]
        if len(words) >= 2:
            return "got_reason"
        return "still_refusing"

    return None


_LLM_CLASSIFY_PROMPTS: dict[str, str] = {
    "opening": """\
You are classifying a Hindi/Hinglish customer response in an EMI collection call.

The agent asked: "Aapka EMI pending hai, kab tak kar paayenge?"
The customer said: "{utterance}"

Choose EXACTLY ONE label (output only the label):
GOTO_PAY_NOW              — agrees to pay today or immediately
GOTO_PARTIAL              — offers partial payment explicitly
GOTO_FUTURE_PROMISE       — agrees to pay on a specific future date
GOTO_ALREADY_PAID         — says payment is already done
GOTO_CALLBACK             — busy, wants a callback later
GOTO_DEATH                — mentions death of borrower or family member
GOTO_FINANCIAL_DIFFICULTY — unable to pay, financial difficulty, needs time
GOTO_REFUSAL              — flat-out refuses, no reason given
MARK_UNCLEAR              — completely unintelligible

When between FINANCIAL_DIFFICULTY and REFUSAL, prefer FINANCIAL_DIFFICULTY.""",

    "offer_partial": """\
The agent offered: "Kya aap partial payment kar sakte hain?"
The customer said: "{utterance}"

Output EXACTLY ONE:
GOTO_PARTIAL_YES — accepts partial payment offer
GOTO_PARTIAL_NO  — declines""",

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
    """Rule-based first; Sarvam-M LLM fallback for ambiguous input."""
    result = _heuristic(utterance, state)
    if result is not None:
        log.info("CLASSIFY[rule] %s → %s | %r", state, result, utterance[:60])
        return result

    prompt_template = _LLM_CLASSIFY_PROMPTS.get(state)
    if not prompt_template:
        return "mark_unclear"

    try:
        resp = await _oai.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt_template.format(utterance=utterance)}],
            temperature=0,
            max_tokens=512,
        )
        raw_full = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"<think>.*?</think>", "", raw_full, flags=re.DOTALL | re.IGNORECASE).strip().upper()
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
    speaking:         bool = False
    unclear_count:    int  = 0
    marks_out:        int  = 0
    last_queued:      str  = ""
    last_interim:     str  = ""
    transcript_path:  str  = ""
    # workflow state
    partial_attempts: int  = 0   # retries in partial_ask_amount
    date_retries:     int  = 0   # retries in date-capture states
    refusal_attempts: int  = 0   # escalation counter in refusal flow
    partial_offered:  bool = False  # ensure partial offered only once


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
    ctx  = {**DEFAULT_CUSTOMER, **{k: str(v) for k, v in body.items() if k != "to"}}
    call = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).calls.create(
        url=f"{NGROK_URL}/outgoing-call", to=to, from_=TWILIO_PHONE_NUMBER,
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

    sess  = CallSession(ctx=dict(DEFAULT_CUSTOMER))
    abort = [False]
    utt_q: asyncio.Queue[str] = asyncio.Queue()
    drained = asyncio.Event()
    drained.set()

    # Per-call spectral denoiser (CPU-bound → runs in thread pool)
    _denoiser  = (
        _StreamDenoiser(prop_decrease=DENOISE_STRENGTH, profile_sec=DENOISE_PROFILE_SEC)
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

        # ── TTS ───────────────────────────────────────────────────────────────
        async def play_tts(text: str) -> None:
            text = text.strip()
            if not text or sess.done or not sess.stream_sid:
                return
            t0 = time.perf_counter()
            abort[0] = False
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
            await asyncio.sleep(HANGUP_GRACE_SEC)
            if sess.call_sid:
                try:
                    TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)\
                        .calls(sess.call_sid).update(status="completed")
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
                            log.info("Barge-in detected — aborting TTS")
                            abort[0] = True
                        elif signal == "END_SPEECH":
                            pending = sess.last_interim.strip()
                            if pending and not sess.done and pending != sess.last_queued:
                                log.info("[USER VAD] %s", pending)
                                sess.last_queued  = pending
                                sess.last_interim = ""
                                record("user_vad", text=pending)
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
                            abort[0] = True
                            log.info("Barge-in utterance: %s", transcript)
                        log.info("[USER] %s", transcript)
                        sess.last_queued  = transcript
                        sess.last_interim = ""
                        record("user", text=transcript)
                        await utt_q.put(transcript)
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

                # Wait briefly then drain any follow-up fragments that arrived
                # while Sarvam STT was still finalising the full sentence.
                await asyncio.sleep(POST_UTTERANCE_PAUSE_SEC)
                while not utt_q.empty():
                    try:
                        utterance = utt_q.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                utterance = utterance.strip() or "[silence]"
                record("user_turn", text=utterance)
                log.debug("FSM state=%s utterance=%r", sess.state, utterance)

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
                        # Capture callback time if already mentioned
                        d = _parse_date(utterance)
                        _today = _date.today()
                        if d and d >= _today:
                            sess.ctx["callback_time"] = _fmt_date(d)
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
                    else:
                        # declined partial → refusal flow
                        await go("refusal_ask_reason")

                # ══════════════════════════════════════════════════════════════
                # C2: PARTIAL PAYMENT — capture amount
                # ══════════════════════════════════════════════════════════════
                elif sess.state == "partial_ask_amount":
                    amount = _parse_amount(utterance)
                    min_p  = int(sess.ctx.get("min_partial_int", "1500"))

                    if amount is None:
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
                        sess.date_retries += 1
                        if sess.date_retries >= 2:
                            # default to 7 days out
                            d = today + timedelta(days=7)
                        else:
                            await speak_state("partial_date_retry")
                            continue

                    if d < today:
                        d = today + timedelta(days=7)
                    if d > today + timedelta(days=90):
                        d = today + timedelta(days=90)

                    sess.ctx["payment_date"] = _fmt_date(d)
                    sess.date_retries = 0
                    await go("partial_confirm")
                    break

                # ══════════════════════════════════════════════════════════════
                # C3: FUTURE PAYMENT PROMISE — capture date
                # ══════════════════════════════════════════════════════════════
                elif sess.state == "future_ask_date":
                    d = _parse_date(utterance)
                    today = _date.today()

                    if d is None:
                        sess.date_retries += 1
                        if sess.date_retries >= 2:
                            d = today + timedelta(days=7)
                        else:
                            await speak_state("future_date_retry")
                            continue

                    if d < today:
                        d = today
                    if d > today + timedelta(days=90):
                        d = today + timedelta(days=90)

                    sess.ctx["payment_date"] = _fmt_date(d)
                    sess.date_retries = 0
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
                    # Any response is treated as callback preference
                    d = _parse_date(utterance)
                    if d and d >= _date.today():
                        sess.ctx["callback_time"] = _fmt_date(d)
                    elif utterance and utterance != "[silence]":
                        sess.ctx["callback_time"] = _clean_callback_time(utterance)
                    else:
                        sess.ctx.setdefault("callback_time", "जल्द ही")
                    await go("refusal_close")
                    break

                # ══════════════════════════════════════════════════════════════
                # C6: ALREADY PAID — capture date
                # ══════════════════════════════════════════════════════════════
                elif sess.state == "already_paid_ask_date":
                    d = _parse_date(utterance)
                    today = _date.today()

                    if d is None:
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
                    d = _parse_date(utterance)
                    today = _date.today()

                    if d and d >= today:
                        sess.ctx["callback_time"] = _fmt_date(d)
                        sess.date_retries = 0
                    elif utterance and utterance != "[silence]":
                        sess.ctx["callback_time"] = _clean_callback_time(utterance)
                        sess.date_retries = 0
                    else:
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
            _place_call(to, dict(DEFAULT_CUSTOMER))
        except Exception as exc:
            log.error("Dial error: %s", exc)

    threading.Thread(target=_dial, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
