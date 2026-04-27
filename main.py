"""
Aditi — Hindi EMI Collection Voice Bot
───────────────────────────────────────
STT : Sarvam Saaras v3   (WebSocket, PCM-16 LE @ 8 kHz)
LLM : Sarvam-M           (classification fallback only — thinking model)
TTS : Sarvam Bulbul v3   (HTTP-streaming, µ-law @ 8 kHz → Twilio)

All bot responses are scripted — zero LLM latency on the hot path.
LLM is called only when the rule-based classifier cannot determine intent.

Call flow:
  Twilio PSTN → Media Stream WebSocket (/media-stream)
    ├─ recv_twilio()      mulaw → PCM-16 → Sarvam STT WebSocket
    ├─ recv_sarvam_stt()  VAD + transcripts → utterance queue
    └─ fsm()              utterances → classify → scripted TTS → Twilio
"""
from __future__ import annotations

import asyncio
import audioop
import base64
import dataclasses
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
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
LLM_MODEL           = os.getenv("SARVAM_LLM_MODEL",    "sarvam-m")
SARVAM_VOICE        = os.getenv("SARVAM_VOICE",        "simran")
PORT                = int(os.getenv("PORT",             "5050"))
TRANSCRIPTS_DIR     = os.getenv("TRANSCRIPTS_DIR",     "transcripts")
MAKE_CALL_API_KEY   = os.getenv("MAKE_CALL_API_KEY",   "")

HANGUP_GRACE_SEC    = float(os.getenv("HANGUP_GRACE_SEC",    "1.5"))
SILENCE_TIMEOUT_SEC = float(os.getenv("SILENCE_TIMEOUT_SEC", "20.0"))
TTS_PACE            = float(os.getenv("TTS_PACE",            "1.1"))

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
# Shared clients
# ─────────────────────────────────────────────────────────────────────────────
_http = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=4.0, read=15.0, write=5.0, pool=5.0),
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)
_oai = AsyncOpenAI(api_key=SARVAM_API_KEY, base_url=SARVAM_LLM_BASE_URL)


# ─────────────────────────────────────────────────────────────────────────────
# Default customer data — overridden per-call via POST /make-call body
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_CUSTOMER: dict[str, str] = {
    "customer_name":    os.getenv("DEFAULT_CUSTOMER_NAME",    "Mr. Sharma"),
    "lender":           os.getenv("DEFAULT_LENDER",           "Easy Home Finance"),
    "loan_type":        os.getenv("DEFAULT_LOAN_TYPE",        "Home Loan"),
    "account_last4":    os.getenv("DEFAULT_ACCOUNT_LAST4",    "4321"),
    "emi_due_date":     os.getenv("DEFAULT_EMI_DUE_DATE",     "5"),
    "emi_amount":       os.getenv("DEFAULT_EMI_AMOUNT",       "15500"),
    "emi_amount_words": os.getenv("DEFAULT_EMI_AMOUNT_WORDS", "पंद्रह हज़ार पाँच सौ"),
}


# ─────────────────────────────────────────────────────────────────────────────
# FSM — scripted responses, state tables, transitions
# ─────────────────────────────────────────────────────────────────────────────

