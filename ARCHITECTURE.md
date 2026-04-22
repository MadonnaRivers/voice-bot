# Easy Home Finance — Post-Due Collection Voice Bot

Hindi-only AI voice bot that calls customers whose loan EMI is past
due and follows the Easy Home Finance post-due collection call flow.
The bot (Aditi) always speaks in Hindi, even if the customer replies
in English or Hinglish.

---

## 1. Models & components

### 1.1 Speech ↔ Language model

| Role | Model | Notes |
|---|---|---|
| STT + LLM + TTS (unified) | **`gpt-4o-mini-realtime-preview-2024-12-17`** (OpenAI Realtime API) | Single speech-to-speech model — understands incoming phone audio and emits speech back, no separate pipeline |
| Voice | `alloy` | One of the built-in OpenAI voices. Others available: `echo`, `shimmer`, `ash`, `ballad`, `coral`, `sage`, `verse` |
| Turn detection | **Server VAD** (`threshold=0.5`, `prefix_padding_ms=300`, `silence_duration_ms=500`) | Runs inside OpenAI; detects when the caller finishes speaking and when they start speaking (used for barge-in) |
| Audio codec | **g711 µ-law @ 8 kHz** both directions | Matches Twilio's native phone audio, so no transcoding is needed |

> **Why no separate STT / TTS?**
> GPT-4o Realtime is an end-to-end speech model — the API consumes raw
> audio frames and emits raw audio frames. There is no separate Whisper
> call, no separate TTS call, and no text-in-the-middle round-trip.
> That's what makes sub-second reply latency possible on a phone call.

### 1.2 Telephony

| Component | Purpose |
|---|---|
| **Twilio Voice** | Places the outbound PSTN call to the customer's phone |
| **Twilio `<Connect><Stream>` (Media Streams)** | Opens a bidirectional WebSocket from Twilio's media servers into our app, carrying base64 µ-law frames in both directions |
| **Twilio phone number** | Caller ID on the outbound call |

### 1.3 App layer

| Component | Purpose |
|---|---|
| **FastAPI** | HTTP + WebSocket server |
| **Uvicorn** | ASGI runtime |
| **`websockets`** (Python) | Client WebSocket into OpenAI Realtime |
| **`python-dotenv`** | Loads secrets from `.env` |
| **`twilio` SDK** | REST client for `calls.create(...)` |

### 1.4 Dev infra

| Component | Purpose |
|---|---|
| **ngrok** | Public HTTPS tunnel so Twilio can reach `localhost:5050` for the TwiML webhook + Media Stream WebSocket |

---

## 2. End-to-end call flow

```
 ┌──────────┐                                            ┌────────────┐
 │ Operator │  python main.py                            │  Twilio    │
 │  (you)   │ ─────────────────────────────────────────▶ │   REST API │
 └──────────┘  client.calls.create(to=..., from_=...,    └─────┬──────┘
                                  url=/outgoing-call)          │ dials over PSTN
                                                               ▼
                                                        ┌────────────┐
                                                        │  Customer  │
                                                        │  phone     │
                                                        └─────┬──────┘
                                                              │ picks up
                                                              ▼
                                        Twilio fetches TwiML from
                                        https://<ngrok>/outgoing-call
                                                  │
                                                  ▼
                                        <Response>
                                          <Pause length="1"/>
                                          <Connect>
                                            <Stream url="wss://.../media-stream"/>
                                          </Connect>
                                        </Response>
                                                  │
                                                  ▼
                                  Twilio opens WebSocket to /media-stream
                                                  │
                                 ┌────────────────┼────────────────┐
                                 ▼                                 ▼
                        receive_from_twilio              send_to_twilio
                        (caller audio ➜ OpenAI)          (bot audio ➜ caller)
                                     \                   /
                                      \                 /
                                       ▼               ▼
                                  wss://api.openai.com/v1/realtime
                                    ?model=gpt-4o-mini-realtime-preview-2024-12-17
                                  (Authorization: Bearer $OPENAI_API_KEY)
```

### 2.1 Step by step

1. Operator runs `python main.py`, enters customer phone number.
2. Uvicorn starts on `0.0.0.0:5050`. A background thread waits 2 s and
   then calls Twilio's REST API → Twilio dials the number from
   `TWILIO_PHONE_NUMBER`.
