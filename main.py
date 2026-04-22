# import time
# import os
# import json
# import asyncio
# import websockets
# from websockets.exceptions import ConnectionClosedOK, ConnectionClosed
# from fastapi import FastAPI, WebSocket, Request
# from fastapi.responses import HTMLResponse
# from fastapi.websockets import WebSocketDisconnect
# from twilio.rest import Client
# from twilio.twiml.voice_response import VoiceResponse, Connect
# from dotenv import load_dotenv

# load_dotenv()

# def load_prompt(relative_path):
#     """Load a prompt file from the ./prompts/ directory.
#     `relative_path` is WITHOUT the .txt extension.
#     Examples: 'persona', 'states/opening', 'states/ptp'.
#     """
#     dir_path = os.path.dirname(os.path.realpath(__file__))
#     prompt_path = os.path.join(dir_path, 'prompts', f'{relative_path}.txt')
#     try:
#         with open(prompt_path, 'r', encoding='utf-8') as file:
#             return file.read().strip()
#     except FileNotFoundError:
#         print(f"Could not find file: {prompt_path}")
#         raise

# # --- Configuration ------------------------------------------------------------
# OPENAI_API_KEY      = os.getenv('OPENAI_API_KEY')
# TWILIO_ACCOUNT_SID  = os.getenv('TWILIO_ACCOUNT_SID')
# TWILIO_AUTH_TOKEN   = os.getenv('TWILIO_AUTH_TOKEN')
# TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER')
# NGROK_URL           = os.getenv('NGROK_URL')
# PORT                = int(os.getenv('PORT', 5050))

# # Default outbound number. `python main.py` will dial this automatically.
# # Override for one run by either passing a CLI arg (`python main.py +91...`)
# # or by setting CALL_TO in the .env file.
# DEFAULT_CALL_TO = os.getenv('CALL_TO', '+917977365303')

# # Speech-to-speech via OpenAI **Realtime API** — model id below is
# # **gpt-4o-mini** (multimodal realtime), *not* a separate "GPT-4.1 mini"
# # text-only SKU. Docs: platform.openai.com/docs/guides/realtime-models-prompting
# OPENAI_REALTIME_MODEL = 'gpt-4o-mini-realtime-preview-2024-12-17'
# # Persona is ALWAYS loaded. State prompts are loaded per-state and
# # concatenated onto the persona when we call `session.update`.
# PERSONA = load_prompt('persona')
# # Aditi is a female virtual assistant. 'shimmer' is the warmest, most
# # clearly-feminine voice in the Realtime API voice set and pronounces
# # Hindi/English code-mixed content cleanly. Alternatives worth trying:
# # 'coral' (newer, also feminine) or 'sage' (softer, more neutral).
# VOICE = 'shimmer'

# SHOW_TIMING_MATH = False
# MIN_TRANSCRIPT_WORDS_FOR_CLASSIFICATION = 2
# HANGUP_MARK_WAIT_STEPS = 60          # 60 * 0.2s = 12s max wait for mark acks
# HANGUP_MIN_GRACE_SECONDS = 1.8       # minimum post-EOC grace even if no marks


# def _looks_like_low_confidence_transcript(transcript: str) -> bool:
#     """Heuristic gate for pickup noise / garbled STT.

#     We only use this as a safety net to avoid hard branch transitions
#     (and especially call-ending branches) on obviously weak transcripts.
#     """
#     text = (transcript or "").strip()
#     if not text:
#         return True

#     words = [w for w in text.split() if w.strip()]
#     if len(words) < MIN_TRANSCRIPT_WORDS_FOR_CLASSIFICATION:
#         return True

#     # If every token is the same (e.g. "प्रस्तित प्रस्तित"), treat as noisy.
#     lowered = [w.lower() for w in words]
#     if len(set(lowered)) == 1:
#         return True

#     return False

# # --- FSM definition -----------------------------------------------------------
# # Each state has:
# #   - prompt_file: which per-state prompt to load
# #   - tools:       which tools Aditi is allowed to invoke in this state
# #   - terminal:    if True, reaching this state ends the call
# #   - auto_advance_to: if set, after Aditi speaks we automatically move back
# #                     to the named state (used for FAQ loopback)
# STATES = {
#     "opening": {
#         "prompt_file": "states/opening",
#         "tools": [
#             "goto_ptp", "goto_cannot_pay", "goto_will_not_pay",
#             "goto_no_loan", "goto_wrong_emi", "goto_already_paid",
#             "answer_faq_who_are_you", "mark_unclear",
#         ],
#         "terminal": False,
#     },
#     "ptp":           {"prompt_file": "states/ptp",            "tools": ["end_call"], "terminal": True},
#     "cannot_pay":    {"prompt_file": "states/cannot_pay",     "tools": ["end_call"], "terminal": True},
#     "will_not_pay":  {"prompt_file": "states/will_not_pay",   "tools": ["end_call"], "terminal": True},
#     "no_loan":       {"prompt_file": "states/no_loan",        "tools": ["end_call"], "terminal": True},
#     "wrong_emi":     {"prompt_file": "states/wrong_emi",      "tools": ["end_call"], "terminal": True},
#     "unclear_close": {"prompt_file": "states/unclear_close",  "tools": ["end_call"], "terminal": True},
#     # --- Already-paid sub-flow: ask date -> ask mode -> thanks -> end ----
#     "already_paid_ask_date": {
#         "prompt_file": "states/already_paid_ask_date",
#         "tools": ["goto_already_paid_mode", "mark_unclear"],
#         "terminal": False,
#     },
#     "already_paid_ask_mode": {
#         "prompt_file": "states/already_paid_ask_mode",
#         "tools": ["goto_already_paid_thanks", "mark_unclear"],
#         "terminal": False,
#     },
#     "already_paid_thanks": {
#         "prompt_file": "states/already_paid_thanks",
#         "tools": ["end_call"],
#         "terminal": True,
#     },
#     # --- Non-terminal interstitials that speak & loop back ---------------
#     "faq_who_are_you": {
#         "prompt_file": "states/faq_who_are_you",
#         "tools": [],
#         "terminal": False,
#         "auto_advance_to": "opening",
#     },
#     "unclear_sm1": {
#         "prompt_file": "states/unclear_sm1",
#         "tools": [],
#         "terminal": False,
#         "auto_advance_to": "opening",
#     },
#     "unclear_sm2": {
#         "prompt_file": "states/unclear_sm2",
#         "tools": [],
#         "terminal": False,
#         "auto_advance_to": "opening",
#     },
# }