# All bot lines. Use {key} placeholders — filled from per-call customer context.
_SCRIPTS: dict[str, str] = {
    "opening": (
        "नमस्ते, मैं {lender} से अदिति बोल रही हूँ। "
        "क्या मैं {customer_name} से बात कर रही हूँ? "
        "आपकी {loan_type} की {emi_amount_words} रुपये की EMI "
        "इस महीने {emi_due_date} तारीख से बकाया है। "
        "क्या आप आज payment कर पाएंगे?"
    ),
    "ptp": (
        "बहुत अच्छा! SMS में आया payment link या नज़दीकी branch से EMI जमा कर दें। "
        "समय पर payment से penalty और CIBIL score दोनों सुरक्षित रहेंगे। "
        "धन्यवाद, आपका दिन शुभ हो।"
    ),
    "cannot_pay": (
        "मैं आपकी बात समझ सकती हूँ। "
        "जैसे ही संभव हो EMI जमा कर दीजिए, ताकि penalty और CIBIL पर असर न पड़े। "
        "मदद चाहिए तो customer care से बात कर सकते हैं। धन्यवाद।"
    ),
    "will_not_pay": (
        "ठीक है, आपकी बात नोट कर ली गई है। "
        "कृपया ध्यान रखें, बकाया EMI रहने पर penalty और CIBIL score पर असर पड़ सकता है। "
        "धन्यवाद।"
    ),
    "no_loan": (
        "ठीक है, हम रिकॉर्ड जाँचेंगे। "
        "किसी भी सहायता के लिए {lender} customer care से संपर्क कर सकते हैं। "
        "धन्यवाद, आपका दिन शुभ हो।"
    ),
    "wrong_emi": (
        "राशि संबंधी आपकी आपत्ति दर्ज कर ली गई है। "
        "सही जानकारी के लिए अपने relationship manager या customer care से संपर्क करें। "
        "धन्यवाद।"
    ),
    "faq_who_are_you": (
        "मैं अदिति बोल रही हूँ, {lender} की तरफ़ से। "
        "यह call आपकी बकाया {loan_type} EMI के बारे में है। "
        "बताइए, क्या आप आज payment कर पाएंगे?"
    ),
    "already_paid_ask_date": (
        "अच्छा! Record update करने के लिए — "
        "आपने किस तारीख को payment किया था?"
    ),
    "already_paid_ask_mode": (
        "और payment किस तरीके से किया था — "
        "UPI, net banking, cash, branch, या कोई और?"
    ),
    "already_paid_thanks": (
        "धन्यवाद, भुगतान विवरण नोट कर लिया गया है। "
        "हम records verify करके update कर देंगे। "
        "आपका दिन शुभ हो।"
    ),
    "unclear_sm1": (
        "माफ़ कीजिए, आवाज़ साफ़ नहीं आई। "
        "क्या आप आज EMI payment कर पाएंगे — हाँ या नहीं?"
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

# States that hang up after speaking
_TERMINAL: set[str] = {
    "ptp", "cannot_pay", "will_not_pay", "no_loan",
    "wrong_emi", "already_paid_thanks", "unclear_close",
}

# States that need user input classified before advancing
_CLASSIFY_STATES: set[str] = {"opening", "already_paid_ask_date", "already_paid_ask_mode"}

# classification result → next FSM state
_TRANSITIONS: dict[str, str] = {
    "goto_ptp":               "ptp",
    "goto_cannot_pay":        "cannot_pay",
    "goto_will_not_pay":      "will_not_pay",
    "goto_already_paid":      "already_paid_ask_date",
    "goto_no_loan":           "no_loan",
    "goto_wrong_emi":         "wrong_emi",
    "answer_faq_who_are_you": "faq_who_are_you",
    "goto_already_paid_mode": "already_paid_ask_mode",
    "goto_already_paid_thanks": "already_paid_thanks",
}

# Non-classify states that loop back to a parent state after speaking
_AUTO_ADVANCE: dict[str, str] = {
    "faq_who_are_you": "opening",
    "unclear_sm1":     "opening",
    "unclear_sm2":     "opening",
}


# ─────────────────────────────────────────────────────────────────────────────
# Classification — rule-based primary, Sarvam-M LLM fallback
# ─────────────────────────────────────────────────────────────────────────────

def _heuristic(utterance: str, state: str) -> str | None:
    """
    Rule-based classifier. Returns a transition name, 'mark_unclear', or None.
    None means the utterance is genuinely ambiguous — caller should use LLM.
    """
    t = utterance.lower().strip()
    if not t or t == "[silence]":
        return "mark_unclear"

    if state == "opening":
        # ── FAQ / identity ───────────────────────────────────────────────────
        if any(k in t for k in ["कौन", "किसका", "कहाँ से", "किसलिए", "why call",
                                  "who are", "easy home", "किस company"]):
            return "answer_faq_who_are_you"

        # ── Already paid ─────────────────────────────────────────────────────
        if any(k in t for k in ["कर दिया", "दे दिया", "भेज दिया", "भेज दिए",
                                  "payment हो गई", "payment हो गया", "पेमेंट हो गई",
                                  "पेमेंट हो गया", "payment कर दिया", "पेमेंट कर दिया",
                                  "already paid", "already", "पहले ही", "हो चुका",
                                  "कर चुका", "कर चुकी", "भर दिया", "भर चुका"]):
            return "goto_already_paid"

        # ── Wrong number / wrong person ──────────────────────────────────────
        if any(k in t for k in ["गलत नंबर", "wrong number", "मेरा loan नहीं",
                                  "loan नहीं है", "कोई loan नहीं", "मैं शर्मा नहीं",
                                  "wrong person", "not me", "गलत है नंबर",
                                  "मेरे नाम पर नहीं", "मेरी नहीं है"]):
            return "goto_no_loan"

        # ── Amount dispute ───────────────────────────────────────────────────
        if any(k in t for k in ["amount गलत", "emi गलत", "राशि गलत", "गलत राशि",
                                  "इतनी emi नहीं", "emi इतनी नहीं", "इतना नहीं बनता",
                                  "गलत amount", "amount wrong"]):
            return "goto_wrong_emi"

        # ── Hard refusal — explicitly will not pay ───────────────────────────
        if any(k in t for k in ["नहीं भरूंगा", "नहीं भरूँगा", "नहीं दूंगा", "नहीं दूँगा",
                                  "मत कॉल", "मत फोन", "मत call", "मत phone",
                                  "पेमेंट नहीं करनी", "payment नहीं करनी",
                                  "payment नहीं करूंगा", "नहीं करूंगा payment",
                                  "नहीं भरेंगे", "नहीं करेंगे",
                                  "कोई मतलब नहीं", "मुझे मतलब नहीं",
                                  "नहीं करना", "नहीं करनी"]):
            return "goto_will_not_pay"

        # ── Cannot / unable to pay — needs time or money ─────────────────────
        if any(k in t for k in ["नहीं कर पाऊंगा", "नहीं कर पाऊँगा", "नहीं हो पाएगा",
                                  "नहीं हो पाएगी", "कर पाऊंगा नहीं", "नहीं कर पाता",
                                  "नहीं कर पाती", "अभी नहीं", "अभी नहि",
                                  "नहीं मेरे", "मेरे को नहीं", "बाद में",
                                  "बाद में करूंगा", "थोड़ा समय", "कुछ दिन",
                                  "पैसे नहीं", "पैसे नहि", "पैसों की",
                                  "मुश्किल है", "तकलीफ", "problem है",
                                  "extend", "समय दे", "time दे",
                                  "10 दिन", "15 दिन", "महीने बाद",
                                  "next month", "अगले हफ़्ते", "अगले हफ्ते"]):
            return "goto_cannot_pay"

        # ── Positive commitment ──────────────────────────────────────────────
        if any(k in t for k in ["हाँ", "हां", "जी हाँ", "जी हां", "ठीक है",
                                  "theek hai", "कर दूंगा", "करूंगा", "करूँगा",
                                  "भर दूंगा", "भर दूँगा", "हो जाएगा",
                                  "कर देता", "कर देती", "pay कर", "okay", " ok ",
                                  "sure", "bilkul", "बिल्कुल"]):
            return "goto_ptp"

        # ── Bare नहीं with no other context → cannot pay (benefit of doubt) ─
        if "नहीं" in t or "नहि" in t or "nahi" in t:
            return "goto_cannot_pay"

        return None  # genuinely ambiguous → LLM fallback

    elif state == "already_paid_ask_date":
        # Anything substantive counts — we just want to capture the date
        if any(k in t for k in ["याद नहीं", "याद नहि", "remember नहीं", "don't remember",
                                  "पता नहीं", "exact याद नहीं"]):
            return "goto_already_paid_mode"  # "don't remember" is still an answer
        if re.search(r"\d", t):
            return "goto_already_paid_mode"  # any number = likely a date
        if any(k in t for k in ["कल", "परसों", "हफ़्ते", "हफ्ते", "week",
                                  "महीने", "तारीख", "तारिख", "date", "को"]):
            return "goto_already_paid_mode"
        words = [w for w in t.split() if w.strip(".,?!।")]
        if len(words) >= 1:
            return "goto_already_paid_mode"
        return "mark_unclear"

    elif state == "already_paid_ask_mode":
        # Any payment method mention or substantive response → proceed
        if any(k in t for k in ["upi", "google pay", "gpay", "phonepe", "paytm",
                                  "bhim", "cash", "नकद", "bank", "net banking",
                                  "neft", "imps", "rtgs", "branch", "atm",
                                  "cheque", "card", "transfer", "online",
                                  "mobile", "wallet"]):
            return "goto_already_paid_thanks"
        words = [w for w in t.split() if w.strip(".,?!।")]
        if len(words) >= 1:
            return "goto_already_paid_thanks"
        return "mark_unclear"

    return None


# Sarvam-M is a thinking model: it emits <think>...</think> before the answer.
# The LLM is ONLY called when the heuristic cannot determine intent.
_LLM_CLASSIFY_PROMPTS: dict[str, str] = {
    "opening": """\
You are classifying a customer's response in a Hindi EMI collection call.

The agent asked: "क्या आप आज payment कर पाएंगे?" (Can you make the payment today?)
The customer said: "{utterance}"

Choose EXACTLY ONE label — output only the label, nothing else:

GOTO_PTP            — customer agrees to pay (today, tomorrow, or soon)
GOTO_CANNOT_PAY     — customer says they are unable to pay / needs time / financial difficulty
GOTO_WILL_NOT_PAY   — customer flat-out refuses with no reason
GOTO_ALREADY_PAID   — customer says payment is already done
GOTO_NO_LOAN        — wrong person, wrong number, or denies having this loan
GOTO_WRONG_EMI      — customer disputes the EMI amount
ANSWER_FAQ_WHO_ARE_YOU — customer asks who you are or why you're calling
MARK_UNCLEAR        — completely unintelligible, pure noise, or silence

When in doubt between GOTO_CANNOT_PAY and GOTO_WILL_NOT_PAY, choose GOTO_CANNOT_PAY.""",

    "already_paid_ask_date": """\
The agent asked: "किस तारीख को payment किया था?" (What date was the payment made?)
The customer said: "{utterance}"

Choose EXACTLY ONE label:
GOTO_ALREADY_PAID_MODE — customer gave any date, approximate time, or said they don't remember
MARK_UNCLEAR           — completely unintelligible or no response at all""",

    "already_paid_ask_mode": """\
The agent asked: "payment किस तरीके से किया था?" (How was the payment made?)
The customer said: "{utterance}"

Choose EXACTLY ONE label:
GOTO_ALREADY_PAID_THANKS — customer mentioned any payment method (even vaguely)
MARK_UNCLEAR             — completely unintelligible or no response at all""",
}

_LLM_KEYWORD_MAP: dict[str, str] = {
    "GOTO_PTP":               "goto_ptp",
    "GOTO_CANNOT_PAY":        "goto_cannot_pay",
    "GOTO_WILL_NOT_PAY":      "goto_will_not_pay",
    "GOTO_ALREADY_PAID":      "goto_already_paid",
    "GOTO_NO_LOAN":           "goto_no_loan",
    "GOTO_WRONG_EMI":         "goto_wrong_emi",
    "ANSWER_FAQ_WHO_ARE_YOU": "answer_faq_who_are_you",
    "GOTO_ALREADY_PAID_MODE": "goto_already_paid_mode",
    "GOTO_ALREADY_PAID_THANKS": "goto_already_paid_thanks",
    "MARK_UNCLEAR":           "mark_unclear",
}


async def classify(utterance: str, state: str) -> str:
    """
    Classify a customer utterance for the given FSM state.
    Rule-based first; LLM (Sarvam-M) fallback for genuinely ambiguous input.
    """
    # 1. Rule-based — fast, deterministic, no network call
    result = _heuristic(utterance, state)
    if result is not None:
        log.info("CLASSIFY[rule] %s → %s | %r", state, result, utterance[:60])
        return result

    # 2. Sarvam-M fallback — handles edge cases the rules don't cover
    prompt_template = _LLM_CLASSIFY_PROMPTS.get(state)
    if not prompt_template:
        return "mark_unclear"

    prompt = prompt_template.format(utterance=utterance)
    try:
        resp = await _oai.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=512,  # thinking model needs room to reason before emitting the keyword
        )
        raw_full = (resp.choices[0].message.content or "").strip()
        # Strip <think>...</think> chain-of-thought blocks before keyword matching
        raw = re.sub(r"<think>.*?</think>", "", raw_full, flags=re.DOTALL | re.IGNORECASE).strip().upper()
        log.info("CLASSIFY[llm] raw=%r", raw[:120])
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
    """Stream TTS audio chunks to caller. Returns True on success."""
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

                # Detect container type from the first bytes
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
                        b64 = (data.get("audios") or [None])[0] or data.get("audio")
                        if b64:
                            got_audio = True
                            await on_chunk(base64.b64decode(b64))
                    except Exception as exc:
                        log.warning("TTS JSON decode failed: %s", exc)
                    return got_audio

                else:  # WAV — strip RIFF header
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

        if not got_audio:
            log.warning("TTS stream: response ended with no audio")
        return got_audio

    except Exception as exc:
        log.error("TTS stream error (%s): %s", type(exc).__name__, exc)
        return False


async def tts_rest(text: str) -> bytes:
    """Fallback blocking TTS — full audio before first byte out."""
    r = await _http.post(SARVAM_TTS_REST_URL, json=_tts_payload(text), headers=_tts_headers())
    if r.status_code >= 400:
        raise RuntimeError(f"TTS REST {r.status_code}: {r.text[:200]}")
    data = r.json()
    b64 = (data.get("audios") or [None])[0] or data.get("audio")
    if not b64:
        raise RuntimeError("TTS REST: no audio in response")
    return base64.b64decode(b64)


# ─────────────────────────────────────────────────────────────────────────────
# Call session
# ─────────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class CallSession:
    ctx:             dict[str, str]
    stream_sid:      str = ""
    call_sid:        str = ""
    state:           str = "opening"
    done:            bool = False
    speaking:        bool = False
    unclear_count:   int  = 0
    marks_out:       int  = 0
    last_queued:     str  = ""
    last_interim:    str  = ""
    transcript_path: str  = ""


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Aditi — Hindi EMI Collection Voice Bot")

# call_sid → per-call customer context (set by /make-call, consumed at call start)
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
    to = (body.get("to") or "").strip()
    if not to:
        raise HTTPException(status_code=422, detail="`to` (phone number) is required")
    ctx = {**DEFAULT_CUSTOMER, **{k: str(v) for k, v in body.items() if k != "to"}}
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
    abort = [False]                         # mutable barge-in flag shared across tasks
    utt_q: asyncio.Queue[str] = asyncio.Queue()
    drained = asyncio.Event()
    drained.set()

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
                "event": event,
                "state": sess.state,
                "sid":   sess.call_sid,
                **fields,
            }
            try:
                with open(sess.transcript_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError:
                pass

        # ── Push 20 ms µ-law frames to Twilio ───────────────────────────────
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
                        "event":     "media",
                        "streamSid": sess.stream_sid,
                        "media":     {"payload": base64.b64encode(frame).decode()},
                    })
                    await asyncio.sleep(0)
            except Exception:
                pass

        # ── Mark event — lets hangup() know audio has played ────────────────
        async def send_mark() -> None:
            if sess.done or not sess.stream_sid:
                return
            drained.clear()
            sess.marks_out += 1
            try:
                await twilio_ws.send_json({
                    "event":     "mark",
                    "streamSid": sess.stream_sid,
                    "mark":      {"name": "tts_done"},
                })
            except Exception:
                pass

        # ── Play TTS: streaming with REST fallback ───────────────────────────
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
                    log.warning("TTS stream failed — falling back to REST")
                    audio = await tts_rest(text)
                    await push(_strip_wav_header(audio))
                if not sess.done:
                    await send_mark()
            except Exception as exc:
                log.error("play_tts error: %s", exc)
            finally:
                sess.speaking = False
            log.info("TTS %.0f ms | %.60s", (time.perf_counter() - t0) * 1000, text)

        # ── SafeMap for template substitution ───────────────────────────────
        class _SafeMap(dict):
            def __missing__(self, key: str) -> str:
                return f"{{{key}}}"

        # ── Speak a scripted line (template-fills customer vars) ─────────────
        async def speak(text: str) -> None:
            text = text.format_map(_SafeMap(sess.ctx))
            log.info("[ADITI] %s", text)
            record("bot", text=text)
            await play_tts(text)

        async def speak_state(state: str) -> None:
            script = _SCRIPTS.get(state, "")
            if script:
                await speak(script)

        # ── Graceful hangup ──────────────────────────────────────────────────
        async def hangup(reason: str = "unknown") -> None:
            if sess.done:
                return
            sess.done = True
            abort[0]  = True
            log.info("Hangup: %s", reason)
            record("hangup", reason=reason)

            # Wait for Twilio to drain queued audio
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

        # ── Twilio media stream receiver ─────────────────────────────────────
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
                        # Restore per-call customer context
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
                log.info("Twilio WebSocket disconnected")
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

                    # ── VAD events ───────────────────────────────────────────
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

                    # ── Final transcript ─────────────────────────────────────
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
                        # Barge-in: abort TTS if bot is currently speaking
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
                """Increment unclear counter, speak prompt, return True if call should end."""
                sess.unclear_count += 1
                target = (
                    "unclear_sm1"  if sess.unclear_count == 1 else
                    "unclear_sm2"  if sess.unclear_count == 2 else
                    "unclear_close"
                )
                old_state  = sess.state
                sess.state = target
                log.info("FSM unclear %d: %s → %s", sess.unclear_count, old_state, target)
                record("unclear", target=target)
                await speak_state(target)
                if target == "unclear_close":
                    asyncio.create_task(hangup("no_response"))
                    return True
                # Auto-advance back to the classifying state
                sess.state = _AUTO_ADVANCE.get(target, "opening")
                return False

            while not sess.done:
                # Wait for the next utterance (or silence timeout)
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

                utterance = utterance.strip() or "[silence]"
                record("user_turn", text=utterance)

                if sess.state not in _CLASSIFY_STATES:
                    # Should not happen in normal flow — log and skip
                    log.warning("FSM: utterance received in non-classify state %s — ignoring", sess.state)
                    continue

                # ── Classify and transition ───────────────────────────────────
                t0     = time.perf_counter()
                action = await classify(utterance, sess.state)
                elapsed = (time.perf_counter() - t0) * 1000
                log.info("CLASSIFY %s → %s (%.0f ms)", sess.state, action, elapsed)
                record("classify", action=action, ms=round(elapsed))

                if action == "mark_unclear":
                    if await handle_unclear():
                        break
                    continue

                next_state = _TRANSITIONS.get(action)
                if not next_state:
                    log.warning("FSM: unknown action %r → treating as unclear", action)
                    if await handle_unclear():
                        break
                    continue

                log.info("FSM %s → %s", sess.state, next_state)
                sess.state = next_state
                sess.unclear_count = 0      # reset on successful exchange
                await speak_state(next_state)

                if sess.done:
                    break

                if next_state in _TERMINAL:
                    asyncio.create_task(hangup(next_state))
                    break

                if next_state in _AUTO_ADVANCE:
                    sess.state = _AUTO_ADVANCE[next_state]

            log.info("FSM ended (state=%s)", sess.state)

        # ── Run all three coroutines concurrently ────────────────────────────
        await asyncio.gather(recv_twilio(), recv_sarvam_stt(), fsm())

    finally:
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
