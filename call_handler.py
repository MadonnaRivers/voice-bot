"""
call_handler.py — Plivo WebSocket media-stream handler + LLM conversation loop.

Receives raw µ-law audio from Plivo, pipes it through:
  denoiser → WebRTC VAD → Sarvam STT (REST, realtime per-utterance)
TTS streams back via Sarvam's chunked /text-to-speech/stream endpoint.
All dialogue logic runs through llm_orchestrator (OpenAI).
"""
from __future__ import annotations

import asyncio
import audioop
import concurrent.futures
import json
import logging
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import webrtcvad
from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect

import carrier as _carrier
from config import (
    VAD_ENABLED, VAD_MODE, VAD_HANGOVER_MS,
    DENOISE_ENABLED, DENOISE_STRENGTH, DENOISE_PROFILE_SEC, DENOISE_STATIONARY,
    HANGUP_GRACE_SEC, SILENCE_TIMEOUT_SEC, BARGE_IN_GUARD_SEC,
    TRANSCRIPTS_DIR, RECORDING_CALLBACK_URL,
    AMD_ENABLED, AMD_WAIT_MS, AMD_LEAVE_MESSAGE, AMD_BEEP_WAIT_MS,
)
from classifier import finalize_call_variables
from call_webhook import build_call_summary_push_body
from denoiser import StreamDenoiser
from llm_orchestrator import stream_conversation_turn, conversation_to_storage_text
from scripts import build_opening_greeting, build_voicemail_message
from utils import ist_now
from session import CallSession, pending_ctx, amd_results, amd_events
from stt import RestStt
from tts import tts_speak

log = logging.getLogger("aditi")

_VAD_HANGOVER_FRAMES = max(1, VAD_HANGOVER_MS // 20)

# Short "thinking" filler — played after each customer reply to cover
# LLM + TTS latency so the customer hears acknowledgement instead of dead air.
_THINKING_FILLER = "ek second please"

# Keys the LLM must never write into session context.
_FORBIDDEN_CTX_KEYS: frozenset[str] = frozenset({
    "silence_count", "error_count", "retry_count", "turn_count",
    "loop_count", "attempt", "state", "call_phase",
})

# CPU-bound denoiser + transcript disk writes run here.
# Sized for 100 concurrent calls: denoise runs on every inbound audio chunk
# (~50/s per call). 64 worker threads keeps p99 dispatch latency low without
# starving the asyncio loop.
_WORKER_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=128, thread_name_prefix="worker")