# # Every classifier tool takes a single `caller_said` string — the model's own
# # Hindi paraphrase of what the caller just said that led to this branch.
# # Two reasons for this:
# #   1. Realtime API intermittently 500s on tools with empty parameter schemas
# #      — requiring one string param sidesteps that.
# #   2. Gives us an audit trail in logs ("why did the model pick this branch?").
# def _classifier(name, desc):
#     return {
#         "type": "function",
#         "name": name,
#         "description": desc + " Invoke silently — do NOT produce audio in this turn.",
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 "caller_said": {
#                     "type": "string",
#                     "description": (
#                         "Short Hindi paraphrase (5–15 words) of what the "
#                         "caller just said that led to this classification."
#                     ),
#                 }
#             },
#             "required": ["caller_said"],
#         },
#     }

# TOOL_DEFS = {
#     "goto_ptp":               _classifier("goto_ptp",               "Caller promised to pay within 3 days (today/tomorrow/day-after)."),
#     "goto_cannot_pay":        _classifier("goto_cannot_pay",        "Caller cannot pay now / will pay later than 3 days / gave a reason (financial, medical)."),
#     "goto_will_not_pay":      _classifier("goto_will_not_pay",      "Caller flatly refuses without giving a reason."),
#     "goto_no_loan":           _classifier("goto_no_loan",           "Caller denies having a loan / says it is not their account."),
#     "goto_wrong_emi":         _classifier("goto_wrong_emi",         "Caller disputes the EMI amount."),
#     "goto_already_paid":      _classifier("goto_already_paid",      "Caller claims they have already paid this EMI."),
#     "goto_already_paid_mode": _classifier("goto_already_paid_mode", "Caller answered the 'when did you pay' question. Move on to asking the payment mode."),
#     "goto_already_paid_thanks": _classifier("goto_already_paid_thanks", "Caller answered the 'how did you pay' question. Move on to the thank-you close."),
#     "answer_faq_who_are_you": _classifier("answer_faq_who_are_you", "Caller asked who you are / where you are calling from / why."),
#     "mark_unclear":           _classifier("mark_unclear",           "Caller's response was unclear — noise, silence, garbled, or no matching branch. When in doubt, always prefer this over guessing."),
#     "end_call": {
#         "type": "function", "name": "end_call",
#         "description": (
#             "End the phone call. Invoke ONLY after speaking the closing line "
#             "for the current state. Never call this before the closing line, "
#             "and never twice."
#         ),
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 "reason": {
#                     "type": "string",
#                     "description": (
#                         "One of: ptp_secured, cannot_pay, refuse_to_pay, "
#                         "no_loan, dispute, already_paid, no_response."
#                     ),
#                 }
#             },
#             "required": ["reason"],
#         },
#     },
# }

# def build_session_instructions(state_name):
#     """Persona (always) + current state prompt (changes per transition)."""
#     state_prompt = load_prompt(STATES[state_name]["prompt_file"])
#     return f"{PERSONA}\n\n---\n\n{state_prompt}"

# def build_session_tools(state_name):
#     """OpenAI tool definitions for the tools allowed in this state."""
#     return [TOOL_DEFS[t] for t in STATES[state_name]["tools"]]

# app = FastAPI()

# if not OPENAI_API_KEY:
#     raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')
# if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_PHONE_NUMBER:
#     raise ValueError('Missing Twilio configuration. Please set it in the .env file.')

# # --- HTTP endpoints -----------------------------------------------------------
# @app.get("/", response_class=HTMLResponse)
# async def index_page():
#     return {"message": "Twilio Media Stream Server is running!"}

# @app.post("/make-call")
# async def make_call(request: Request):
#     """Make an outgoing call to the specified phone number."""
#     data = await request.json()
#     to_phone_number = data.get("to")
#     if not to_phone_number:
#         return {"error": "Phone number is required"}

#     client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
#     call = client.calls.create(
#         url=f"{NGROK_URL}/outgoing-call",
#         to=to_phone_number,
#         from_=TWILIO_PHONE_NUMBER,
#     )
#     return {"call_sid": call.sid}

# @app.api_route("/outgoing-call", methods=["GET", "POST"])
# async def handle_outgoing_call(request: Request):
#     """Return TwiML that connects the live call to our media stream WebSocket."""
#     response = VoiceResponse()
#     response.pause(length=1)
#     connect = Connect()
#     connect.stream(url=f'wss://{request.url.hostname}/media-stream')
#     response.append(connect)
#     return HTMLResponse(content=str(response), media_type="application/xml")

# # --- Media stream bridge ------------------------------------------------------
# @app.websocket("/media-stream")
# async def handle_media_stream(websocket: WebSocket):
#     """Bridge audio between Twilio Media Streams and the OpenAI Realtime API,
#     with barge-in / interruption support."""
#     print("Client connected")
#     await websocket.accept()

#     async with websockets.connect(
#         f'wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}',
#         extra_headers={
#             "Authorization": f"Bearer {OPENAI_API_KEY}",
#             "OpenAI-Beta": "realtime=v1",
#         },
#     ) as openai_ws:
#         # --- FSM state for this call ---------------------------------------
#         # current_state drives which prompt + tools Aditi sees. We swap it
#         # via session.update on every transition.
#         current_state = "opening"
#         unclear_attempts = 0  # incremented each time Aditi calls mark_unclear
#         initial_item_sent = False  # True after the opening line is triggered
#         pending_tool_calls = []   # filled by response.function_call_arguments.done,
#                                   # drained on response.done

#         # --- Call-level state used by both coroutines ----------------------
#         stream_sid                    = None
#         call_sid                      = None     # Twilio Call SID, needed to hang up via REST
#         latest_media_timestamp        = 0        # last ts seen from Twilio (ms)
#         last_assistant_item           = None     # id of the item the bot is currently speaking
#         mark_queue                    = []       # Twilio "mark" events in flight
#         response_start_timestamp_twilio = None   # Twilio ts when current bot utterance began
#         end_call_requested            = False    # set True once Aditi calls end_call tool
#         last_caller_transcript        = ""       # latest Whisper transcript for classification sanity checks

#         # Initialize session in OPENING state (with opening-state tools).
#         await send_session_update(openai_ws, current_state)

#         async def receive_from_twilio():
#             """Twilio -> OpenAI: forward inbound caller audio."""
#             nonlocal stream_sid, call_sid, latest_media_timestamp
#             nonlocal response_start_timestamp_twilio, last_assistant_item
#             try:
#                 async for message in websocket.iter_text():
#                     data = json.loads(message)
#                     evt = data.get('event')
#                     if evt == 'media':
#                         # If we've already decided to hang up, stop forwarding
#                         # audio — the OpenAI socket may be mid-close.
#                         if end_call_requested:
#                             continue
#                         latest_media_timestamp = int(data['media']['timestamp'])
#                         audio_append = {
#                             "type": "input_audio_buffer.append",
#                             "audio": data['media']['payload'],
#                         }
#                         try:
#                             await openai_ws.send(json.dumps(audio_append))
#                         except ConnectionClosedOK:
#                             # Normal closure (code 1000) — peer already hung
#                             # up cleanly. Nothing to do, just stop forwarding.
#                             break
#                         except ConnectionClosed as e:
#                             # Abnormal but not fatal — log once and stop.
#                             print(f"[info] OpenAI socket closed while "
#                                   f"forwarding audio: {e}")
#                             break
#                         except Exception as e:
#                             print(f"Error forwarding audio to OpenAI: {e}")
#                             break
#                     elif evt == 'start':
#                         stream_sid = data['start']['streamSid']
#                         call_sid = data['start'].get('callSid')
#                         print(f"Incoming stream has started {stream_sid} (callSid={call_sid})")
#                         response_start_timestamp_twilio = None
#                         latest_media_timestamp = 0
#                         last_assistant_item = None
#                     elif evt == 'mark':
#                         if mark_queue:
#                             mark_queue.pop(0)
#             except WebSocketDisconnect:
#                 print("Client disconnected.")
#             finally:
#                 try:
#                     await openai_ws.close()
#                 except Exception:
#                     pass

#         async def send_to_twilio():
#             """OpenAI -> Twilio: forward bot audio and handle barge-in."""
#             nonlocal stream_sid, last_assistant_item
#             nonlocal response_start_timestamp_twilio, end_call_requested
#             nonlocal current_state, unclear_attempts, initial_item_sent
#             nonlocal last_caller_transcript
#             turn_counter = 0
#             try:
#                 async for openai_message in openai_ws:
#                     response = json.loads(openai_message)
#                     rtype = response.get('type')

#                     # ---- Structured logging for the events we care about ---
#                     # Caller speech boundaries (server VAD)
#                     if rtype == 'input_audio_buffer.speech_started':
#                         print("\n[CALLER] 🎤 speech started")
#                     elif rtype == 'input_audio_buffer.speech_stopped':
#                         print("[CALLER] 🎤 speech stopped")
#                     elif rtype == 'input_audio_buffer.committed':
#                         print(f"[CALLER] 🎤 committed audio, item_id={response.get('item_id')}")

#                     # Whisper transcript of what the caller just said
#                     elif rtype == 'conversation.item.input_audio_transcription.completed':
#                         transcript = response.get('transcript', '').strip()
#                         last_caller_transcript = transcript
#                         print(f"[CALLER -> STT] \"{transcript}\"")
#                     elif rtype == 'conversation.item.input_audio_transcription.failed':
#                         print(f"[CALLER -> STT] ❌ transcription failed: "
#                               f"{response.get('error')}")

#                     # Aditi's text (the transcript that matches the audio she
#                     # is currently speaking). Arrives incrementally, but we
#                     # only print when complete so the log stays clean.
#                     elif rtype == 'response.audio_transcript.done':
#                         print(f"[ADITI  -> TTS] \"{response.get('transcript', '').strip()}\"")

#                     # Response wrapper — contains token usage + final status
#                     elif rtype == 'response.done':
#                         log_response_done(response, turn_counter)
#                         turn_counter += 1

#                     elif rtype == 'error':
#                         print("[ERROR] OpenAI error event:")
#                         print(json.dumps(response, indent=2, ensure_ascii=False))

#                     elif rtype == 'session.created':
#                         sess = response.get('session', {})
#                         print(f"[SESSION] created id={sess.get('id')} "
#                               f"model={sess.get('model')}")
#                     elif rtype == 'session.updated':
#                         print(f"[SESSION] updated (state={current_state})")
#                     elif rtype == 'rate_limits.updated':
#                         # Keep this terse — one line with remaining budget.
#                         for rl in response.get('rate_limits', []):
#                             print(f"[RATE_LIMIT] {rl.get('name')}: "
#                                   f"{rl.get('remaining')}/{rl.get('limit')} "
#                                   f"(reset {rl.get('reset_seconds')}s)")

#                     if rtype == 'session.updated' and not initial_item_sent:
#                         # First session.updated of the call — this is when we
#                         # trigger Aditi to speak the opening line. Subsequent
#                         # session.updated events come from FSM transitions and
#                         # must NOT re-trigger the opener.
#                         initial_item_sent = True
#                         await send_initial_conversation_item(openai_ws)

#                     # When the bot has finished its turn on OpenAI's side, clear
#                     # the barge-in bookkeeping. Otherwise a caller who speaks
#                     # later will trigger a truncate on a completed item, which
#                     # fails with `Audio content of Xms is already shorter than Yms`.
#                     if rtype == 'response.done':
#                         last_assistant_item = None
#                         response_start_timestamp_twilio = None
#                         status = (response.get('response') or {}).get('status')
#                         transition_happened = False

#                         # 1) Execute any tool calls we buffered during this
#                         #    response. Only runs on completed responses —
#                         #    cancelled/failed responses drop their buffered
#                         #    calls (they weren't real user turns).
#                         if pending_tool_calls and status == 'completed':
#                             transition_happened = await execute_tool_calls(pending_tool_calls)
#                             pending_tool_calls.clear()
#                         elif pending_tool_calls:
#                             print(f"[TOOL] dropping {len(pending_tool_calls)} "
#                                   f"buffered call(s) (response {status})")
#                             pending_tool_calls.clear()

#                         # 2) Auto-advance: non-terminal speaking states (FAQ
#                         #    answer, SM1/SM2) should drop back to OPENING
#                         #    AFTER Aditi finishes speaking.
#                         # If we already transitioned in this same response.done
#                         # (due to a tool call), skip post-response state logic
#                         # to avoid immediate double-transition / premature hangup.
#                         state_def = STATES.get(current_state, {})
#                         next_state = state_def.get("auto_advance_to")
#                         if (not transition_happened) and next_state and status == 'completed':
#                             print(f"[STATE] auto-advance {current_state} -> {next_state}")
#                             current_state = next_state
#                             await openai_ws.send(json.dumps({
#                                 "type": "session.update",
#                                 "session": {
#                                     "instructions": build_session_instructions(next_state),
#                                     "tools":        build_session_tools(next_state),
#                                     "tool_choice":  "auto",
#                                 },
#                             }))

#                         # If current state is terminal and the model didn't
#                         # call end_call, still close the call after the line.
#                         if (
#                             not transition_happened
#                             and
#                             status == 'completed'
#                             and STATES.get(current_state, {}).get("terminal")
#                             and not end_call_requested
#                         ):
#                             end_call_requested = True
#                             print(f"[end_call] auto terminal hangup in state={current_state}")
#                             asyncio.create_task(graceful_hangup())

#                     if rtype == 'response.audio.delta' and response.get('delta'):
#                         audio_delta = {
#                             "event": "media",
#                             "streamSid": stream_sid,
#                             "media": {"payload": response['delta']},
#                         }
#                         await websocket.send_json(audio_delta)

#                         # Remember when this bot utterance began, in Twilio's
#                         # timebase. Needed to compute how much of the reply
#                         # was actually played when user barges in.
#                         if response_start_timestamp_twilio is None:
#                             response_start_timestamp_twilio = latest_media_timestamp
#                             if SHOW_TIMING_MATH:
#                                 print(f"Response start ts: {response_start_timestamp_twilio}ms")

#                         if response.get('item_id'):
#                             last_assistant_item = response['item_id']

#                         await send_mark(websocket, stream_sid)

#                     # ---- BARGE-IN -------------------------------------------
#                     # Server-side VAD detected the caller starting to speak.
#                     # Only interrupt if the bot is actually mid-utterance
#                     # (last_assistant_item + a response-start timestamp + audio
#                     # still buffered inside Twilio). Otherwise this is just a
#                     # normal turn on the caller's side and there is nothing to
#                     # truncate.
#                     if rtype == 'input_audio_buffer.speech_started':
#                         if (last_assistant_item
#                                 and response_start_timestamp_twilio is not None
#                                 and mark_queue):
#                             print("Caller started speaking -> interrupting bot")
#                             await handle_speech_started_event()

#                     # ---- FSM TOOL BUFFER ----------------------------------
#                     # Tool-call args arrive while the response is STILL
#                     # active. Executing a new response.create here would
#                     # cause a server_error. So we just buffer the call and
#                     # act on it in the `response.done` branch below.
#                     if rtype == 'response.function_call_arguments.done':
#                         fname = response.get('name') or ''
#                         call_id = response.get('call_id')
#                         try:
#                             args = json.loads(response.get('arguments') or '{}')
#                         except Exception:
#                             args = {}
#                         print(f"[TOOL] {fname}({json.dumps(args, ensure_ascii=False)})")
#                         pending_tool_calls.append({
#                             "name": fname,
#                             "call_id": call_id,
#                             "args": args,
#                         })
#             except Exception as e:
#                 print(f"Error in send_to_twilio: {e}")

#         async def transition_to(new_state):
#             """FSM edge. Swap session instructions + tools to the new state,
#             then trigger a fresh response.create so Aditi speaks that state's
#             line."""
#             nonlocal current_state
#             old_state = current_state
#             current_state = new_state
#             print(f"[STATE] {old_state} -> {new_state}")

#             await openai_ws.send(json.dumps({
#                 "type": "session.update",
#                 "session": {
#                     "instructions": build_session_instructions(new_state),
#                     "tools":        build_session_tools(new_state),
#                     "tool_choice":  "auto",
#                 },
#             }))
#             # Ask the model to produce the next response now.
#             await openai_ws.send(json.dumps({"type": "response.create"}))

#         async def execute_tool_calls(calls):
#             """Drain the tool-call buffer. Called from response.done so we
#             are sure the previous response has fully completed — it's safe
#             now to send function_call_output + session.update + response.create
#             without tripping a server_error."""
#             nonlocal unclear_attempts, end_call_requested, last_caller_transcript

#             # 1) ACK every tool call with a function_call_output. Required
#             #    by OpenAI — otherwise subsequent response.create fails.
#             for c in calls:
#                 if c.get("call_id"):
#                     await openai_ws.send(json.dumps({
#                         "type": "conversation.item.create",
#                         "item": {
#                             "type": "function_call_output",
#                             "call_id": c["call_id"],
#                             "output": json.dumps({"ok": True}),
#                         },
#                     }))

#             # 2) Act on the FIRST tool call only. (The model rarely emits
#             #    more than one per response; if it does, the first wins.)
#             first = calls[0]
#             fname = first["name"]
#             caller_said = (first.get("args") or {}).get("caller_said", "")

#             # Safety net: if STT looks weak/noisy, do not trust semantic
#             # classification branches. Route to unclear retry instead.
#             if (
#                 fname in TOOL_DEFS
#                 and fname != "mark_unclear"
#                 and fname != "end_call"
#                 and _looks_like_low_confidence_transcript(caller_said)
#                 and _looks_like_low_confidence_transcript(last_caller_transcript)
#             ):
#                 print(
#                     "[TOOL] low-confidence transcript; overriding "
#                     f"{fname} -> mark_unclear (stt='{last_caller_transcript}')"
#                 )
#                 fname = "mark_unclear"

#             transition_map = {
#                 "goto_ptp":                 "ptp",
#                 "goto_cannot_pay":          "cannot_pay",
#                 "goto_will_not_pay":        "will_not_pay",
#                 "goto_no_loan":             "no_loan",
#                 "goto_wrong_emi":           "wrong_emi",
#                 "goto_already_paid":        "already_paid_ask_date",
#                 "goto_already_paid_mode":   "already_paid_ask_mode",
#                 "goto_already_paid_thanks": "already_paid_thanks",
#                 "answer_faq_who_are_you":   "faq_who_are_you",
#             }
#             if fname in transition_map:
#                 await transition_to(transition_map[fname])
#                 return True

#             elif fname == "mark_unclear":
#                 # User requirement: close call on every intent path.
#                 # For unclear/noise, directly route to close line.
#                 unclear_attempts += 1
#                 await transition_to("unclear_close")
#                 return True

#             elif fname == "end_call" and not end_call_requested:
#                 end_call_requested = True
#                 reason = first["args"].get("reason", "unspecified")
#                 print(f"[end_call] reason={reason}  terminal_state={current_state}")
#                 asyncio.create_task(graceful_hangup())
#                 return True

#             return False

#         async def graceful_hangup():
#             """Wait for Aditi's EOC line to finish reaching the caller, then
#             terminate the Twilio call via REST and close both sockets."""
#             started_wait = time.monotonic()
#             # Wait for Twilio mark acks so we know bot audio chunks were rendered.
#             # Keep waiting up to ~12s for slow mobile networks.
#             for _ in range(HANGUP_MARK_WAIT_STEPS):
#                 if not mark_queue:
#                     break
#                 await asyncio.sleep(0.2)

#             # Even if marks are already empty/missed, enforce a minimum grace
#             # so callers hear the full final words before REST hangup.
#             elapsed = time.monotonic() - started_wait
#             remaining_grace = max(HANGUP_MIN_GRACE_SECONDS - elapsed, 0.0)
#             if remaining_grace:
#                 await asyncio.sleep(remaining_grace)

#             if call_sid:
#                 try:
#                     Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) \
#                         .calls(call_sid) \
#                         .update(status='completed')
#                     print(f"[end_call] Twilio call {call_sid} terminated.")
#                 except Exception as e:
#                     print(f"[end_call] Failed to hang up via Twilio REST: {e}")
#             else:
#                 print("[end_call] No call_sid recorded, cannot REST-hangup.")

#             # Close sockets so the server coroutines exit cleanly.
#             try:
#                 await openai_ws.close()
#             except Exception:
#                 pass
#             try:
#                 await websocket.close()
#             except Exception:
#                 pass

#         async def handle_speech_started_event():
#             """Truncate the bot's utterance on OpenAI's side AND clear Twilio's
#             audio buffer so the caller is no longer talked over."""
#             nonlocal response_start_timestamp_twilio, last_assistant_item

#             # Wall-clock ms between 'bot started this reply' and 'caller
#             # started speaking'. Clamp to 0 to avoid negative values if
#             # timestamps race.
#             elapsed_time = max(
#                 latest_media_timestamp - (response_start_timestamp_twilio or 0),
#                 0,
#             )

#             if SHOW_TIMING_MATH:
#                 print(
#                     f"Truncating: latest_ts={latest_media_timestamp}, "
#                     f"response_start_ts={response_start_timestamp_twilio}, "
#                     f"elapsed={elapsed_time}ms"
#                 )

#             if last_assistant_item:
#                 truncate_event = {
#                     "type": "conversation.item.truncate",
#                     "item_id": last_assistant_item,
#                     "content_index": 0,
#                     "audio_end_ms": elapsed_time,
#                 }
#                 try:
#                     await openai_ws.send(json.dumps(truncate_event))
#                 except Exception as e:
#                     # Truncate can fail with "audio shorter than Xms" if the
#                     # model finished the item between our check and this
#                     # send. That's harmless — just log and continue clearing
#                     # Twilio's buffer so the caller hears silence.
#                     print(f"Error sending truncate (ignored): {e}")

#             # Flush any bot audio still queued inside Twilio
#             try:
#                 await websocket.send_json({
#                     "event": "clear",
#                     "streamSid": stream_sid,
#                 })
#             except Exception as e:
#                 print(f"Error sending Twilio clear: {e}")

#             mark_queue.clear()
#             last_assistant_item = None
#             response_start_timestamp_twilio = None

#         async def send_mark(connection, sid):
#             """Send a Twilio 'mark' event so we can later track how much of a
#             bot utterance has actually been rendered into the caller's ear."""
#             if sid:
#                 mark_event = {
#                     "event": "mark",
#                     "streamSid": sid,
#                     "mark": {"name": "responsePart"},
#                 }
#                 await connection.send_json(mark_event)
#                 mark_queue.append("responsePart")

#         await asyncio.gather(receive_from_twilio(), send_to_twilio())

# # --- OpenAI Realtime helpers --------------------------------------------------
# def log_response_done(event, turn_index):
#     """Pretty-print a `response.done` event: status, any function calls the
#     bot invoked, the final assistant transcript, and token usage JSON."""
#     resp = event.get('response') or {}
#     status = resp.get('status')
#     status_details = resp.get('status_details') or {}
#     usage = resp.get('usage') or {}

#     header = f"\n[ASSISTANT response.done] turn #{turn_index}  status={status}"
#     if status != 'completed' and status_details:
#         header += f"  details={json.dumps(status_details, ensure_ascii=False)}"
#     print(header)

#     # Surface any tool calls (e.g. end_call) so you can see exactly what the
#     # model emitted, even before the dedicated handler acts on it.
#     for item in resp.get('output') or []:
#         if item.get('type') == 'function_call':
#             print(f"  ↪ function_call: {item.get('name')}"
#                   f"({item.get('arguments')})  "
#                   f"call_id={item.get('call_id')}")
#         elif item.get('type') == 'message':
#             # The audio_transcript.done event already prints the transcript,
#             # so we skip it here to avoid duplication.
#             pass

#     # Token breakdown — this is the STT/LLM/TTS-equivalent split.
#     if usage:
#         in_details = usage.get('input_token_details') or {}
#         out_details = usage.get('output_token_details') or {}
#         compact = {
#             "total":      usage.get('total_tokens', 0),
#             "input":      usage.get('input_tokens', 0),
#             "output":     usage.get('output_tokens', 0),
#             "in_audio":   in_details.get('audio_tokens', 0),   # ≈ STT side
#             "in_text":    in_details.get('text_tokens', 0),    # prompt/ctx
#             "in_cached":  in_details.get('cached_tokens', 0),
#             "out_audio":  out_details.get('audio_tokens', 0),  # ≈ TTS side
#             "out_text":   out_details.get('text_tokens', 0),   # LLM text
#         }
#         print("  tokens: " + json.dumps(compact, ensure_ascii=False))

# async def send_session_update(openai_ws, state_name):
#     """Configure (or re-configure) the Realtime session for a given FSM
#     state. Called once at session start with state='opening', and again
#     on each transition via the FSM's transition_to() helper.

#     - g711 µ-law at 8 kHz on both sides (matches Twilio Media Streams natively)
#     - Server-side VAD for turn detection, with interrupt_response=True so the
#       model stops generating the moment the caller starts talking.
#     - Whisper-1 forced to Hindi for caller transcripts (for logs).
#     - instructions = persona + current state's prompt file
#     - tools        = the tools allowed in this state
#     """
#     session_update = {
#         "type": "session.update",
#         "session": {
#             "turn_detection": {
#                 "type": "server_vad",
#                 # Raised from 0.5 → 0.7 so random phone-line noise / throat
#                 # clearing / background chatter doesn't falsely trigger a
#                 # caller "turn". On Indian mobile networks with ambient
#                 # noise this makes a big difference.
#                 "threshold": 0.7,
#                 "prefix_padding_ms": 300,
#                 # Raised from 500 → 800 so the model waits a bit longer
#                 # before deciding the caller has finished.
#                 "silence_duration_ms": 800,
#                 "create_response": True,
#                 "interrupt_response": True,
#             },
#             "input_audio_format": "g711_ulaw",
#             "output_audio_format": "g711_ulaw",
#             "input_audio_transcription": {
#                 "model": "whisper-1",
#                 "language": "hi",
#             },
#             # Reduce stationary/background call noise before VAD + STT.
#             "input_audio_noise_reduction": {
#                 "type": "near_field",
#             },
#             "voice": VOICE,
#             "instructions": build_session_instructions(state_name),
#             "modalities": ["text", "audio"],
#             # 0.6 is the minimum the Realtime API accepts. Keeping it at
#             # the floor makes Aditi stick tightly to the pinned lines in
#             # each state's prompt.
#             "temperature": 0.6,
#             "tools":       build_session_tools(state_name),
#             "tool_choice": "auto",
#         },
#     }
#     print(f"[SESSION] configuring for state='{state_name}'")
#     await openai_ws.send(json.dumps(session_update))

# async def send_initial_conversation_item(openai_ws):
#     """Trigger Aditi to speak the opening line right after pickup.
#     Sent once, right after the initial session.updated event."""
#     initial_conversation_item = {
#         "type": "conversation.item.create",
#         "item": {
#             "type": "message",
#             "role": "user",
#             "content": [
#                 {
#                     "type": "input_text",
#                     "text": (
#                         "ग्राहक ने अभी-अभी फ़ोन उठाया है। "
#                         "अपने OPENING state निर्देशों में दिए गए वाक्य के "
#                         "अनुसार ठीक हिंदी में उनका अभिवादन करें। "
#                         "पूरी बातचीत केवल हिंदी में होगी।"
#                     ),
#                 }
#             ],
#         },
#     }
#     await openai_ws.send(json.dumps(initial_conversation_item))
#     await openai_ws.send(json.dumps({"type": "response.create"}))

# # --- CLI / entrypoint ---------------------------------------------------------
# def place_outbound_call(to_phone_number: str):
#     client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
#     call = client.calls.create(
#         url=f"{NGROK_URL}/outgoing-call",
#         to=to_phone_number,
#         from_=TWILIO_PHONE_NUMBER,
#     )
#     print(f"Call initiated with SID: {call.sid}")
#     return call.sid

# if __name__ == "__main__":
#     import sys
#     import threading
#     import uvicorn

#     # Precedence: CLI arg > CALL_TO env var > DEFAULT_CALL_TO constant.
#     # So `python main.py` just calls the hardcoded default, and
#     # `python main.py +919999999999` dials a different number for one run.
#     if len(sys.argv) > 1 and sys.argv[1].strip():
#         to_phone_number = sys.argv[1].strip()
#     else:
#         to_phone_number = DEFAULT_CALL_TO

#     print(f"Dialing {to_phone_number} …")

#     # Start uvicorn first, then place the call a moment later so that Twilio's
#     # webhook to /outgoing-call hits a running server.
#     def _delayed_call():
#         time.sleep(2)
#         try:
#             place_outbound_call(to_phone_number)
#         except Exception as e:
#             print(f"Error initiating call: {e}")

#     threading.Thread(target=_delayed_call, daemon=True).start()
#     uvicorn.run(app, host="0.0.0.0", port=PORT)

from __future__ import annotations

import time
import os
import json
import asyncio
import base64
import httpx
import websockets
import re
from websockets.exceptions import ConnectionClosedOK, ConnectionClosed
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Prompt loader
# ---------------------------------------------------------------------------
def load_prompt(relative_path):
    dir_path = os.path.dirname(os.path.realpath(__file__))
    prompt_path = os.path.join(dir_path, 'prompts', f'{relative_path}.txt')
    try:
        with open(prompt_path, 'r', encoding='utf-8') as file:
            return file.read().strip()
    except FileNotFoundError:
        print(f"Could not find file: {prompt_path}")
        raise

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OPENAI_API_KEY      = os.getenv('OPENAI_API_KEY')
DEEPGRAM_API_KEY    = os.getenv('DEEPGRAM_API_KEY')
SARVAM_API_KEY      = os.getenv('SARVAM_API_KEY')
TWILIO_ACCOUNT_SID  = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN   = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER')
NGROK_URL           = os.getenv('NGROK_URL')
PORT                = int(os.getenv('PORT', 5050))
DEFAULT_CALL_TO     = os.getenv('CALL_TO', '+917977365303')

LLM_MODEL = 'gpt-4.1-mini'

# Deepgram STT — mulaw 8kHz direct (no transcoding needed)
DEEPGRAM_WS_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model=nova-3"
    "&language=multi"
    "&encoding=mulaw"
    "&sample_rate=8000"
    "&channels=1"
    "&punctuate=true"
    "&interim_results=true"
    "&endpointing=200"
    "&vad_events=true"
    "&smart_format=true"
    "&no_delay=true"
)

# Sarvam TTS — Bulbul v3, mulaw 8kHz output (Twilio-native)
SARVAM_TTS_URL  = "https://api.sarvam.ai/text-to-speech"
SARVAM_VOICE    = os.getenv('SARVAM_VOICE', 'priya')
SARVAM_LANGUAGE = "hi-IN"

MIN_TRANSCRIPT_WORDS = 2
HANGUP_GRACE_SEC     = 1.5

# ---------------------------------------------------------------------------
# Persona & FSM
# ---------------------------------------------------------------------------
PERSONA = load_prompt('persona')

STATES = {
    "opening": {
        "prompt_file": "states/opening",
        "tools": [
            "goto_ptp","goto_cannot_pay","goto_will_not_pay",
            "goto_no_loan","goto_wrong_emi","goto_already_paid",
            "answer_faq_who_are_you","mark_unclear",
        ],
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

def _classifier(name, desc):
    return {"type":"function","function":{
        "name":name,
        "description":desc+" Invoke silently — do NOT produce audio in this turn.",
        "parameters":{"type":"object","properties":{
            "caller_said":{"type":"string","description":"Short Hindi paraphrase (5-15 words)."}
        },"required":["caller_said"]},
    }}

TOOL_DEFS = {
    "goto_ptp":               _classifier("goto_ptp","Caller promised to pay within 3 days."),
    "goto_cannot_pay":        _classifier("goto_cannot_pay","Caller cannot pay now or needs >3 days."),
    "goto_will_not_pay":      _classifier("goto_will_not_pay","Caller flatly refuses without reason."),
    "goto_no_loan":           _classifier("goto_no_loan","Caller denies having this loan."),
    "goto_wrong_emi":         _classifier("goto_wrong_emi","Caller disputes the EMI amount."),
    "goto_already_paid":      _classifier("goto_already_paid","Caller says EMI already paid."),
    "goto_already_paid_mode": _classifier("goto_already_paid_mode","Caller answered when they paid."),
    "goto_already_paid_thanks":_classifier("goto_already_paid_thanks","Caller answered how they paid."),
    "answer_faq_who_are_you": _classifier("answer_faq_who_are_you","Caller asked who you are or why calling."),
    "mark_unclear":           _classifier("mark_unclear","Response was unclear, noise, or silence."),
    "end_call": {"type":"function","function":{
        "name":"end_call",
        "description":"End the call. Only after speaking closing line.",
        "parameters":{"type":"object","properties":{
            "reason":{"type":"string","description":"ptp_secured|cannot_pay|refuse_to_pay|no_loan|dispute|already_paid|no_response"}
        },"required":["reason"]},
    }},
}

TRANSITION_MAP = {
    "goto_ptp":"ptp","goto_cannot_pay":"cannot_pay","goto_will_not_pay":"will_not_pay",
    "goto_no_loan":"no_loan","goto_wrong_emi":"wrong_emi",
    "goto_already_paid":"already_paid_ask_date",
    "goto_already_paid_mode":"already_paid_ask_mode",
    "goto_already_paid_thanks":"already_paid_thanks",
    "answer_faq_who_are_you":"faq_who_are_you",
}

def build_system_prompt(state_name):
    return f"{PERSONA}\n\n---\n\n{load_prompt(STATES[state_name]['prompt_file'])}"

def get_tools_for_state(state_name):
    return [TOOL_DEFS[t] for t in STATES[state_name]["tools"]]

def _low_confidence(t: str) -> bool:
    words = [w for w in (t or "").split() if w.strip()]
    if len(words) < MIN_TRANSCRIPT_WORDS:
        return True
    return len(set(w.lower() for w in words)) == 1

def get_state_spoken_line(state_name: str) -> str:
    try:
        text = load_prompt(STATES[state_name]["prompt_file"])
    except Exception:
        return ""
    m = re.search(r'"([^"]+)"', text, flags=re.DOTALL)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""

# ---------------------------------------------------------------------------
# Sarvam TTS
# ---------------------------------------------------------------------------
sarvam_http = httpx.AsyncClient(timeout=12.0)

async def sarvam_tts_bytes(text: str) -> bytes:
    resp = await sarvam_http.post(
        SARVAM_TTS_URL,
        json={
            "text": text,
            "target_language_code": SARVAM_LANGUAGE,
            "speaker": SARVAM_VOICE,
            "model": "bulbul:v3",
            "speech_sample_rate": 8000,
            "enable_preprocessing": True,
            "output_audio_codec": "mulaw",
        },
        headers={"api-subscription-key": SARVAM_API_KEY, "Content-Type": "application/json"},
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Sarvam TTS {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    b64 = (data.get("audios") or [None])[0] or data.get("audio")
    if not b64:
        raise RuntimeError(f"No audio in Sarvam response: {str(data)[:200]}")
    return base64.b64decode(b64)

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

async def call_llm(history, state_name, allow_tools=True):
    tools = get_tools_for_state(state_name) if allow_tools else []
    kwargs = {
        "model": LLM_MODEL,
        "messages": [{"role":"system","content":build_system_prompt(state_name)}] + history,
        "temperature": 0.3,
        "max_tokens": 90,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    try:
        r = await openai_client.chat.completions.create(**kwargs)
        msg = r.choices[0].message
        if msg.tool_calls:
            tc = msg.tool_calls[0]
            return None, tc.function.name, tc.function.arguments
        return msg.content, None, None
    except Exception as e:
        print(f"[LLM ERROR] {e}")
        return None, None, None

# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
app = FastAPI()

for v, n in [
    (OPENAI_API_KEY,"OPENAI_API_KEY"),(DEEPGRAM_API_KEY,"DEEPGRAM_API_KEY"),
    (SARVAM_API_KEY,"SARVAM_API_KEY"),(TWILIO_ACCOUNT_SID,"TWILIO_ACCOUNT_SID"),
    (TWILIO_AUTH_TOKEN,"TWILIO_AUTH_TOKEN"),(TWILIO_PHONE_NUMBER,"TWILIO_PHONE_NUMBER"),
]:
    if not v:
        raise ValueError(f"Missing {n} in .env")

@app.get("/",response_class=HTMLResponse)
async def index():
    return {"message":"Aditi — Deepgram STT + gpt-4.1-mini LLM + Sarvam TTS"}

@app.post("/make-call")
async def make_call(request: Request):
    data = await request.json()
    to = data.get("to")
    if not to:
        return {"error":"Phone number required"}
    call = Client(TWILIO_ACCOUNT_SID,TWILIO_AUTH_TOKEN).calls.create(
        url=f"{NGROK_URL}/outgoing-call", to=to, from_=TWILIO_PHONE_NUMBER)
    return {"call_sid":call.sid}

@app.api_route("/outgoing-call",methods=["GET","POST"])
async def outgoing_call(request: Request):
    r = VoiceResponse()
    r.pause(length=1)
    c = Connect()
    c.stream(url=f'wss://{request.url.hostname}/media-stream')
    r.append(c)
    return HTMLResponse(content=str(r), media_type="application/xml")

# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------
@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    print("Client connected")
    await websocket.accept()

    # All shared state in mutable containers so nested async fns see updates
    ctx = {
        "stream_sid":    None,
        "call_sid":      None,
        "state":         "opening",
        "history":       [],
        "done":          False,   # ← THE key flag: True = stop everything NOW
        "speaking":      False,
        "last_final":    "",
        "last_interim":  "",
        "unclear":       0,
    }
    q: asyncio.Queue = asyncio.Queue()

    dg_hdrs = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}

    async with websockets.connect(DEEPGRAM_WS_URL, extra_headers=dg_hdrs) as dg_ws:

        # ── speak helper ─────────────────────────────────────────────────────
        async def speak(text: str) -> None:
            if ctx["done"] or not ctx["stream_sid"] or not text:
                return
            print(f'[ADITI] "{text.strip()}"')
            ctx["speaking"] = True
            try:
                audio = await sarvam_tts_bytes(text.strip())
                for i in range(0, len(audio), 160):
                    if ctx["done"]:
                        return
                    await websocket.send_json({
                        "event":"media","streamSid":ctx["stream_sid"],
                        "media":{"payload":base64.b64encode(audio[i:i+160]).decode()},
                    })
                if not ctx["done"]:
                    await websocket.send_json({
                        "event":"mark","streamSid":ctx["stream_sid"],
                        "mark":{"name":"tts_done"},
                    })
            except Exception as e:
                if not ctx["done"]:
                    print(f"[TTS ERROR] {e}")
            finally:
                ctx["speaking"] = False

        async def speak_state(state_name: str) -> None:
            text = get_state_spoken_line(state_name)
            if not text:
                text, _, _ = await call_llm([], state_name, allow_tools=False)
            if text:
                ctx["history"].append({"role":"assistant","content":text})
                await speak(text)

        # ── hangup ───────────────────────────────────────────────────────────
        async def hangup(reason: str = "unspecified") -> None:
            if ctx["done"]:
                return
            print(f"[HANGUP] reason={reason}")
            ctx["done"] = True          # ← set FIRST so no more TTS/sends happen
            await asyncio.sleep(HANGUP_GRACE_SEC)
            if ctx["call_sid"]:
                try:
                    Client(TWILIO_ACCOUNT_SID,TWILIO_AUTH_TOKEN)\
                        .calls(ctx["call_sid"]).update(status="completed")
                    print(f"[HANGUP] call {ctx['call_sid']} terminated")
                except Exception as e:
                    print(f"[HANGUP] REST failed: {e}")
            for ws in (dg_ws, websocket):
                try:
                    await ws.close()
                except Exception:
                    pass

        # ── opening ──────────────────────────────────────────────────────────
        async def do_opening():
            text = get_state_spoken_line("opening")
            if not text:
                seed = {"role":"user","content":
                    "ग्राहक ने फ़ोन उठाया है। opening state अनुसार हिंदी में अभिवादन करें।"}
                text, _, _ = await call_llm([seed], "opening", allow_tools=False)
            if text and not ctx["done"]:
                ctx["history"].append({"role":"assistant","content":text})
                await speak(text)

        # ── Twilio → Deepgram ─────────────────────────────────────────────────
        async def recv_twilio():
            opened = False
            try:
                async for msg in websocket.iter_text():
                    if ctx["done"]:
                        break
                    data = json.loads(msg)
                    evt  = data.get("event")

                    if evt == "start":
                        ctx["stream_sid"] = data["start"]["streamSid"]
                        ctx["call_sid"]   = data["start"].get("callSid")
                        print(f"[STREAM] {ctx['stream_sid']} call={ctx['call_sid']}")
                        if not opened:
                            opened = True
                            asyncio.create_task(do_opening())

                    elif evt == "media":
                        if ctx["done"]:
                            continue
                        if ctx["speaking"]:
                            try:
                                await websocket.send_json(
                                    {"event":"clear","streamSid":ctx["stream_sid"]})
                            except Exception:
                                pass
                            ctx["speaking"] = False
                        raw = base64.b64decode(data["media"]["payload"])
                        try:
                            await dg_ws.send(raw)
                        except (ConnectionClosedOK, ConnectionClosed):
                            break
                        except Exception as e:
                            print(f"[DG send] {e}")
                            break

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

        # ── Deepgram → queue ──────────────────────────────────────────────────
        async def recv_deepgram():
            try:
                async for msg in dg_ws:
                    if ctx["done"]:
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
                        final = bool(data.get("is_final") or data.get("speech_final"))
                        if final:
                            # Drop STT finals that arrive while bot audio is still playing
                            # (common echo/loopback artifact on PSTN calls).
                            if ctx["speaking"]:
                                continue
                            print(f'[STT final] "{tr}"')
                            ctx["last_final"]   = tr
                            ctx["last_interim"] = ""
                            if not ctx["done"]:
                                await q.put(tr)
                        elif tr != ctx["last_interim"]:
                            print(f'[STT interim] "{tr}"')
                            ctx["last_interim"] = tr
                    elif t == "SpeechStarted":
                        print("[🎤]")
                    elif t == "Error":
                        print(f"[DG ERROR] {data}")
            except Exception as e:
                if not ctx["done"]:
                    print(f"[DG recv] {e}")

        # ── FSM loop ──────────────────────────────────────────────────────────
        async def fsm_loop():

            async def handle_unclear():
                """Increment unclear counter, speak retry, return True if we should exit."""
                # In already-paid subflow, unclear should re-ask the SAME question,
                # not jump to generic opening retries.
                if ctx["state"] in ("already_paid_ask_date", "already_paid_ask_mode"):
                    ctx["unclear"] += 1
                    print(f"[FSM] unclear in {ctx['state']} (attempt {ctx['unclear']}) → repeat same state")
                    await speak_state(ctx["state"])
                    if ctx["unclear"] >= 3:
                        ctx["state"] = "unclear_close"
                        await speak_state("unclear_close")
                        asyncio.create_task(hangup("no_response"))
                        return True
                    return False

                ctx["unclear"] += 1
                if ctx["unclear"] == 1:
                    tgt = "unclear_sm1"
                elif ctx["unclear"] == 2:
                    tgt = "unclear_sm2"
                else:
                    tgt = "unclear_close"
                ctx["state"] = tgt
                print(f"[FSM] unclear {ctx['unclear']} → {tgt}")
                await speak_state(tgt)
                if tgt == "unclear_close":
                    asyncio.create_task(hangup("no_response"))
                    return True          # signal: exit loop
                nxt = STATES[tgt].get("auto_advance_to")
                if nxt:
                    ctx["state"] = nxt
                return False

            while not ctx["done"]:

                # Wait for utterance, with silence timeout
                try:
                    tr = await asyncio.wait_for(q.get(), timeout=25.0)
                except asyncio.TimeoutError:
                    if ctx["done"]:
                        break
                    print("[FSM] 25s silence → unclear")
                    should_exit = await handle_unclear()
                    if should_exit:
                        break
                    continue

                if ctx["done"]:
                    break

                tr = tr.strip() or "[silence]"
                ctx["history"].append({"role":"user","content":tr})

                text, tool, args_raw = await call_llm(ctx["history"], ctx["state"])

                if ctx["done"]:
                    break

                # Low confidence override
                if (tool and tool not in ("mark_unclear","end_call")
                        and _low_confidence(tr)
                        and _low_confidence(ctx["last_final"])):
                    print(f"[FSM] low-confidence → mark_unclear (was {tool})")
                    tool     = "mark_unclear"
                    args_raw = json.dumps({"caller_said":tr})

                if tool:
                    print(f"[TOOL] {tool}({args_raw})")
                    try:
                        args = json.loads(args_raw or "{}")
                    except Exception:
                        args = {}

                    if tool in TRANSITION_MAP:
                        new_state = TRANSITION_MAP[tool]
                        print(f"[FSM] {ctx['state']} → {new_state}")
                        ctx["state"] = new_state
                        await speak_state(new_state)
                        if ctx["done"]:
                            break
                        if STATES[new_state].get("terminal"):
                            asyncio.create_task(hangup(args.get("reason", new_state)))
                            break                  # EXIT
                        nxt = STATES[new_state].get("auto_advance_to")
                        if nxt:
                            ctx["state"] = nxt

                    elif tool == "mark_unclear":
                        if await handle_unclear():
                            break                  # EXIT

                    elif tool == "end_call":
                        asyncio.create_task(hangup(args.get("reason","unspecified")))
                        break                      # EXIT

                elif text:
                    ctx["history"].append({"role":"assistant","content":text})
                    await speak(text)

            print("[FSM] exited")

        await asyncio.gather(recv_twilio(), recv_deepgram(), fsm_loop())

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def place_outbound_call(to):
    call = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).calls.create(
        url=f"{NGROK_URL}/outgoing-call", to=to, from_=TWILIO_PHONE_NUMBER)
    print(f"Call SID: {call.sid}")
    return call.sid

if __name__ == "__main__":
    import sys, threading, uvicorn
    to = sys.argv[1].strip() if len(sys.argv) > 1 else DEFAULT_CALL_TO
    print(f"Dialing {to} …")
    def _call():
        time.sleep(2)
        try:
            place_outbound_call(to)
        except Exception as e:
            print(f"Call error: {e}")
    threading.Thread(target=_call, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT)