3. When the customer picks up, Twilio requests TwiML from
   `https://<NGROK_URL>/outgoing-call`. Our FastAPI returns a
   `<Connect><Stream>` TwiML that tells Twilio to open a WebSocket to
   `/media-stream`.
4. On that WebSocket we:
   - **Open a second WebSocket** to OpenAI Realtime with our API key.
   - Send a `session.update` with µ-law format, server VAD, `alloy`
     voice, and Aditi's system prompt.
   - When `session.updated` arrives, push a seed user turn
     ("customer picked up, greet them now") and call
     `response.create` — Aditi speaks first.
5. For the rest of the call we run two coroutines concurrently:
   - **Twilio → OpenAI**: forward every `media` frame as
     `input_audio_buffer.append`.
   - **OpenAI → Twilio**: forward every `response.audio.delta` as
     a Twilio `media` frame.
6. Server-VAD on OpenAI detects when the caller stops speaking and
   auto-creates the next response. No manual `input_audio_buffer.commit`
   is needed.
7. Hangup from either side closes the WebSockets and both coroutines
   exit.

---

## 3. Barge-in / interruption

When the caller starts speaking while Aditi is still talking, three
things have to happen **simultaneously**:

1. **OpenAI stops generating.** Handled automatically by
   `turn_detection.interrupt_response = true` on the server side.
2. **OpenAI's conversation memory is truncated** to what the caller
   actually *heard*. Otherwise the model thinks it finished its
   sentence and will behave as if it did. We compute the elapsed play
   time using Twilio's media timestamps and send:
   ```json
   {
     "type": "conversation.item.truncate",
     "item_id": "<current assistant item>",
     "content_index": 0,
     "audio_end_ms": <elapsed_ms>
   }
   ```
3. **Twilio flushes its buffered audio** — otherwise the customer keeps
   hearing the bot for another ~1 s after OpenAI stops. We send:
   ```json
   { "event": "clear", "streamSid": "<sid>" }
   ```

### 3.1 Detection trigger

We listen for the `input_audio_buffer.speech_started` event from the
Realtime API. That event fires the instant server-VAD detects the
caller's voice. From there `handle_speech_started_event()` runs the
three steps above.

### 3.2 Timing math

To know how far into Aditi's utterance the caller interrupted we track:

- `response_start_timestamp_twilio` — Twilio media timestamp at the
  first `response.audio.delta` of the current reply.
- `latest_media_timestamp` — the latest Twilio media timestamp we have
  seen from the caller side.
- `mark_queue` — a queue of `mark` events we send after every audio
  chunk, popped as Twilio echoes them back; non-empty ⇒ audio is still
  in flight to the caller.

```
audio_end_ms = latest_media_timestamp - response_start_timestamp_twilio
```

This is the number of ms of Aditi's utterance the caller heard before
they started speaking. We feed that back to OpenAI so its memory is
consistent with the caller's reality.

---

## 4. Call flow (from Easy Home Finance PDF)

```
                            Aditi opening line
                "Your EMI of <due_amount> is overdue. Pay today?"
                                     │
  ┌───────────────────┬──────────────┼──────────────┬───────────────────┐
  │                   │              │              │                   │
invalid            wrong no/      busy/          already           deceased
/ no input          DND          later           paid
  │                   │              │              │                   │
SM1 retry           EOC           EOC            Ask date           Sympathy
  │                  apology      "call back"    → Ask mode         → EOC
SM2 retry                                         → Thanks EOC
  │
EOC "call back"

  ┌───────────────────┬──────────────┬───────────────────┬──────────────┐
  │                   │              │                   │              │
loan closed      no loan/        dispute           pay offline     call answered
  │               not my loan     (wrong amount)   branch / RM     on behalf
EOC              EOC              → Contact RM     → Pay via SMS   → Inform
 "still active"  "contact          → EOC               link / app       borrower
                  cust. care"                        → EOC               → EOC

        ┌───────────────────┬───────────────────┬──────────────────┐
        │                   │                   │                  │
     PTP within           Pay later         NACH: debit      Partial
     3 days (today,       / RTP without      again /         payment
     tomorrow, day        reason             balance added
     after)               │                  │                │
        │             "why?" retry          EOC "already     "use SMS link
     "thanks, use     → classify reason     bounced, use     for partial,
      SMS link"       → "pay ASAP to         SMS link"       pay rest ASAP"
        │              protect CIBIL"       → EOC            → EOC
       EOC             → EOC
```

