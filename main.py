"""
Aditi — Hindi EMI Collection Voice Bot
STT : Deepgram Nova-3       (WebSocket, mulaw 8 kHz)
LLM : gpt-4.1-mini          (streaming, temp 0.3)
TTS : Sarvam Bulbul v3      (HTTP-streaming POST — chunks arrive while synth)

Why HTTP-stream TTS instead of Sarvam WS TTS?
  The WS TTS SDK/protocol has version-specific quirks (extra_headers vs
  additional_headers, async-with vs await, frame schema changes).
  Sarvam's REST-stream endpoint (POST /text-to-speech/stream) returns audio
  as a chunked HTTP response — first bytes arrive in ~150 ms, same latency
  benefit as WS, zero WebSocket complexity.

Latency budget (target ≈ 1.2–1.8 s):
  Deepgram endpointing 200 ms         ~200–400 ms
  LLM time-to-first-token             ~600–800 ms
  Sarvam HTTP-stream first chunk      ~150–250 ms
  ─────────────────────────────────────────────
  Total                               ~950–1450 ms
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
import websockets
import websockets.exceptions as ws_exc
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from openai import AsyncOpenAI
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse
from dotenv import load_dotenv

load_dotenv()

# ────────────────────────────────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────────────────────────────────
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
DEEPGRAM_API_KEY    = os.getenv("DEEPGRAM_API_KEY")
SARVAM_API_KEY      = os.getenv("SARVAM_API_KEY")
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
NGROK_URL           = os.getenv("NGROK_URL")
PORT                = int(os.getenv("PORT", 5050))
DEFAULT_CALL_TO     = os.getenv("CALL_TO", "+917977365303")
TRANSCRIPTS_DIR     = os.getenv("TRANSCRIPTS_DIR", "transcripts")

LLM_MODEL        = "gpt-4.1-mini"
SARVAM_VOICE     = os.getenv("SARVAM_VOICE", "priya")
SARVAM_LANGUAGE  = "hi-IN"
HANGUP_GRACE_SEC = 1.8
MIN_WORDS        = 2

# Deepgram — mulaw 8 kHz direct from Twilio
DEEPGRAM_WS_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model=nova-3&language=multi&encoding=mulaw&sample_rate=8000"
    "&channels=1&punctuate=true&interim_results=true"
    "&endpointing=200&utterance_end_ms=1000"
    "&vad_events=true&smart_format=true&no_delay=true"
)

# Sarvam TTS — REST mode (stable playback).
SARVAM_STREAM_URL = "https://api.sarvam.ai/text-to-speech/stream"
SARVAM_REST_URL   = "https://api.sarvam.ai/text-to-speech"

# Persistent HTTP client — keeps TCP/TLS alive between calls (~100 ms saved)
_http = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=4.0, read=15.0, write=5.0, pool=5.0),
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)

# ────────────────────────────────────────────────────────────────────────────
# Prompt helpers
# ────────────────────────────────────────────────────────────────────────────
def load_prompt(rel: str) -> str:
    base = os.path.dirname(os.path.realpath(__file__))
    with open(os.path.join(base, "prompts", f"{rel}.txt"), encoding="utf-8") as f:
        return f.read().strip()

# ────────────────────────────────────────────────────────────────────────────
# FSM
# ────────────────────────────────────────────────────────────────────────────
PERSONA = load_prompt("persona")

STATES: dict = {
    "opening": {
        "prompt_file": "states/opening",
        "tools": ["goto_ptp","goto_cannot_pay","goto_will_not_pay",
                  "goto_no_loan","goto_wrong_emi","goto_already_paid",
                  "answer_faq_who_are_you","mark_unclear"],
        "terminal": False,
    },
    "ptp":           {"prompt_file":"states/ptp",           "tools":["end_call"],"terminal":True},
    "cannot_pay":    {"prompt_file":"states/cannot_pay",    "tools":["end_call"],"terminal":True},
    "will_not_pay":  {"prompt_file":"states/will_not_pay",  "tools":["end_call"],"terminal":True},
    "no_loan":       {"prompt_file":"states/no_loan",       "tools":["end_call"],"terminal":True},
    "wrong_emi":     {"prompt_file":"states/wrong_emi",     "tools":["end_call"],"terminal":True},
    "unclear_close": {"prompt_file":"states/unclear_close", "tools":["end_call"],"terminal":True},
    "already_paid_ask_date": {
        "prompt_file":"states/already_paid_ask_date",
        "tools":["goto_already_paid_mode","mark_unclear"],"terminal":False,
    },
    "already_paid_ask_mode": {
        "prompt_file":"states/already_paid_ask_mode",
        "tools":["goto_already_paid_thanks","mark_unclear"],"terminal":False,
    },
    "already_paid_thanks": {
        "prompt_file":"states/already_paid_thanks","tools":["end_call"],"terminal":True,
    },
    "faq_who_are_you": {
        "prompt_file":"states/faq_who_are_you","tools":[],"terminal":False,
        "auto_advance_to":"opening",
    },
    "unclear_sm1": {
        "prompt_file":"states/unclear_sm1","tools":[],"terminal":False,
        "auto_advance_to":"opening",
    },
    "unclear_sm2": {
        "prompt_file":"states/unclear_sm2","tools":[],"terminal":False,
        "auto_advance_to":"opening",
    },
}

def _clf(name: str, desc: str) -> dict:
    return {"type":"function","function":{
        "name":name,
        "description":desc+" Invoke silently — no audio.",
        "parameters":{"type":"object","properties":{
            "caller_said":{"type":"string","description":"Hindi paraphrase 5-15 words."}
        },"required":["caller_said"]},
    }}

TOOL_DEFS: dict = {
    "goto_ptp":                _clf("goto_ptp",               "Caller will pay within 3 days."),
    "goto_cannot_pay":         _clf("goto_cannot_pay",        "Caller cannot pay or needs >3 days."),
    "goto_will_not_pay":       _clf("goto_will_not_pay",      "Caller refuses without reason."),
    "goto_no_loan":            _clf("goto_no_loan",           "Caller denies this loan."),
    "goto_wrong_emi":          _clf("goto_wrong_emi",         "Caller disputes EMI amount."),
    "goto_already_paid":       _clf("goto_already_paid",      "Caller says already paid."),
    "goto_already_paid_mode":  _clf("goto_already_paid_mode", "Caller gave payment date."),
    "goto_already_paid_thanks":_clf("goto_already_paid_thanks","Caller gave payment mode."),
    "answer_faq_who_are_you":  _clf("answer_faq_who_are_you", "Caller asked who you are."),
    "mark_unclear":            _clf("mark_unclear",           "Unclear/noise/silence."),
    "end_call": {"type":"function","function":{
        "name":"end_call","description":"Hang up after speaking closing line.",
        "parameters":{"type":"object","properties":{
            "reason":{"type":"string"}
        },"required":["reason"]},
    }},
}

TRANSITION_MAP: dict = {
    "goto_ptp":"ptp","goto_cannot_pay":"cannot_pay","goto_will_not_pay":"will_not_pay",
    "goto_no_loan":"no_loan","goto_wrong_emi":"wrong_emi",
    "goto_already_paid":"already_paid_ask_date",
    "goto_already_paid_mode":"already_paid_ask_mode",
    "goto_already_paid_thanks":"already_paid_thanks",
    "answer_faq_who_are_you":"faq_who_are_you",
}

def sys_prompt(state: str) -> str:
    return f"{PERSONA}\n\n---\n\n{load_prompt(STATES[state]['prompt_file'])}"

def tools_for(state: str) -> list:
    return [TOOL_DEFS[t] for t in STATES[state]["tools"]]

def scripted_line(state: str) -> str:
    """Extract the first double-quoted block from a state prompt file."""
    try:
        txt = load_prompt(STATES[state]["prompt_file"])
    except Exception:
        return ""
    m = re.search(r'"([^"]+)"', txt, re.DOTALL)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""

def low_conf(t: str) -> bool:
    words = [w for w in (t or "").split() if w.strip()]
    return len(words) < MIN_WORDS or len({w.lower() for w in words}) == 1

# ────────────────────────────────────────────────────────────────────────────
# TTS — Sarvam HTTP-stream  (primary)
# ────────────────────────────────────────────────────────────────────────────
async def tts_stream(text: str, on_chunk, abort: list) -> bool:
    """
    POST to /text-to-speech/stream with stream=True.
    httpx yields raw bytes as they arrive — first chunk in ~150 ms.
    Calls on_chunk(bytes) for each audio fragment.
    abort[0]=True stops early (barge-in).
    Returns True on success, False on error.
    """
    headers = {
        "api-subscription-key": SARVAM_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "target_language_code": SARVAM_LANGUAGE,
        "speaker": SARVAM_VOICE,
        "model": "bulbul:v3",
        "speech_sample_rate": 8000,
        "output_audio_codec": "mulaw",
        "enable_preprocessing": True,
        "pace": 1.1,
    }
    try:
        async with _http.stream("POST", SARVAM_STREAM_URL,
                                json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                print(f"[TTS-STREAM] HTTP {resp.status_code}: {body[:200]}")
                return False

            # Keep this path simple and identical in spirit to the requests example:
            # consume streamed bytes and forward immediately.
            got_audio = False
            container_mode = None  # None|raw|wav|unsupported
            wav_buf = b""
            wav_data_started = False
            async for chunk in resp.aiter_bytes(chunk_size=8192):
                if abort[0]:
                    return True
                if not chunk:
                    continue

                # Detect stream type once from first bytes.
                if container_mode is None:
                    probe = chunk[:12]
                    if probe.startswith(b"RIFF") and b"WAVE" in probe:
                        container_mode = "wav"
                    elif probe.startswith(b"ID3") or (len(probe) >= 2 and probe[0] == 0xFF and (probe[1] & 0xE0) == 0xE0):
                        container_mode = "unsupported"
                    elif probe.startswith(b"{") or probe.startswith(b"["):
                        container_mode = "unsupported"
                    else:
                        container_mode = "raw"

                if container_mode == "raw":
                    got_audio = True
                    await on_chunk(chunk)
                    continue

                if container_mode == "unsupported":
                    print("[TTS-STREAM] unsupported streamed container (json/mp3); cannot play on Twilio mulaw path")
                    return False

                # WAV container path:
                # Twilio media stream expects raw mulaw bytes, not RIFF headers.
                wav_buf += chunk
                if not wav_data_started:
                    idx = wav_buf.find(b"data")
                    if idx == -1:
                        # Need more header bytes
                        continue
                    # "data" + 4-byte size header
                    payload_start = idx + 8
                    if len(wav_buf) <= payload_start:
                        continue
                    audio_payload = wav_buf[payload_start:]
                    wav_buf = b""
                    wav_data_started = True
                    if audio_payload:
                        got_audio = True
                        await on_chunk(audio_payload)
                    continue

                # After data chunk starts, everything else is audio payload bytes.
                got_audio = True
                await on_chunk(wav_buf)
                wav_buf = b""

        if not got_audio:
            print("[TTS-STREAM] stream ended with no audio")
            return False
        return True
    except Exception as e:
        print(f"[TTS-STREAM] {type(e).__name__}: {e}")
        return False


async def tts_rest(text: str) -> bytes:
    headers = {
        "api-subscription-key": SARVAM_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "target_language_code": SARVAM_LANGUAGE,
        "speaker": SARVAM_VOICE,
        "model": "bulbul:v3",
        "speech_sample_rate": 8000,
        "output_audio_codec": "mulaw",
        "enable_preprocessing": True,
        "pace": 1.1,
    }
    r = await _http.post(SARVAM_REST_URL, json=payload, headers=headers)
    if r.status_code >= 400:
        raise RuntimeError(f"Sarvam REST {r.status_code}: {r.text[:200]}")
    data = r.json()
    b64 = (data.get("audios") or [None])[0] or data.get("audio")
    if not b64:
        raise RuntimeError(f"No audio: {str(data)[:150]}")
    return base64.b64decode(b64)

# ────────────────────────────────────────────────────────────────────────────
# LLM — streaming
# ────────────────────────────────────────────────────────────────────────────
oai = AsyncOpenAI(api_key=OPENAI_API_KEY)

async def llm_stream(history: list, state: str):
    """
    Async generator.
    Yields: (token_str, None, None)   — text token
            (None, tool_name, args)   — tool call
            (None, None, None)        — stream done (text only)
    """
    tools = tools_for(state)
    kw: dict = {
        "model": LLM_MODEL,
        "messages": [{"role":"system","content":sys_prompt(state)}] + history,
        "temperature": 0.3,
        "max_tokens": 90,
        "stream": True,
    }
    if tools:
        kw["tools"] = tools
        kw["tool_choice"] = "auto"

    tool_name, tool_args = None, ""
    try:
        async for chunk in await oai.chat.completions.create(**kw):
            d = chunk.choices[0].delta if chunk.choices else None
            if not d:
                continue
            if d.tool_calls:
                tc = d.tool_calls[0]
                if tc.function.name:
                    tool_name = tc.function.name
                if tc.function.arguments:
                    tool_args += tc.function.arguments
            elif d.content:
                yield d.content, None, None
    except Exception as e:
        print(f"[LLM] {e}")

    yield (None, tool_name, tool_args) if tool_name else (None, None, None)

# ────────────────────────────────────────────────────────────────────────────
# FastAPI
# ────────────────────────────────────────────────────────────────────────────
app = FastAPI()

for _v, _n in [
    (OPENAI_API_KEY,"OPENAI_API_KEY"),(DEEPGRAM_API_KEY,"DEEPGRAM_API_KEY"),
    (SARVAM_API_KEY,"SARVAM_API_KEY"),(TWILIO_ACCOUNT_SID,"TWILIO_ACCOUNT_SID"),
    (TWILIO_AUTH_TOKEN,"TWILIO_AUTH_TOKEN"),(TWILIO_PHONE_NUMBER,"TWILIO_PHONE_NUMBER"),
]:
    if not _v:
        raise ValueError(f"Missing {_n} in .env")

@app.get("/", response_class=HTMLResponse)
async def index():
    return {"message": "Aditi — Deepgram STT + GPT-4.1-mini + Sarvam HTTP-stream TTS"}

@app.post("/make-call")
async def make_call(request: Request):
    data = await request.json()
    to   = data.get("to")
    if not to:
        return {"error": "to required"}
    call = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).calls.create(
        url=f"{NGROK_URL}/outgoing-call", to=to, from_=TWILIO_PHONE_NUMBER)
    return {"call_sid": call.sid}

@app.api_route("/outgoing-call", methods=["GET","POST"])
async def outgoing_call(request: Request):
    r = VoiceResponse(); r.pause(length=1)
    c = Connect(); c.stream(url=f'wss://{request.url.hostname}/media-stream')
    r.append(c)
    return HTMLResponse(str(r), media_type="application/xml")

# ────────────────────────────────────────────────────────────────────────────
# Main WebSocket handler
# ────────────────────────────────────────────────────────────────────────────
@app.websocket("/media-stream")
async def media_stream(twilio_ws: WebSocket):
    print("Client connected")
    await twilio_ws.accept()

    # All shared mutable state in one dict — all closures see mutations live
    S = {
        "stream_sid":  None,
        "call_sid":    None,
        "state":       "opening",
        "history":     [],
        "done":        False,   # True = stop everything immediately
        "speaking":    False,
        "last_final":  "",
        "last_interim":"",
        "last_queued": "",
        "unclear":     0,
        "marks_out":   0,
        "tpath":       None,
    }
    abort   = [False]               # mutable flag for barge-in abort
    utt_q:  asyncio.Queue = asyncio.Queue()
    drained = asyncio.Event();      drained.set()

    dg_headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}

    # ── Connect to Deepgram ──────────────────────────────────────────────
    # Use await (not async-with) for compatibility with all websockets versions
    dg_ws = await websockets.connect(DEEPGRAM_WS_URL, extra_headers=dg_headers)

    try:
        # ── Transcript logger ────────────────────────────────────────────
        def log(ev: str, **kw):
            if not S["tpath"]:
                ts  = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
                sid = (S["call_sid"] or "x").replace("/","_")
                Path(TRANSCRIPTS_DIR).mkdir(parents=True, exist_ok=True)
                S["tpath"] = f"{TRANSCRIPTS_DIR}/{ts}_{sid}.jsonl"
            row = {
                "ts": datetime.utcnow().isoformat(timespec="milliseconds")+"Z",
                "ev": ev, "state": S["state"], "sid": S["call_sid"], **kw
            }
            try:
                with open(S["tpath"],"a",encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False)+"\n")
            except Exception:
                pass

        # ── Push mulaw chunk to Twilio ───────────────────────────────────
        async def push(chunk: bytes):
            if S["done"] or abort[0]:
                return
            try:
                # Twilio PSTN playback is most reliable with 20ms mulaw frames (160 bytes).
                for i in range(0, len(chunk), 160):
                    if S["done"] or abort[0]:
                        return
                    frame = chunk[i:i+160]
                    if not frame:
                        return
                    await twilio_ws.send_json({
                        "event": "media",
                        "streamSid": S["stream_sid"],
                        "media": {"payload": base64.b64encode(frame).decode()},
                    })
                    # Pace packets to avoid flooding Twilio's media buffer.
                    await asyncio.sleep(0.01)
            except Exception:
                pass

        def _strip_wav_header(audio: bytes) -> bytes:
            if audio.startswith(b"RIFF"):
                idx = audio.find(b"data")
                if idx != -1:
                    return audio[idx + 8:]
            return audio

        # ── Core speak: REST TTS ───────────────────────────────────────────
        async def speak(text: str) -> None:
            text = text.strip()
            if not text or S["done"] or not S["stream_sid"]:
                return
            t0 = time.perf_counter()
            print(f'[ADITI] "{text}"')
            log("bot", text=text)
            S["speaking"] = True
            abort[0]      = False
            try:
                audio = await tts_rest(text)
                audio = _strip_wav_header(audio)
                await push(audio)

                if not S["done"]:
                    drained.clear()
                    S["marks_out"] += 1
                    await twilio_ws.send_json({
                        "event": "mark",
                        "streamSid": S["stream_sid"],
                        "mark": {"name": "tts_done"},
                    })
                ms = int((time.perf_counter()-t0)*1000)
                print(f"[TTS] {ms}ms")
            except Exception as e:
                if not S["done"]:
                    print(f"[TTS ERR] {e}")
            finally:
                S["speaking"] = False

        # ── Speak scripted state line — ZERO LLM latency ────────────────
        async def speak_state(state: str) -> None:
            """
            Every terminal / transition state has a fixed Hindi line in its
            prompt file inside double-quotes.  We extract it and go straight
            to TTS — no LLM call needed.  Saves ~700-800 ms per transition.
            """
            text = scripted_line(state)
            if not text:
                # States without a quoted line (e.g. faq_who_are_you):
                # do a single non-streaming LLM call
                r = await oai.chat.completions.create(
                    model=LLM_MODEL,
                    messages=[{"role":"system","content":sys_prompt(state)},
                              {"role":"user","content":"बोलें।"}],
                    temperature=0.3, max_tokens=90,
                )
                text = (r.choices[0].message.content or "").strip()
            if text:
                S["history"].append({"role":"assistant","content":text})
                await speak(text)

        # ── LLM streaming + sentence-level TTS pipeline ──────────────────
        async def run_turn(tr: str) -> tuple[str, str|None, str|None]:
            """
            Stream tokens from LLM.
            On every sentence boundary (।  .  !  ?) flush accumulated tokens
            to the TTS worker immediately — caller hears first audio before
            the LLM has even finished generating the second sentence.

            Returns (full_text, tool_name, tool_args_raw).
            """
            S["speaking"] = True
            full, tool_name, tool_args_raw = "", None, None
            buf = ""

            # Serial TTS worker: plays sentences in order, no overlap
            tts_q: asyncio.Queue = asyncio.Queue()
            play_done = asyncio.Event()

            async def _worker():
                while True:
                    sentence = await tts_q.get()
                    if sentence is None:
                        break
                    if S["done"]:
                        continue
                    abort[0] = False
                    try:
                        audio = await tts_rest(sentence)
                        audio = _strip_wav_header(audio)
                        await push(audio)
                    except Exception as e:
                        print(f"[TTS WORKER] REST failed: {e}")
                play_done.set()

            worker = asyncio.create_task(_worker())
            t0     = time.perf_counter()
            ttft   = False

            try:
                async for token, tname, targs in llm_stream(S["history"], S["state"]):
                    if S["done"]:
                        break
                    if token is None and tname is None:
                        break                           # stream complete
                    if tname:
                        tool_name     = tname
                        tool_args_raw = targs
                        break

                    if token:
                        if not ttft:
                            print(f"[LLM TTFT] {int((time.perf_counter()-t0)*1000)}ms")
                            ttft = True
                        full += token
                        buf  += token
                        # Flush on sentence boundary
                        if re.search(r"[।.!?]\s*$", buf.rstrip()):
                            s = buf.strip()
                            if s:
                                print(f'[ADITI chunk] "{s}"')
                                log("bot_chunk", text=s)
                                await tts_q.put(s)
                            buf = ""

                # Flush leftover text
                if buf.strip() and not tool_name and not S["done"]:
                    s = buf.strip()
                    print(f'[ADITI tail] "{s}"')
                    log("bot_chunk", text=s)
                    await tts_q.put(s)

            except Exception as e:
                print(f"[LLM ERR] {e}")
            finally:
                await tts_q.put(None)       # stop worker
                await play_done.wait()
                S["speaking"] = False

            if full:
                print(f"[TURN] {int((time.perf_counter()-t0)*1000)}ms total")

            return full, tool_name, tool_args_raw

        # ── Hangup ───────────────────────────────────────────────────────
        async def hangup(reason: str = "?") -> None:
            if S["done"]:
                return
            print(f"[HANGUP] {reason}")
            log("hangup", reason=reason)
            S["done"]  = True
            abort[0]   = True
            # Wait for Twilio mark-ack so caller hears closing line fully
            if S["marks_out"] > 0:
                try:
                    await asyncio.wait_for(drained.wait(), timeout=6.0)
                except asyncio.TimeoutError:
                    print("[HANGUP] mark timeout")
            await asyncio.sleep(HANGUP_GRACE_SEC)
            if S["call_sid"]:
                try:
                    Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)\
                        .calls(S["call_sid"]).update(status="completed")
                    print(f"[HANGUP] call {S['call_sid']} ended")
                except Exception as e:
                    print(f"[HANGUP] {e}")
            for ws in (dg_ws, twilio_ws):
                try:
                    await ws.close()
                except Exception:
                    pass

        # ── Opening greeting (no LLM needed — scripted line) ─────────────
        async def do_opening():
            text = scripted_line("opening")
            if text and not S["done"]:
                S["history"].append({"role":"assistant","content":text})
                await speak(text)

        # ── Twilio receiver ───────────────────────────────────────────────
        async def recv_twilio():
            opened = False
            try:
                async for raw in twilio_ws.iter_text():
                    if S["done"]:
                        break
                    evt = json.loads(raw).get("event")
                    data = json.loads(raw)

                    if evt == "start":
                        S["stream_sid"] = data["start"]["streamSid"]
                        S["call_sid"]   = data["start"].get("callSid")
                        print(f"[STREAM] {S['stream_sid']} call={S['call_sid']}")
                        log("call_start")
                        if not opened:
                            opened = True
                            asyncio.create_task(do_opening())

                    elif evt == "media":
                        if S["done"]:
                            continue
                        # Do NOT clear bot audio on every inbound media frame.
                        # Twilio sends media frames continuously, and clearing here
                        # cancels playback before caller hears anything.
                        audio = base64.b64decode(data["media"]["payload"])
                        try:
                            await dg_ws.send(audio)
                        except (ws_exc.ConnectionClosedOK, ws_exc.ConnectionClosed):
                            break
                        except Exception as e:
                            print(f"[DG send] {e}"); break

                    elif evt == "mark":
                        if S["marks_out"] > 0:
                            S["marks_out"] -= 1
                        if S["marks_out"] <= 0:
                            S["marks_out"] = 0
                            drained.set()

            except WebSocketDisconnect:
                print("Twilio disconnected")
            except RuntimeError as e:
                if "WebSocket is not connected" not in str(e):
                    raise
            finally:
                try:
                    await dg_ws.close()
                except Exception:
                    pass

        # ── Deepgram receiver ─────────────────────────────────────────────
        async def recv_deepgram():
            try:
                async for msg in dg_ws:
                    if S["done"]:
                        break
                    if isinstance(msg, bytes):
                        continue
                    try:
                        data = json.loads(msg)
                    except Exception:
                        continue

                    t = data.get("type")
                    if t == "Results":
                        alts = data.get("channel",{}).get("alternatives",[{}])
                        tr   = (alts[0].get("transcript") or "").strip()
                        if not tr:
                            continue
                        is_final = bool(data.get("is_final") or data.get("speech_final"))
                        if is_final:
                            if S["speaking"] or S["done"]:
                                continue
                            if tr == S["last_queued"]:
                                continue
                            print(f'[STT] "{tr}"')
                            S["last_final"]  = tr
                            S["last_interim"]= ""
                            S["last_queued"] = tr
                            log("user", text=tr)
                            await utt_q.put(tr)
                        elif tr != S["last_interim"]:
                            print(f'[STT ~] "{tr}"')
                            S["last_interim"] = tr

                    elif t == "UtteranceEnd":
                        tr = S["last_interim"].strip()
                        if tr and not S["speaking"] and not S["done"] \
                                and tr != S["last_queued"]:
                            print(f'[STT UE] "{tr}"')
                            S["last_final"]  = tr
                            S["last_interim"]= ""
                            S["last_queued"] = tr
                            log("user_ue", text=tr)
                            await utt_q.put(tr)

                    elif t == "SpeechStarted":
                        print("[🎤]")
                    elif t == "Error":
                        print(f"[DG ERR] {data}")

            except Exception as e:
                if not S["done"]:
                    print(f"[DG recv] {e}")

        # ── FSM loop ──────────────────────────────────────────────────────
        async def fsm():

            async def do_unclear() -> bool:
                """Handle unclear response. Returns True → exit loop."""
                S["unclear"] += 1
                tgt = ("unclear_sm1"   if S["unclear"] == 1 else
                       "unclear_sm2"   if S["unclear"] == 2 else
                       "unclear_close")
                S["state"] = tgt
                print(f"[FSM] unclear {S['unclear']} → {tgt}")
                await speak_state(tgt)
                if tgt == "unclear_close":
                    asyncio.create_task(hangup("no_response"))
                    return True
                nxt = STATES[tgt].get("auto_advance_to")
                if nxt:
                    S["state"] = nxt
                return False

            while not S["done"]:

                # Wait for a final utterance from Deepgram
                try:
                    tr = await asyncio.wait_for(utt_q.get(), timeout=25.0)
                except asyncio.TimeoutError:
                    if S["done"]:
                        break
                    print("[FSM] 25 s silence")
                    if await do_unclear():
                        break
                    continue

                if S["done"]:
                    break

                tr = tr.strip() or "[silence]"
                S["history"].append({"role":"user","content":tr})
                log("user_turn", text=tr)

                # Stream LLM + TTS concurrently
                full, tool_name, tool_args_raw = await run_turn(tr)

                if S["done"]:
                    break

                # Low-confidence safety net
                if (tool_name and tool_name not in ("mark_unclear","end_call")
                        and low_conf(tr) and low_conf(S["last_final"])):
                    print(f"[FSM] low-conf: {tool_name} → mark_unclear")
                    tool_name     = "mark_unclear"
                    tool_args_raw = json.dumps({"caller_said":tr})

                if tool_name:
                    print(f"[TOOL] {tool_name}  args={tool_args_raw}")
                    log("tool", name=tool_name, args=(tool_args_raw or ""))
                    try:
                        args = json.loads(tool_args_raw or "{}")
                    except Exception:
                        args = {}

                    if tool_name in TRANSITION_MAP:
                        ns = TRANSITION_MAP[tool_name]
                        print(f"[FSM] {S['state']} → {ns}")
                        S["state"] = ns
                        await speak_state(ns)      # scripted, zero LLM wait
                        if S["done"]:
                            break
                        if STATES[ns].get("terminal"):
                            asyncio.create_task(hangup(args.get("reason", ns)))
                            break
                        nxt = STATES[ns].get("auto_advance_to")
                        if nxt:
                            S["state"] = nxt

                    elif tool_name == "mark_unclear":
                        if await do_unclear():
                            break

                    elif tool_name == "end_call":
                        asyncio.create_task(hangup(args.get("reason","?")))
                        break

                elif full:
                    # Plain text already spoken; just add to history
                    S["history"].append({"role":"assistant","content":full})

            print("[FSM] done")

        # ── Run all three coroutines concurrently ─────────────────────────
        await asyncio.gather(recv_twilio(), recv_deepgram(), fsm())

    finally:
        # Always close Deepgram WS cleanly
        try:
            await dg_ws.close()
        except Exception:
            pass

# ────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ────────────────────────────────────────────────────────────────────────────
def place_call(to: str) -> str:
    call = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).calls.create(
        url=f"{NGROK_URL}/outgoing-call", to=to, from_=TWILIO_PHONE_NUMBER)
    print(f"Call SID: {call.sid}")
    return call.sid

if __name__ == "__main__":
    import sys, threading, uvicorn
    to = sys.argv[1].strip() if len(sys.argv) > 1 else DEFAULT_CALL_TO
    print(f"Dialing {to} …")
    def _dial():
        time.sleep(2)
        try:
            place_call(to)
        except Exception as e:
            print(f"Dial error: {e}")
    threading.Thread(target=_dial, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT)