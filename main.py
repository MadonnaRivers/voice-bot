"""
Aditi — Hindi EMI Collection Voice Bot
───────────────────────────────────────
STT : Sarvam Saaras v3  (WebSocket, PCM-16 LE @ 8 kHz)
LLM : Sarvam-M          (OpenAI-compatible streaming API)
TTS : Sarvam Bulbul v3  (HTTP-streaming, µ-law @ 8 kHz → Twilio)

Latency budget (target ≈ 1.2–1.8 s end-to-end):
  Sarvam STT endpointing / VAD         ~200–400 ms
  LLM time-to-first-token              ~600–800 ms
  Sarvam TTS HTTP-stream first chunk   ~150–250 ms
  ──────────────────────────────────────────────────
  Total                                ~950–1450 ms

Call flow:
  Twilio PSTN → Media Stream WebSocket (/media-stream)
    ├─ recv_twilio()     mulaw in → PCM-16 → Sarvam STT WebSocket
    ├─ recv_sarvam_stt() transcripts → utterance queue
    └─ fsm()             reads utterances, runs LLM, streams TTS back to Twilio
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
# Config — fail fast on missing required secrets
# ─────────────────────────────────────────────────────────────────────────────
def _require(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


SARVAM_API_KEY      = _require("SARVAM_API_KEY")
TWILIO_ACCOUNT_SID  = _require("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = _require("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = _require("TWILIO_PHONE_NUMBER")
NGROK_URL           = _require("NGROK_URL").rstrip("/")

SARVAM_LLM_BASE_URL = os.getenv("SARVAM_LLM_BASE_URL", "https://api.sarvam.ai/v1")
SARVAM_STT_WS_BASE  = os.getenv("SARVAM_STT_WS_BASE",  "wss://api.sarvam.ai/speech-to-text/ws")
SARVAM_STT_MODEL    = os.getenv("SARVAM_STT_MODEL",    "saaras:v3")
SARVAM_STT_LANGUAGE = os.getenv("SARVAM_STT_LANGUAGE", "hi-IN")
SARVAM_TTS_LANGUAGE = "hi-IN"
LLM_MODEL           = os.getenv("SARVAM_LLM_MODEL",    "sarvam-m")
SARVAM_VOICE        = os.getenv("SARVAM_VOICE",        "simran")
PORT                = int(os.getenv("PORT", "5050"))
DEFAULT_CALL_TO     = os.getenv("CALL_TO", "")
TRANSCRIPTS_DIR     = os.getenv("TRANSCRIPTS_DIR", "transcripts")
MAKE_CALL_API_KEY   = os.getenv("MAKE_CALL_API_KEY", "")

# Runtime tunables
HANGUP_GRACE_SEC    = float(os.getenv("HANGUP_GRACE_SEC",    "1.8"))
SILENCE_TIMEOUT_SEC = float(os.getenv("SILENCE_TIMEOUT_SEC", "25.0"))
MIN_WORDS           = int(os.getenv("MIN_WORDS",             "2"))
LLM_MAX_TOKENS      = int(os.getenv("LLM_MAX_TOKENS",        "90"))
LLM_TEMPERATURE     = float(os.getenv("LLM_TEMPERATURE",     "0.3"))
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
# Shared HTTP client — keeps TCP/TLS alive across utterances (~100 ms saved)
# ─────────────────────────────────────────────────────────────────────────────
_http = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=4.0, read=15.0, write=5.0, pool=5.0),
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)

# ─────────────────────────────────────────────────────────────────────────────
# Prompt helpers
# ─────────────────────────────────────────────────────────────────────────────
_PROMPT_BASE = Path(__file__).parent / "prompts"


class _SafeMap(dict):
    """dict subclass that returns {key} unchanged for missing keys."""
    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


def load_prompt(rel: str, ctx: dict[str, str] | None = None) -> str:
    text = (_PROMPT_BASE / f"{rel}.txt").read_text(encoding="utf-8").strip()
    return text.format_map(_SafeMap(ctx)) if ctx else text


def scripted_line(state: str, ctx: dict[str, str] | None = None) -> str:
    """Return the first double-quoted string from a state prompt file."""
    try:
        txt = load_prompt(STATES[state]["prompt_file"], ctx)
    except Exception:
        return ""
    m = re.search(r'"([^"]+)"', txt, re.DOTALL)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""

# ─────────────────────────────────────────────────────────────────────────────
# FSM — states, tools, transitions
# ─────────────────────────────────────────────────────────────────────────────
STATES: dict[str, dict[str, Any]] = {
    "opening": {
        "prompt_file": "states/opening",
        "tools": [
            "goto_ptp", "goto_cannot_pay", "goto_will_not_pay",
            "goto_no_loan", "goto_wrong_emi", "goto_already_paid",
            "answer_faq_who_are_you", "mark_unclear",
        ],
        "terminal": False,
    },
    "ptp":           {"prompt_file": "states/ptp",           "tools": ["end_call"], "terminal": True},
    "cannot_pay":    {"prompt_file": "states/cannot_pay",    "tools": ["end_call"], "terminal": True},
    "will_not_pay":  {"prompt_file": "states/will_not_pay",  "tools": ["end_call"], "terminal": True},
    "no_loan":       {"prompt_file": "states/no_loan",       "tools": ["end_call"], "terminal": True},
    "wrong_emi":     {"prompt_file": "states/wrong_emi",     "tools": ["end_call"], "terminal": True},
    "unclear_close": {"prompt_file": "states/unclear_close", "tools": ["end_call"], "terminal": True},
    "already_paid_ask_date": {
        "prompt_file": "states/already_paid_ask_date",
        "tools": ["goto_already_paid_mode", "mark_unclear"],
        "terminal": False,
    },
    "already_paid_ask_mode": {
        "prompt_file": "states/already_paid_ask_mode",
        "tools": ["goto_already_paid_thanks", "mark_unclear"],
        "terminal": False,
    },
    "already_paid_thanks": {
        "prompt_file": "states/already_paid_thanks",
        "tools": ["end_call"],
        "terminal": True,
    },
    "faq_who_are_you": {
        "prompt_file": "states/faq_who_are_you",
        "tools": [],
        "terminal": False,
        "auto_advance_to": "opening",
    },
    "unclear_sm1": {
        "prompt_file": "states/unclear_sm1",
        "tools": [],
        "terminal": False,
        "auto_advance_to": "opening",
    },
    "unclear_sm2": {
        "prompt_file": "states/unclear_sm2",
        "tools": [],
        "terminal": False,
        "auto_advance_to": "opening",
    },
}

TRANSITION_MAP: dict[str, str] = {
    "goto_ptp":                 "ptp",
    "goto_cannot_pay":          "cannot_pay",
    "goto_will_not_pay":        "will_not_pay",
    "goto_no_loan":             "no_loan",
    "goto_wrong_emi":           "wrong_emi",
    "goto_already_paid":        "already_paid_ask_date",
    "goto_already_paid_mode":   "already_paid_ask_mode",
    "goto_already_paid_thanks": "already_paid_thanks",
    "answer_faq_who_are_you":   "faq_who_are_you",
}


# ── Keyword-based classification (replaces tool calling — sarvam-m has no tool support) ──
# Each classification state's prompt describes WHEN to use which keyword.
# The LLM must output exactly one keyword on a single line, nothing else.

# Maps the uppercase keyword the LLM outputs → internal transition name
_KEYWORD_MAP: dict[str, str] = {
    "GOTO_PTP":                 "goto_ptp",
    "GOTO_CANNOT_PAY":          "goto_cannot_pay",
    "GOTO_WILL_NOT_PAY":        "goto_will_not_pay",
    "GOTO_NO_LOAN":             "goto_no_loan",
    "GOTO_WRONG_EMI":           "goto_wrong_emi",
    "GOTO_ALREADY_PAID":        "goto_already_paid",
    "GOTO_ALREADY_PAID_MODE":   "goto_already_paid_mode",
    "GOTO_ALREADY_PAID_THANKS": "goto_already_paid_thanks",
    "ANSWER_FAQ_WHO_ARE_YOU":   "answer_faq_who_are_you",
    "MARK_UNCLEAR":             "mark_unclear",
    "END_CALL":                 "end_call",
}

# Appended to every classification state's system prompt
_CLASSIFY_FOOTER = """
━━━ OUTPUT FORMAT ━━━
Reply with EXACTLY one keyword — no explanation, no punctuation, nothing else:

GOTO_PTP | GOTO_CANNOT_PAY | GOTO_WILL_NOT_PAY | GOTO_ALREADY_PAID | GOTO_NO_LOAN | GOTO_WRONG_EMI | GOTO_ALREADY_PAID_MODE | GOTO_ALREADY_PAID_THANKS | ANSWER_FAQ_WHO_ARE_YOU | MARK_UNCLEAR | END_CALL
"""


def sys_prompt(state: str, ctx: dict[str, str] | None = None) -> str:
    persona      = load_prompt("persona",                    ctx)
    state_prompt = load_prompt(STATES[state]["prompt_file"], ctx)
    base = f"{persona}\n\n---\n\n{state_prompt}"
    # Append keyword footer for classification states (those that previously used tools)
    if STATES[state].get("tools"):
        return base + "\n" + _CLASSIFY_FOOTER
    return base

# ─────────────────────────────────────────────────────────────────────────────
# Default customer data — overridden per-call via POST /make-call body
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_CUSTOMER: dict[str, str] = {
    "customer_name":    os.getenv("DEFAULT_CUSTOMER_NAME",        "Mr. Sharma"),
    "lender":           os.getenv("DEFAULT_LENDER",               "Easy Home Finance"),
    "loan_type":        os.getenv("DEFAULT_LOAN_TYPE",            "Home Loan"),
    "account_last4":    os.getenv("DEFAULT_ACCOUNT_LAST4",        "4321"),
    "emi_due_date":     os.getenv("DEFAULT_EMI_DUE_DATE",         "5"),
    "emi_amount":       os.getenv("DEFAULT_EMI_AMOUNT",           "15500"),
    "emi_amount_words": os.getenv("DEFAULT_EMI_AMOUNT_WORDS",     "पंद्रह हज़ार पाँच सौ"),
}

# ─────────────────────────────────────────────────────────────────────────────
# TTS — Sarvam Bulbul v3
# ─────────────────────────────────────────────────────────────────────────────
def _tts_payload(text: str) -> dict:
    return {
        "text":                 text,
        "target_language_code": SARVAM_TTS_LANGUAGE,
        "speaker":              SARVAM_VOICE,
        "model":                "bulbul:v3",
        "speech_sample_rate":   8000,
        "output_audio_codec":   "mulaw",
        "enable_preprocessing": True,
        "pace":                 TTS_PACE,
    }


def _tts_headers() -> dict[str, str]:
    return {
        "api-subscription-key": SARVAM_API_KEY,
        "Content-Type":         "application/json",
    }


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
    """
    POST to Sarvam TTS /stream endpoint; forward audio chunks as they arrive.
    First chunk lands in ~150 ms. Returns True on success, False on error.
    Handles three possible response containers: raw µ-law, WAV, or JSON+base64.
    """
    try:
        async with _http.stream(
            "POST",
            SARVAM_TTS_STREAM_URL,
            json=_tts_payload(text),
            headers=_tts_headers(),
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                log.error("TTS stream HTTP %d: %s", resp.status_code, body[:300])
                return False

            container: str | None = None   # "raw" | "wav" | "json"
            wav_buf             = b""
            wav_payload_started = False
            got_audio           = False

            async for chunk in resp.aiter_bytes(chunk_size=8192):
                if abort[0]:
                    return True
                if not chunk:
                    continue

                # Detect container type once from the first bytes
                if container is None:
                    probe = chunk[:12]
                    if probe.startswith(b"RIFF") and b"WAVE" in probe:
                        container = "wav"
                    elif probe[:1] in (b"{", b"["):
                        container = "json"
                    else:
                        container = "raw"

                # ── Raw µ-law ────────────────────────────────────────────────
                if container == "raw":
                    got_audio = True
                    await on_chunk(chunk)
                    continue

                # ── JSON with base64 audio ───────────────────────────────────
                if container == "json":
                    try:
                        tail = await resp.aread()
                        data = json.loads((chunk + tail).decode("utf-8", errors="ignore"))
                        b64  = (data.get("audios") or [None])[0] or data.get("audio")
                        if b64:
                            got_audio = True
                            await on_chunk(base64.b64decode(b64))
                    except Exception as exc:
                        log.warning("TTS stream JSON decode failed: %s", exc)
                    return got_audio

                # ── WAV container — strip RIFF header, forward raw payload ───
                if not wav_payload_started:
                    wav_buf += chunk
                    idx = wav_buf.find(b"data")
                    if idx == -1:
                        continue  # accumulate until we have the full header
                    payload_start = idx + 8  # skip "data" + 4-byte length field
                    if len(wav_buf) <= payload_start:
                        continue
                    audio = wav_buf[payload_start:]
                    wav_buf = b""
                    wav_payload_started = True
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
    """Fallback: blocking REST call — full audio received before first byte out."""
    r = await _http.post(
        SARVAM_TTS_REST_URL,
        json=_tts_payload(text),
        headers=_tts_headers(),
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Sarvam TTS REST {r.status_code}: {r.text[:300]}")
    data = r.json()
    b64  = (data.get("audios") or [None])[0] or data.get("audio")
    if not b64:
        raise RuntimeError(f"Sarvam TTS REST: no audio in response — {str(data)[:200]}")
    return base64.b64decode(b64)

# ─────────────────────────────────────────────────────────────────────────────
# LLM — Sarvam-M via OpenAI-compatible streaming API
# ─────────────────────────────────────────────────────────────────────────────
_oai = AsyncOpenAI(api_key=SARVAM_API_KEY, base_url=SARVAM_LLM_BASE_URL)


async def classify(
    history: list[dict],
    state: str,
    ctx: dict[str, str] | None = None,
) -> str:
    """
    Ask the LLM to output a single classification keyword.
    Returns the internal transition name (e.g. 'goto_ptp', 'mark_unclear').
    Non-streaming — called only for states that previously used tool calls.
    """
    try:
        resp = await _oai.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "system", "content": sys_prompt(state, ctx)}] + history,
            temperature=0,
            max_tokens=10,
        )
        raw = (resp.choices[0].message.content or "").strip().upper()
        # Match longest keyword first to avoid partial hits
        for kw in sorted(_KEYWORD_MAP, key=len, reverse=True):
            if kw in raw:
                return _KEYWORD_MAP[kw]
    except Exception as exc:
        log.error("LLM classify error: %s", exc)
    return "mark_unclear"


async def llm_stream(
    history: list[dict],
    state: str,
    ctx: dict[str, str] | None = None,
):
    """
    Streaming text generator for states that produce spoken responses.
    Yields str tokens. Never called for classification states.
    """
    try:
        async for chunk in await _oai.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "system", "content": sys_prompt(state, ctx)}] + history,
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS,
            stream=True,
        ):
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content
    except Exception as exc:
        log.error("LLM stream error: %s", exc)

# ─────────────────────────────────────────────────────────────────────────────
# Call session — all mutable per-call state in one typed container
# ─────────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class CallSession:
    ctx: dict[str, str]                  # per-call customer template vars
    stream_sid:      str  = ""
    call_sid:        str  = ""
    state:           str  = "opening"
    history:         list = dataclasses.field(default_factory=list)
    done:            bool = False
    speaking:        bool = False
    last_queued:     str  = ""           # dedup guard; reset after each turn
    last_interim:    str  = ""
    unclear_count:   int  = 0
    marks_out:       int  = 0            # outstanding Twilio mark events
    transcript_path: str  = ""

# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Aditi — Hindi EMI Collection Voice Bot")

# Maps call_sid → per-call customer context; populated by /make-call
_pending_ctx: dict[str, dict[str, str]] = {}


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse("<h3>Aditi — Sarvam STT · Sarvam LLM · Sarvam TTS</h3>")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "llm": LLM_MODEL, "voice": SARVAM_VOICE})


@app.post("/make-call")
async def make_call(
    request: Request,
    x_api_key: str = Header(default=""),
) -> JSONResponse:
    if MAKE_CALL_API_KEY and x_api_key != MAKE_CALL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Api-Key header")

    body = await request.json()
    to   = (body.get("to") or "").strip()
    if not to:
        raise HTTPException(status_code=422, detail="`to` (phone number) is required")

    # Merge default customer data with any fields supplied in the request body
    ctx = {**DEFAULT_CUSTOMER, **{k: str(v) for k, v in body.items() if k != "to"}}

    call = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).calls.create(
        url=f"{NGROK_URL}/outgoing-call",
        to=to,
        from_=TWILIO_PHONE_NUMBER,
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
# Main WebSocket handler
# ─────────────────────────────────────────────────────────────────────────────
@app.websocket("/media-stream")
async def media_stream(twilio_ws: WebSocket) -> None:
    await twilio_ws.accept()
    log.info("Twilio media stream connected")

    sess    = CallSession(ctx=dict(DEFAULT_CUSTOMER))
    abort   = [False]                       # mutable barge-in flag
    utt_q: asyncio.Queue[str] = asyncio.Queue()
    drained = asyncio.Event()
    drained.set()

    stt_headers = {"Api-Subscription-Key": SARVAM_API_KEY}
    try:
        stt_ws = await websockets.connect(
            SARVAM_STT_WS_URL,
            extra_headers=stt_headers,
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
                    frame = chunk[offset : offset + 160]
                    if not frame:
                        return
                    await twilio_ws.send_json({
                        "event":     "media",
                        "streamSid": sess.stream_sid,
                        "media":     {"payload": base64.b64encode(frame).decode()},
                    })
                    # Yield without sleeping — Sarvam streaming already paces delivery.
                    # Sleeping per frame (even 10 ms) adds 1–2 s of total latency.
                    await asyncio.sleep(0)
            except Exception:
                pass

        # ── Twilio mark — lets hangup() know when audio has fully played ─────
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

        # ── Primary TTS: streaming with REST fallback ────────────────────────
        async def play_tts(text: str) -> None:
            """Stream audio to Twilio, send a mark when done."""
            text = text.strip()
            if not text or sess.done or not sess.stream_sid:
                return
            t0       = time.perf_counter()
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

        # ── Speak a text line (records to transcript) ────────────────────────
        async def speak(text: str) -> None:
            log.info("[ADITI] %s", text)
            record("bot", text=text)
            await play_tts(text)

        # ── Speak the scripted (or LLM-generated) line for a given state ─────
        async def speak_state(state: str) -> None:
            text = scripted_line(state, sess.ctx)
            if not text:
                # States without a quoted scripted line — ask the LLM once
                resp = await _oai.chat.completions.create(
                    model=LLM_MODEL,
                    messages=[
                        {"role": "system", "content": sys_prompt(state, sess.ctx)},
                        {"role": "user",   "content": "बोलें।"},
                    ],
                    temperature=LLM_TEMPERATURE,
                    max_tokens=LLM_MAX_TOKENS,
                )
                text = (resp.choices[0].message.content or "").strip()
            if text:
                sess.history.append({"role": "assistant", "content": text})
                await speak(text)

        # ── LLM streaming + sentence-level TTS pipeline ──────────────────────
        async def run_turn(utterance: str) -> str:
            """
            Stream LLM tokens to TTS sentence-by-sentence.
            Used only for text-generating states (faq, unclear_sm1/2).
            Returns the full text spoken.
            """
            sess.speaking = True
            full_text     = ""
            buf           = ""

            tts_q: asyncio.Queue[str | None] = asyncio.Queue()
            play_done = asyncio.Event()

            async def _tts_worker() -> None:
                while True:
                    sentence = await tts_q.get()
                    if sentence is None:
                        break
                    if sess.done:
                        continue
                    abort[0] = False
                    try:
                        ok = await tts_stream(sentence, push, abort)
                        if not ok and not sess.done:
                            audio = await tts_rest(sentence)
                            await push(_strip_wav_header(audio))
                    except Exception as exc:
                        log.error("TTS worker error: %s", exc)
                if not sess.done:
                    await send_mark()
                play_done.set()

            asyncio.create_task(_tts_worker())
            t0        = time.perf_counter()
            ttft_done = False

            try:
                async for token in llm_stream(sess.history, sess.state, sess.ctx):
                    if sess.done:
                        break
                    if not token:
                        continue
                    if not ttft_done:
                        log.info("LLM TTFT %.0f ms", (time.perf_counter() - t0) * 1000)
                        ttft_done = True
                    full_text += token
                    buf       += token
                    if re.search(r"[।.!?]\s*$", buf.rstrip()) or len(buf) >= 100:
                        s = buf.strip()
                        if s:
                            log.debug("[ADITI chunk] %s", s)
                            record("bot_chunk", text=s)
                            await tts_q.put(s)
                        buf = ""

                if buf.strip() and not sess.done:
                    s = buf.strip()
                    log.debug("[ADITI tail] %s", s)
                    record("bot_chunk", text=s)
                    await tts_q.put(s)

            except Exception as exc:
                log.error("LLM turn error: %s", exc)
            finally:
                await tts_q.put(None)
                await play_done.wait()
                sess.speaking = False

            if full_text:
                log.info("Turn total %.0f ms", (time.perf_counter() - t0) * 1000)
            return full_text

        # ── Graceful hangup ──────────────────────────────────────────────────
        async def hangup(reason: str = "unknown") -> None:
            if sess.done:
                return
            sess.done = True
            abort[0]  = True
            log.info("Hangup: %s", reason)
            record("hangup", reason=reason)

            # Wait for Twilio to finish draining queued audio before hanging up
            if sess.marks_out > 0:
                try:
                    await asyncio.wait_for(drained.wait(), timeout=7.0)
                except asyncio.TimeoutError:
                    log.warning("Hangup: audio drain timeout")

            await asyncio.sleep(HANGUP_GRACE_SEC)

            if sess.call_sid:
                try:
                    TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)\
                        .calls(sess.call_sid)\
                        .update(status="completed")
                    log.info("Call %s terminated", sess.call_sid)
                except Exception as exc:
                    log.error("Twilio hangup error: %s", exc)

            for ws in (stt_ws, twilio_ws):
                try:
                    await ws.close()
                except Exception:
                    pass

        # ── Opening greeting — scripted, zero LLM latency ───────────────────
        async def do_opening() -> None:
            text = scripted_line("opening", sess.ctx)
            if text and not sess.done:
                sess.history.append({"role": "assistant", "content": text})
                await speak(text)

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
                        # Restore per-call customer context set by /make-call
                        if sess.call_sid in _pending_ctx:
                            sess.ctx = _pending_ctx.pop(sess.call_sid)
                        log.info("Stream=%s Call=%s", sess.stream_sid, sess.call_sid)
                        record("call_start")
                        if not opened:
                            opened = True
                            asyncio.create_task(do_opening())

                    elif evt == "media":
                        if sess.done:
                            continue
                        raw_audio = base64.b64decode(data["media"]["payload"])
                        try:
                            # Sarvam STT v3: audio frames are JSON with AudioData body.
                            # sample_rate must match the URL param (8000); encoding is always "audio/wav".
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
            # Sarvam v3 response shapes:
            #   {"type": "data",   "data": {"transcript": "...", ...}}  → final transcript
            #   {"type": "events", "data": {"signal_type": "START_SPEECH"|"END_SPEECH"}}
            #   {"type": "error",  "data": {"error": "...", "code": ...}}
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

                    msg_type  = str(frame.get("type", "")).lower()
                    inner     = frame.get("data") if isinstance(frame.get("data"), dict) else {}

                    # ── VAD events ──────────────────────────────────────────
                    if msg_type == "events":
                        signal = str(inner.get("signal_type", "")).upper()
                        if signal == "START_SPEECH":
                            log.debug("🎤 speech started")
                        elif signal == "END_SPEECH":
                            # Flush any pending interim as a final utterance
                            pending = sess.last_interim.strip()
                            if pending and not sess.speaking and not sess.done and pending != sess.last_queued:
                                log.info("[USER END] %s", pending)
                                sess.last_queued  = pending
                                sess.last_interim = ""
                                record("user_ue", text=pending)
                                await utt_q.put(pending)
                        continue

                    # ── Error events ────────────────────────────────────────
                    if msg_type == "error":
                        log.error("STT error: %s", frame)
                        continue

                    # ── Transcript — normalise across v3 and legacy shapes ──
                    transcript = (
                        inner.get("transcript")                                    # v3: data.transcript
                        or frame.get("transcript")                                 # flat legacy
                        or frame.get("text")                                       # alt legacy
                        or (                                                        # Deepgram-style
                            (((frame.get("channel") or {})
                              .get("alternatives") or [{}])[0])
                            .get("transcript")
                            if isinstance(frame.get("channel"), dict) else None
                        )
                        or ""
                    ).strip()

                    # v3 "data" messages are final transcripts (sent after VAD END_SPEECH).
                    # Legacy shapes use is_final / speech_final flags.
                    is_final = bool(
                        msg_type == "data"
                        or frame.get("is_final")
                        or frame.get("speech_final")
                        or frame.get("final")
                        or msg_type in ("transcript", "transcription_final", "utterance_end")
                    )

                    if not transcript:
                        continue

                    if is_final:
                        if sess.speaking or sess.done or transcript == sess.last_queued:
                            continue
                        log.info("[USER] %s", transcript)
                        sess.last_queued  = transcript
                        sess.last_interim = ""
                        record("user", text=transcript)
                        await utt_q.put(transcript)
                    elif transcript != sess.last_interim:
                        log.debug("[USER~] %s", transcript)
                        sess.last_interim = transcript

            except Exception as exc:
                if not sess.done:
                    log.error("STT recv error: %s", exc)
            finally:
                # If STT dies unexpectedly (wrong model, network drop, etc.)
                # and the call is still live, terminate cleanly rather than
                # sitting silent until the 25 s timeout escalates.
                if not sess.done:
                    log.error("STT WebSocket closed unexpectedly — ending call")
                    asyncio.create_task(hangup("stt_failure"))

        # ── FSM loop ─────────────────────────────────────────────────────────
        async def fsm() -> None:

            def _low_conf(text: str) -> bool:
                """True if the transcript is too short or all one repeated word."""
                words = [w for w in (text or "").split() if w.strip()]
                return len(words) < MIN_WORDS or len({w.lower() for w in words}) == 1

            async def handle_unclear() -> bool:
                """Escalate the unclear counter. Returns True if call should end."""
                sess.unclear_count += 1
                target = (
                    "unclear_sm1"   if sess.unclear_count == 1 else
                    "unclear_sm2"   if sess.unclear_count == 2 else
                    "unclear_close"
                )
                sess.state = target
                log.info("FSM unclear %d → %s", sess.unclear_count, target)
                await speak_state(target)
                if target == "unclear_close":
                    asyncio.create_task(hangup("no_response"))
                    return True
                nxt = STATES[target].get("auto_advance_to")
                if nxt:
                    sess.state = nxt
                return False

            while not sess.done:
                # Block until STT delivers a final utterance (or silence timeout)
                try:
                    utterance = await asyncio.wait_for(
                        utt_q.get(), timeout=SILENCE_TIMEOUT_SEC
                    )
                except asyncio.TimeoutError:
                    if sess.done:
                        break
                    log.info("FSM: %.0f s silence", SILENCE_TIMEOUT_SEC)
                    if await handle_unclear():
                        break
                    continue

                if sess.done:
                    break

                utterance = utterance.strip() or "[silence]"
                sess.history.append({"role": "user", "content": utterance})
                sess.last_queued = ""   # reset dedup guard for the next turn
                record("user_turn", text=utterance)

                if STATES[sess.state].get("tools"):
                    # ── Classification state: ask LLM for one keyword, no audio ──
                    tool_name = await classify(sess.history, sess.state, sess.ctx)

                    if sess.done:
                        break

                    # Low-confidence guard: override if input was too short/noisy
                    if tool_name not in ("mark_unclear", "end_call") and _low_conf(utterance):
                        log.info("FSM low-conf override: %s → mark_unclear", tool_name)
                        tool_name = "mark_unclear"

                    log.info("CLASSIFY %s → %s", sess.state, tool_name)
                    record("tool", name=tool_name)

                    if tool_name in TRANSITION_MAP:
                        next_state = TRANSITION_MAP[tool_name]
                        log.info("FSM %s → %s", sess.state, next_state)
                        sess.state = next_state
                        await speak_state(next_state)
                        if sess.done:
                            break
                        if STATES[next_state].get("terminal"):
                            asyncio.create_task(hangup(next_state))
                            break
                        nxt = STATES[next_state].get("auto_advance_to")
                        if nxt:
                            sess.state = nxt

                    elif tool_name == "mark_unclear":
                        if await handle_unclear():
                            break

                    elif tool_name == "end_call":
                        asyncio.create_task(hangup("end_call"))
                        break

                else:
                    # ── Text-generation state: stream LLM → TTS ───────────────
                    full_text = await run_turn(utterance)
                    if sess.done:
                        break
                    if full_text:
                        sess.history.append({"role": "assistant", "content": full_text})

            log.info("FSM loop ended (state=%s)", sess.state)

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
        url=f"{NGROK_URL}/outgoing-call",
        to=to,
        from_=TWILIO_PHONE_NUMBER,
    )
    _pending_ctx[call.sid] = ctx
    log.info("Call SID: %s", call.sid)
    return call.sid


if __name__ == "__main__":
    import sys
    import threading
    import uvicorn

    to = sys.argv[1].strip() if len(sys.argv) > 1 else DEFAULT_CALL_TO
    if not to:
        print("Usage: python main.py <phone_number>")
        sys.exit(1)

    log.info("Dialing %s …", to)

    def _dial() -> None:
        time.sleep(2)
        try:
            _place_call(to, dict(DEFAULT_CUSTOMER))
        except Exception as exc:
            log.error("Dial error: %s", exc)

    threading.Thread(target=_dial, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