async def media_stream(ws: WebSocket) -> None:
    await ws.accept()
    log.info("[PLIVO] media stream connected (call answered)")

    from scripts import build_default_ctx
    sess  = CallSession(ctx=build_default_ctx())
    abort = [False]
    utt_q: asyncio.Queue[str] = asyncio.Queue()
    drained = asyncio.Event()
    drained.set()

    _denoiser = (
        StreamDenoiser(
            prop_decrease=DENOISE_STRENGTH,
            profile_sec=DENOISE_PROFILE_SEC,
            stationary=DENOISE_STATIONARY,
        )
        if DENOISE_ENABLED else None
    )
    _loop = asyncio.get_event_loop()
    _vad_inst = webrtcvad.Vad(VAD_MODE) if VAD_ENABLED else None
    _vad_hangover_left = [0]

    try:
        # ── Transcript JSONL writer (off-thread) ────────────────────────────
        def _write_line(path: str, line: str) -> None:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

        def record(event: str, **fields: Any) -> None:
            if not sess.transcript_path:
                ts  = ist_now().strftime("%Y%m%d_%H%M%S_%f")
                sid = sess.call_sid.replace("/", "_") or "unknown"
                Path(TRANSCRIPTS_DIR).mkdir(parents=True, exist_ok=True)
                sess.transcript_path = f"{TRANSCRIPTS_DIR}/{ts}_{sid}.jsonl"
                if sess.call_sid:
                    from session import recording_pending
                    recording_pending[sess.call_sid] = sess.transcript_path
            row = {
                "ts":    ist_now().isoformat(timespec="milliseconds"),
                "event": event, "state": sess.state, "sid": sess.call_sid, **fields,
            }
            line = json.dumps(row, ensure_ascii=False)
            _loop.run_in_executor(_WORKER_POOL, _write_line, sess.transcript_path, line)

        # ── Plivo output (paced 20 ms µ-law frames) ─────────────────────────
        _utt_push_start   = [0.0]
        _utt_bytes_pushed = [0]
        _audio_drain_time = [0.0]
        # Persistent frame deadline across chunks — prevents micro-gaps that
        # make the TTS voice "break" mid-syllable when Sarvam pauses briefly
        # between streaming chunks.
        _frame_deadline = [0.0]
        # Leftover (< 160 byte) remainder carried between push() calls.
        # CRITICAL: Sarvam streams arbitrary-size chunks (e.g. 1024 bytes),
        # which are NOT multiples of 160. If we padded each chunk's tail
        # frame to 160 with silence, we'd inject ~12 ms of 0xFF silence at
        # EVERY chunk boundary (~8×/sec) → continuous crackle. Instead we
        # carry the remainder forward and only emit full 160-byte frames.
        _pcm_carry = [b""]

        async def push(chunk: bytes) -> None:
            if sess.done or abort[0] or sess._hangup_started or not sess.stream_sid:
                return
            try:
                _FRAME_DUR = 160 / 8000.0
                buf = _pcm_carry[0] + chunk
                _pcm_carry[0] = b""
                now = time.monotonic()
                if _frame_deadline[0] == 0.0 or now - _frame_deadline[0] > 0.25:
                    _frame_deadline[0] = now
                n = len(buf)
                full = (n // 160) * 160
                for offset in range(0, full, 160):
                    if sess.done or abort[0] or sess._hangup_started:
                        _pcm_carry[0] = b""
                        return
                    frame = buf[offset:offset + 160]
                    await ws.send_json(_carrier.media_msg(frame, sess.stream_sid))
                    _utt_bytes_pushed[0] += 160
                    _audio_drain_time[0] = _utt_push_start[0] + _utt_bytes_pushed[0] / 8000.0
                    _frame_deadline[0] += _FRAME_DUR
                    sleep_for = _frame_deadline[0] - time.monotonic()
                    await asyncio.sleep(max(0.0, sleep_for))
                # Carry the sub-160 remainder into the next chunk (no padding).
                if full < n:
                    _pcm_carry[0] = buf[full:]
            except Exception as exc:
                abort[0] = True
                if not sess.done and not sess._hangup_started:
                    log.debug("push interrupted (carrier gone): %s", exc)

        async def _flush_carry() -> None:
            """Emit the final remainder once, padded to a full 160-byte frame.
            Called only at end of an utterance — padding here is harmless
            (single ~< 20 ms tail), unlike mid-stream padding."""
            rem = _pcm_carry[0]
            _pcm_carry[0] = b""
            if not rem or sess.done or abort[0] or sess._hangup_started or not sess.stream_sid:
                return
            frame = rem + (b"\xff" * (160 - len(rem)))
            try:
                await ws.send_json(_carrier.media_msg(frame, sess.stream_sid))
                _utt_bytes_pushed[0] += 160
                _audio_drain_time[0] = _utt_push_start[0] + _utt_bytes_pushed[0] / 8000.0
            except Exception:
                pass

        async def send_mark() -> None:
            if sess.done or not sess.stream_sid:
                return
            msg = _carrier.mark_msg(sess.stream_sid)
            if msg is None:
                drained.set()
                return
            drained.clear()
            sess.marks_out += 1
            try:
                await ws.send_json(msg)
            except Exception:
                pass

        async def send_clear() -> None:
            if sess.done or not sess.stream_sid:
                return
            msg = _carrier.clear_msg(sess.stream_sid)
            sess.marks_out = 0
            drained.set()
            if msg is None:
                return
            try:
                await ws.send_json(msg)
            except Exception:
                pass

        # Goodbye/terminal markers — every closing line ends with one of these.
        # If the line being spoken is a closing, suppress barge-in for its whole
        # duration (streaming sets sess.closing_in_progress too late otherwise).
        _CLOSING_MARKERS = (
            "आपका दिन शुभ हो",          # payment_confirm / ptp / partial / cannot_pay / disputed
            "records update कर देंगे",   # already_paid
            "team जल्द आपसे contact",    # deceased
            "call back करेंगे",          # no_response
        )

        # ── TTS ─────────────────────────────────────────────────────────────
        async def play_tts(text: str) -> None:
            text = text.strip()
            if not text or sess.done or sess._hangup_started or not sess.stream_sid:
                return
            # If a prior TTS is still in flight (e.g. greeting being aborted
            # mid-stream so we can switch to the voicemail line), WAIT for it
            # to actually exit before resetting abort[0]. Otherwise resetting
            # the flag here un-aborts the prior stream and both end up pushing
            # audio bytes concurrently → the "cracked" interleaved voice.
            if sess.speaking:
                abort[0] = True  # signal any prior tts loop to stop NOW
                _wait_start = time.monotonic()
                while sess.speaking and not sess.done and not sess._hangup_started:
                    if time.monotonic() - _wait_start > 1.5:
                        log.warning("[TTS] prior speech didn't exit in 1.5s; proceeding anyway")
                        break
                    await asyncio.sleep(0.02)
            # Closing line → no barge-in while the goodbye plays.
            if any(m in text for m in _CLOSING_MARKERS):
                sess.closing_in_progress = True
            t0 = time.perf_counter()
            abort[0] = False
            sess.barge_in_active = False
            sess.tts_started_at  = time.monotonic()
            _utt_push_start[0]   = time.monotonic()
            _utt_bytes_pushed[0] = 0
            _frame_deadline[0]   = 0.0
            _pcm_carry[0]        = b""   # fresh utterance, no carried remainder
            sess.speaking = True
            try:
                ok = await tts_speak(text, push, abort)
                if not ok:
                    log.warning("[TTS] produced no audio for: %.40s", text)
                if not abort[0]:
                    await _flush_carry()   # emit final sub-160 tail once, padded
                if not sess.done:
                    await send_mark()
            except Exception as exc:
                log.error("play_tts error: %s", exc)
            finally:
                sess.speaking = False
            log.info("[BOT-TTS] done in %.0f ms | %.60s",
                     (time.perf_counter() - t0) * 1000, text)

        class _SafeMap(dict):
            def __missing__(self, key: str) -> str:
                return f"{{{key}}}"

        async def speak(text: str) -> None:
            text = text.format_map(_SafeMap(sess.ctx)).strip()
            if not text:
                return
            log.info("[BOT] %s", text)
            record("bot", text=text)
            await play_tts(text)
            sess.opening_complete = True

        async def _speak_opening(opening_text: str) -> None:
            await speak(opening_text)
            sess.llm_history.append({"role": "assistant", "content": opening_text})

        # One-shot guard so AMD-callback and STT-phrase fallback can't double-fire.
        _voicemail_fired = [False]

        async def _speak_voicemail() -> None:
            """Speak the voicemail message and end the call. No LLM loop.

            Critical: wait AMD_BEEP_WAIT_MS first so our message lands AFTER
            the voicemail's own greeting + beep. Speaking too early means the
            first half of the message is heard by the voicemail (not recorded)
            and the customer only gets a half-message on playback.
            """
            if _voicemail_fired[0]:
                return
            _voicemail_fired[0] = True
            sess.state = "voicemail"
            log.info("[AMD] voicemail mode — aborting greeting, waiting %d ms for beep",
                     AMD_BEEP_WAIT_MS)
            # Stop any in-flight greeting NOW so the customer's voicemail greeting
            # plays cleanly (without our voice overlapping it).
            abort[0] = True
            await send_clear()
            # Pre-beep silence — let the voicemail's greeting + beep complete.
            await asyncio.sleep(AMD_BEEP_WAIT_MS / 1000.0)
            if sess.done or sess._hangup_started:
                return
            text = build_voicemail_message(sess.ctx)
            log.info("[AMD] beep wait done — speaking voicemail message")
            record("voicemail_attempt", text=text)
            # Use speak() so closing-marker logic suppresses barge-in mid-message.
            await speak(text)
            sess.llm_history.append({"role": "assistant", "content": text})
            asyncio.create_task(hangup("voicemail_left"))

        async def _amd_monitor() -> None:
            """Wait up to ~8 s (Plivo's AMD detection window) for a 'machine'
            verdict. Driven by an asyncio.Event set in /amd-callback — no
            polling, so 100 concurrent calls don't burn 1000 task switches/
            second just watching for a verdict. Plivo's callback ONLY fires
            for machine-detected calls (humans never trigger it), so the
            absence of an event is treated as 'probably human'."""
            if not AMD_ENABLED or not sess.call_sid:
                return
            ev = amd_events.get(sess.call_sid)
            if ev is None:
                ev = asyncio.Event()
                amd_events[sess.call_sid] = ev
            # AMD-callback may have already fired BEFORE we registered the
            # event (Plivo can deliver fast). If a verdict is already in the
            # results map, set the event ourselves so we don't block on it.
            if amd_results.get(sess.call_sid):
                ev.set()
            try:
                try:
                    await asyncio.wait_for(ev.wait(), timeout=8.0)
                except asyncio.TimeoutError:
                    return
                if sess.done or _voicemail_fired[0]:
                    return
                if amd_results.get(sess.call_sid, "") == "machine":
                    log.info("[AMD] mid-call machine verdict — switching to voicemail")
                    if AMD_LEAVE_MESSAGE:
                        await _speak_voicemail()
                    else:
                        asyncio.create_task(hangup("voicemail"))
            finally:
                amd_events.pop(sess.call_sid, None)

        # ── Barge-in (driven by sustained VAD speech-start) ─────────────────
        def _barge_in_allowed() -> bool:
            return sess.opening_complete and not sess.closing_in_progress

        async def on_speech_start() -> None:
            """
            Barge-in is DISABLED.

            We still record that the customer is producing audio so the
            silence timer in the LLM loop pauses while they speak (this is
            what stops the silence prompt firing over a speaking customer),
            but we NEVER abort the bot's in-flight TTS. The bot always
            finishes its current line; whatever the customer says meanwhile
            is transcribed and processed on the next turn.
            """
            log.info("[STT] speech detected")
            # Mark that the customer is actively producing input. The silence
            # timer in the LLM loop checks this flag and pauses while it's set.
            sess.last_speech_started_at = time.monotonic()
            # No barge-in: do not abort TTS, do not flush Plivo audio.
            return

        # ── Voicemail-by-STT fallback ──────────────────────────────────────
        # When Plivo AMD misses (no callback, slow callback, or unsupported on
        # the plan), the voicemail's own greeting gets transcribed by Sarvam.
        # Detecting a "voicemail / leave a message / forwarded / not available"
        # phrase in the early transcripts is the most reliable signal we have.
        _VOICEMAIL_PHRASES = (
            # English / transliterated (Sarvam often returns these as Devanagari)
            "voice mail", "voicemail", "वॉइस मेल", "वॉइसमेल", "वॉयस मेल", "वॉयसमेल",
            "leave a message", "leave a voicemail", "leave your message",
            "after the beep", "after the tone", "बीप के बाद", "बीप क बाद",
            "forwarded to voicemail", "फॉरवर्डेड टू वॉइस", "फॉरवर्ड टू वॉइस",
            "not available", "उपलब्ध नहीं", "उपलब्ध नही",
            "is unable to take", "cannot take your call", "can't take your call",
            "please record your message", "रिकॉर्ड your message",
            "powered by google", "automated voice",
            # Hindi carrier voicemail phrases
            "व्यस्त है", "उत्तर नहीं", "जवाब नहीं दे रहे",
            "आपका संदेश", "संदेश छोड़", "संदेश रिकॉर्ड",
        )

        def _looks_like_voicemail(text: str) -> bool:
            low = text.lower()
            return any(p.lower() in low for p in _VOICEMAIL_PHRASES)

        async def on_transcript(text: str) -> None:
            # Customer's utterance finished and was transcribed → clear the
            # speech-active flag so the silence timer can resume counting.
            sess.last_speech_started_at = 0.0
            text = (text or "").strip()
            if not text or sess.done or text == sess.last_queued:
                return

            # STT-side voicemail catch — fires only once (gated by
            # _voicemail_fired which is shared with the AMD monitor).
            if (not _voicemail_fired[0]
                    and not sess._hangup_started
                    and not sess.done
                    and len(sess.llm_history) <= 1   # only opening so far
                    and _looks_like_voicemail(text)):
                log.info("[AMD/STT] voicemail phrase detected in transcript — "
                         "switching to voicemail mode: %.80s", text)
                # _speak_voicemail flips _voicemail_fired itself
                asyncio.create_task(_speak_voicemail())
                return

            log.info("[USER] %s", text)
            sess.last_queued = text
            sess.barge_in_active = False
            await utt_q.put(text)

        rest_stt = RestStt(on_transcript=on_transcript, on_speech_start=on_speech_start)

        # ── Hangup ──────────────────────────────────────────────────────────
        async def hangup(reason: str = "unknown") -> None:
            if sess.done or sess._hangup_started:
                return
            sess._hangup_started = True
            abort[0] = True
            log.info("[CALL] hangup — reason=%s", reason)
            record("hangup", reason=reason)
            if sess.marks_out > 0:
                try:
                    await asyncio.wait_for(drained.wait(), timeout=12.0)
                except asyncio.TimeoutError:
                    log.warning("Hangup: audio drain timeout")
            else:
                remaining = _audio_drain_time[0] - time.monotonic()
                if remaining > 0.1:
                    log.info("[CALL] waiting %.1f s for Plivo audio drain", remaining)
                    await asyncio.sleep(remaining)
            sess.done = True
            await rest_stt.close()
            call_vars: dict[str, Any] = {}
            try:
                call_vars = await finalize_call_variables(
                    reason, sess.ctx, conversation_to_storage_text(sess.llm_history),
                ) or {}
                if call_vars:
                    record("call_summary", **call_vars)
            except Exception as exc:
                log.warning("call vars error: %s", exc)
            if sess.call_sid:
                wh_body = build_call_summary_push_body(
                    sess.call_sid, reason, call_vars, ctx=sess.ctx, state=sess.state,
                )
                try:
                    from config import RECORDINGS_DIR
                    Path(RECORDINGS_DIR).mkdir(parents=True, exist_ok=True)
                    summary_path = f"{RECORDINGS_DIR}/{sess.call_sid}.summary.json"
                    with open(summary_path, "w", encoding="utf-8") as fh:
                        fh.write(json.dumps(wh_body, ensure_ascii=False, indent=2))
                    log.info("Call summary persisted (call=%s)", sess.call_sid)
                except Exception as exc:
                    log.warning("Could not persist call summary: %s", exc)
            await asyncio.sleep(HANGUP_GRACE_SEC)
            if sess.call_sid:
                try:
                    await _carrier.hangup(sess.call_sid)
                except Exception as exc:
                    log.error("Carrier hangup error: %s", exc)
            try:
                await ws.close()
            except Exception:
                pass

        # ── Carrier WebSocket receiver ─────────────────────────────────────
        async def recv_ws() -> None:
            try:
                async for raw in ws.iter_text():
                    if sess.done:
                        break
                    data = json.loads(raw)
                    evt_type, payload = _carrier.parse_ws_frame(data)

                    if evt_type == "call_start":
                        sess.stream_sid = payload["stream_sid"]
                        sess.call_sid   = payload["call_sid"]
                        if sess.call_sid in pending_ctx:
                            sess.ctx = pending_ctx.pop(sess.call_sid)
                        log.info("[PLIVO] call connected — Stream=%s Call=%s",
                                 sess.stream_sid, sess.call_sid)
                        record(
                            "call_start",
                            phone=sess.ctx.get("phone_number", ""),
                            customer=sess.ctx.get("customer_name", ""),
                            loan_id=sess.ctx.get("loan_id", ""),
                            emi=sess.ctx.get("emi_overdue_amt") or sess.ctx.get("emi_amount", ""),
                            emi_date=sess.ctx.get("emi_overdue_date") or sess.ctx.get("emi_due_date", ""),
                        )
                        if sess.call_sid and RECORDING_CALLBACK_URL:
                            asyncio.create_task(
                                _carrier.start_recording(sess.call_sid, RECORDING_CALLBACK_URL)
                            )

                        # Fire opening greeting IMMEDIATELY for humans (Plivo's
                        # AMD callback only fires for machines — humans never
                        # trigger it, so waiting upfront would just delay every
                        # real call). The AMD monitor runs in parallel; if a
                        # machine verdict arrives within ~8 s, it aborts the
                        # greeting and switches to voicemail mode.
                        if not sess.greeting_sent:
                            sess.greeting_sent = True
                            sess.state = "opening"
                            opening_text = build_opening_greeting(sess.ctx)
                            record("llm_turn", call_phase="opening",
                                   end_call=False, hangup_reason="",
                                   context_patch={})
                            asyncio.create_task(_speak_opening(opening_text))
                            asyncio.create_task(_amd_monitor())

                    elif evt_type == "audio_frame":
                        if sess.done:
                            continue
                        # Block customer audio ONLY during opening greeting
                        # (let it set the scene cleanly) and during terminal
                        # closings (call is ending anyway). At every other
                        # moment audio flows through so STT can detect customer
                        # speech and trigger barge-in via on_speech_start().
                        if not sess.opening_complete or sess.closing_in_progress:
                            continue
                        mulaw = payload["mulaw"]
                        try:
                            pcm16 = audioop.ulaw2lin(mulaw, 2)
                            if _denoiser is not None:
                                pcm16 = await _loop.run_in_executor(
                                    _WORKER_POOL, _denoiser.feed_sync, pcm16
                                )
                            is_speech = True
                            if _vad_inst is not None and len(pcm16) == 320:
                                try:
                                    is_speech = _vad_inst.is_speech(pcm16, 8000)
                                except Exception:
                                    is_speech = True
                                if is_speech:
                                    _vad_hangover_left[0] = _VAD_HANGOVER_FRAMES
                                elif _vad_hangover_left[0] > 0:
                                    _vad_hangover_left[0] -= 1
                                    is_speech = True
                            if len(pcm16) == 320:
                                # Refresh the "customer is making sound" timestamp
                                # on every confirmed speech frame, but only once
                                # the speech-start gate (on_speech_start) has
                                # already fired — otherwise short noise bursts
                                # would falsely pause the silence timer. After
                                # the gate, this turns last_speech_started_at
                                # from a one-shot stamp into a continuous
                                # "last audible sound" tracker, which prevents
                                # the silence prompt firing over a 5-10 s
                                # customer monologue (e.g. job-loss explanation).
                                if is_speech and sess.last_speech_started_at:
                                    sess.last_speech_started_at = time.monotonic()
                                await rest_stt.feed(pcm16, is_speech)
                        except Exception as exc:
                            log.debug("audio frame error: %s", exc)

                    elif evt_type == "mark_ack":
                        if sess.marks_out > 0:
                            sess.marks_out -= 1
                        if sess.marks_out <= 0:
                            sess.marks_out = 0
                            drained.set()

            except WebSocketDisconnect:
                log.info("[PLIVO] carrier WS disconnected")
            except RuntimeError as exc:
                if "WebSocket is not connected" not in str(exc):
                    raise
            finally:
                abort[0] = True
                if not sess._hangup_started and not sess.done and sess.call_sid:
                    # Drain any in-flight STT before declaring carrier_disconnect.
                    # Without this, a customer's last-second utterance (PTP /
                    # "haan kar dunga" / "kal pay kar dunga") is logged as
                    # [USER] but never reaches llm_history because the LLM
                    # loop has already exited — so finalize_call_variables
                    # sees an empty conversation and emits "no customer
                    # response captured". Production logs from the 06:30
                    # batch (VIKAS, 234b5430) showed 11 s of customer audio
                    # lost exactly this way.
                    try:
                        await rest_stt.drain(timeout=1.5)
                    except Exception as exc:
                        log.debug("STT drain error: %s", exc)
                    # Sweep any utterances still queued into llm_history so
                    # the post-call summarizer sees them. on_transcript may
                    # have pushed late transcripts here after the LLM loop
                    # stopped consuming.
                    late_count = 0
                    while True:
                        try:
                            late = utt_q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if late:
                            sess.llm_history.append({"role": "user", "content": late})
                            late_count += 1
                    if late_count:
                        log.info("[CALL] preserved %d late utterance(s) before hangup", late_count)
                    asyncio.create_task(hangup("carrier_disconnect"))

        # ── LLM conversation loop ──────────────────────────────────────────
        async def llm_conversation_loop() -> None:
            for _ in range(400):
                if sess.done:
                    return
                if sess.call_sid and sess.stream_sid:
                    break
                await asyncio.sleep(0.05)
            else:
                log.error("call_start never arrived — cannot run LLM loop")
                return

            async def _stream_apply_turn(
                llm_input: str,
                user_msg_for_history: str | None,
                preroll: str | None = None,
            ) -> bool:
                say_ready  = asyncio.Event()
                say_holder = [""]
                turn_holder: list[dict[str, Any]] = [{}]

                async def _on_say(text: str) -> None:
                    say_holder[0] = text
                    say_ready.set()

                async def _llm_bg() -> None:
                    result = await stream_conversation_turn(
                        sess.ctx, sess.llm_history, llm_input, _on_say
                    )
                    turn_holder[0] = result
                    say_ready.set()

                # Kick off LLM generation FIRST so it runs concurrently with
                # the preroll filler below — the filler buys time while the
                # LLM (and its STT round-trip) complete, hiding the latency.
                llm_task = asyncio.create_task(_llm_bg())

                # Static "thinking" filler — played once VAD has segmented the
                # customer's utterance (i.e. they stopped talking) and we're
                # about to answer. It plays to completion while _llm_bg streams
                # in the background. Not added to llm_history. Skipped if the
                # LLM already produced its say before the filler started.
                if preroll and not sess.done and not sess._hangup_started and not say_ready.is_set():
                    log.info("[BOT] %s", preroll)
                    record("bot", text=preroll)
                    await play_tts(preroll)

                await say_ready.wait()
                say_text = say_holder[0].strip()

                tts_task: asyncio.Task | None = None
                if say_text and not sess.done:
                    fmt = say_text.format_map(_SafeMap(sess.ctx)).strip()
                    if fmt:
                        log.info("[BOT] %s", fmt)
                        record("bot", text=fmt)
                        tts_task = asyncio.create_task(play_tts(fmt))

                await llm_task
                turn = turn_holder[0] or {
                    "say": say_text, "context_patch": {}, "end_call": False,
                    "hangup_reason": "", "call_phase": "llm",
                }

                patch = turn.get("context_patch")
                if isinstance(patch, dict):
                    safe = {str(k): str(v) for k, v in patch.items()
                            if k not in _FORBIDDEN_CTX_KEYS}
                    if len(safe) < len(patch):
                        log.warning("LLM tried to set forbidden ctx keys: %s",
                                    set(patch) - set(safe))
                    sess.ctx.update(safe)
                sess.state = str(turn.get("call_phase") or "llm")
                record(
                    "llm_turn",
                    call_phase=turn.get("call_phase"),
                    end_call=bool(turn.get("end_call")),
                    hangup_reason=turn.get("hangup_reason"),
                    context_patch=turn.get("context_patch"),
                )

                if user_msg_for_history is not None:
                    sess.llm_history.append({"role": "user", "content": user_msg_for_history})

                if turn.get("end_call"):
                    sess.closing_in_progress = True

                if tts_task:
                    await tts_task
                    if say_text:
                        sess.llm_history.append({"role": "assistant", "content": say_text})
                    sess.opening_complete = True
                elif turn.get("end_call"):
                    log.warning("LLM requested end_call with empty say")

                if turn.get("end_call"):
                    hr = str(turn.get("hangup_reason") or "llm_terminal")
                    asyncio.create_task(hangup(hr))
                    return True
                return False

            # Greeting fired by call_start handler.

            async def _wait_user_silence(timeout: float) -> str | None:
                """Silence budget pauses while:
                  - bot is currently speaking,
                  - opening greeting hasn't finished yet,
                  - or customer is actively mid-utterance (STT detected
                    speech but the transcript hasn't arrived yet).
                Without the 3rd guard the silence prompt would fire while
                the customer is in the middle of saying something — exactly
                the "हैलो आप वहाँ हैं?" clash we saw in prod logs.
                """
                # Stale-utterance grace: if speech_start fires but no transcript
                # arrives within this window (cough, mic noise, brief click),
                # assume STT lost it and resume counting silence.
                STALE_SPEECH_SEC = 5.0
                remaining = timeout
                tick = 0.3
                while remaining > 0 and not sess.done:
                    if sess.speaking or not sess.opening_complete:
                        await asyncio.sleep(tick)
                        continue
                    # Pause while customer is in the middle of speaking.
                    # With the per-frame refresh in recv_ws, this timestamp
                    # tracks "last audible sound", so the 5 s grace now means
                    # "5 s of true silence since the last speech frame", not
                    # "5 s since speech-start" — which was the source of the
                    # silence-prompt clash on long customer utterances.
                    if sess.last_speech_started_at:
                        age = time.monotonic() - sess.last_speech_started_at
                        if age < STALE_SPEECH_SEC:
                            await asyncio.sleep(tick)
                            continue
                        # Stale flag — no fresh speech for STALE_SPEECH_SEC; clear it.
                        sess.last_speech_started_at = 0.0
                    # Also pause while a flushed utterance is still awaiting a
                    # transcript from Sarvam. Otherwise the silence prompt
                    # could fire in the ~0.5-1.5 s window between VAD-detected
                    # end-of-speech and the transcript arriving.
                    if rest_stt.pending_recent():
                        await asyncio.sleep(tick)
                        continue
                    wait = min(tick, remaining)
                    try:
                        return await asyncio.wait_for(utt_q.get(), timeout=wait)
                    except asyncio.TimeoutError:
                        remaining -= wait
                return None

            _silence_count       = 0
            _total_silence_count = 0
            _turn_count          = 0
            _MAX_TURNS           = 20

            while not sess.done:
                if _turn_count >= _MAX_TURNS:
                    log.warning("LLM: max turns (%d) reached — force ending", _MAX_TURNS)
                    await _stream_apply_turn(
                        "[FORCE_END: Maximum conversation length reached. "
                        "Say a brief goodbye and set end_call=true, hangup_reason=max_turns.]",
                        "[अधिकतम मोड़ — कॉल समाप्त]",
                    )
                    if not sess.done:
                        await hangup("max_turns")
                    break

                utterance = await _wait_user_silence(SILENCE_TIMEOUT_SEC)
                if utterance is not None:
                    _silence_count = 0
                else:
                    if sess.done:
                        break
                    _silence_count       += 1
                    _total_silence_count += 1
                    log.info("LLM: %.0f s silence (burst=%d total=%d)",
                             SILENCE_TIMEOUT_SEC, _silence_count, _total_silence_count)

                    if _total_silence_count >= 5 or _silence_count >= 3:
                        log.info("LLM: silence ceiling hit — ending call")
                        await _stream_apply_turn(
                            "[SILENCE_3: No response after 3 silence prompts. "
                            "Say the no-response callback goodbye and set "
                            "end_call=true, hangup_reason=no_response.]",
                            "[मौन — कोई उत्तर नहीं]",
                        )
                        if not sess.done:
                            await hangup("silence_timeout")
                        break

                    if _silence_count == 1:
                        if await _stream_apply_turn(
                            "[SILENCE_1: No response. Briefly ask if they are there / "
                            "couldn't hear, please repeat. end_call=false.]",
                            "[मौन — कोई उत्तर नहीं]",
                        ):
                            break
                        continue

                    if await _stream_apply_turn(
                        "[SILENCE_2: Still no response. Ask once more, "
                        "very briefly, if they can hear. end_call=false.]",
                        "[मौन — कोई उत्तर नहीं]",
                    ):
                        break
                    continue

                if sess.done:
                    break

                # Merge fragments arriving within a short window so we don't
                # respond to half a sentence. Kept short (0.5 s) — the 500 ms
                # STT silence hangover already groups most speech, so a long
                # merge wait is mostly dead time before the bot replies.
                fragments = [utterance]
                MERGE_WINDOW_SEC = 0.5
                while True:
                    try:
                        nxt = await asyncio.wait_for(utt_q.get(), timeout=MERGE_WINDOW_SEC)
                    except asyncio.TimeoutError:
                        break
                    if nxt:
                        fragments.append(nxt)
                utterance = " ".join(f.strip() for f in fragments if f and f.strip()) or "[silence]"
                if len(fragments) > 1:
                    log.info("[USER MERGED] %d fragments → %s", len(fragments), utterance)
                record("user", text=utterance)
                _turn_count += 1
                # Pass a short "thinking" filler as preroll: it plays the
                # moment VAD has confirmed end-of-speech and the transcript is
                # in, WHILE the LLM generates concurrently — so the customer
                # hears an acknowledgement instead of dead air during LLM/TTS
                # latency. Silence-prompt turns below pass no preroll.
                if await _stream_apply_turn(
                    utterance,
                    utterance,
                    preroll=_THINKING_FILLER,
                ):
                    break

            log.info("[CALL] LLM loop ended (phase=%s, turns=%d)", sess.state, _turn_count)

        await asyncio.gather(recv_ws(), llm_conversation_loop())

    finally:
        try:
            if not sess._hangup_started and not sess.done:
                await hangup("unexpected_disconnect")
        except Exception as exc:
            log.warning("cleanup hangup error: %s", exc)
        try:
            await rest_stt.close()
        except Exception:
            pass
        log.info("[CALL] media stream handler closed")