### 4.1 FAQ handling

Any time the customer asks one of the 7 FAQs (who are you / due date /
which loan / more time / payment modes / are you a robot / is this
spam), Aditi answers it verbatim, then returns to the flow stage she
was at (e.g. "So, can you clear this payment today?").

### 4.2 Fallbacks

- **Invalid / no input after two retries** → polite EOC, "we'll call
  back later."
- **User asks to speak to a human** → "I can't transfer; please call
  customer care." → EOC.

### 4.3 Language policy

- The bot speaks **only in Hindi**, end to end.
- If the customer replies in English or Hinglish, Aditi still responds
  in Hindi — she never switches to English.
- Technical terms that every Indian phone customer understands —
  "EMI", "loan", "SMS", "app", "branch", "customer care",
  "relationship manager", "CIBIL" — stay in English inside the Hindi
  sentence, since translating them hurts comprehension.
- Implemented entirely via the system prompt. The Realtime model does
  multilingual audio understanding natively, so the caller can answer
  in any language and Aditi will still reply in Hindi with the
  `alloy` voice.

---

## 5. Files

| File | Purpose |
|---|---|
| `main.py` | FastAPI app, Twilio ⇄ OpenAI bridge, barge-in logic |
| `prompts/system_prompt.txt` | Aditi persona + full collection flow + FAQs (bilingual) |
| `requirements.txt` | Pinned Python deps |
| `.env` | Secrets: `OPENAI_API_KEY`, Twilio creds, `NGROK_URL`, `PORT` |
| `Readme.md` | Original setup instructions |
| `ARCHITECTURE.md` | This file |

---

## 6. Environment variables

| Variable | Used for |
|---|---|
| `OPENAI_API_KEY` | Auth header on the Realtime WebSocket |
| `TWILIO_ACCOUNT_SID` | Twilio REST auth |
| `TWILIO_AUTH_TOKEN` | Twilio REST auth |
| `TWILIO_PHONE_NUMBER` | Caller ID on outbound calls |
| `NGROK_URL` | Public HTTPS base that Twilio hits for `/outgoing-call` |
| `PORT` | Uvicorn port (default `5050`) |

---

## 7. Running it

```bash
# One-time
pip install -r requirements.txt

# Expose localhost to the internet
ngrok http 5050

# Put the https://… forwarding URL into .env as NGROK_URL
# Then:
python main.py
# → enter the customer phone number when prompted
```

You should see in the logs, in order:

1. `Call initiated with SID: CA…`
2. `POST /outgoing-call HTTP/1.1 200 OK`
3. `Client connected` + `WebSocket /media-stream accepted`
4. `Sending session update…`
5. `[OpenAI] session.created`
6. `[OpenAI] session.updated`
7. `[OpenAI] input_audio_buffer.speech_started` (when caller talks)
8. `Caller started speaking -> interrupting bot` (on barge-in)

---

## 8. Known limitations / next steps

- **No dynamic customer data injection.** The prompt currently hardcodes
  a sample customer (`Mr. Sharma`, ₹15,500, account `4321`). For
  production, format the system prompt with real values per call before
  sending `session.update`.
- **No call logging / transcript storage.** Nothing is persisted today.
  Add `response.audio_transcript.delta` logging + a DB to save call
  outcomes and PTP commitments.
- **No auth on `/make-call`.** Anyone who finds the ngrok URL can dial
  your Twilio number. Add a shared-secret header before deploying.
- **No CI/CD / Dockerfile.** See `Readme.md` for local-only setup.
- **No call-ending logic.** The bot says "EOC" but doesn't actually
  hang up. To hang up programmatically, detect `EOC` in the transcript
  stream and call Twilio's REST API `calls(call_sid).update(status='completed')`.
- **`.env` is tracked in git.** Move it to `.gitignore` and ship a
  `.env.example` before pushing anywhere public.
