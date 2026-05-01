"""
call_handler.py — WebSocket media-stream handler + conversation FSM.

Receives raw µ-law audio from Twilio, pipes it through:
  denoiser → VAD → Sarvam STT (WebSocket)
and drives the scripted EMI collection FSM.
"""
from __future__ import annotations

import array
import asyncio
import audioop
import base64
import concurrent.futures
import json
import logging
import re
import time
import wave
from datetime import date as _date, timedelta
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import webrtcvad
import websockets
import websockets.exceptions as ws_exc
from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect
from twilio.rest import Client as TwilioClient

from config import (
    SARVAM_API_KEY, SARVAM_STT_WS_URL,
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
    VAD_ENABLED, VAD_MODE, VAD_HANGOVER_MS,
    DENOISE_ENABLED, DENOISE_STRENGTH, DENOISE_PROFILE_SEC, DENOISE_STATIONARY,
    HANGUP_GRACE_SEC, SILENCE_TIMEOUT_SEC, BARGE_IN_GUARD_SEC,
    POST_UTTERANCE_PAUSE_SEC, TRANSCRIPTS_DIR,
)
from denoiser import StreamDenoiser
from tts import tts_stream, tts_rest, tts_stream_pipelined, _strip_wav_header
from classifier import (
    classify, llm_extract_amount, extract_callback_time,
    check_faq, finalize_call_variables, FAQ_TRIGGER_RE,
)
from scripts import SCRIPTS, TERMINAL, BARGE_IN_LOCKED, AUTO_ADVANCE
from session import CallSession, pending_ctx
from utils import parse_date, parse_amount, fmt_date, is_callback_time

log = logging.getLogger("aditi")

_VAD_HANGOVER_FRAMES = max(1, VAD_HANGOVER_MS // 20)


# ─────────────────────────────────────────────────────────────────────────────
# Audio file saver
# ─────────────────────────────────────────────────────────────────────────────
def _save_call_audio(sess: "CallSession") -> None:  # type: ignore[name-defined]
    """
    Write per-call WAV files to the transcripts folder:
      <base>_customer.wav  — customer voice (denoised PCM16, mono, 8 kHz)
      <base>_bot.wav       — bot TTS output (PCM16, mono, 8 kHz)
      <base>_combined.wav  — stereo: left=customer, right=bot
    """
    from session import CallSession as _CS  # local import avoids circular
    base = (
        sess.transcript_path.replace(".jsonl", "")
        if sess.transcript_path
        else f"{TRANSCRIPTS_DIR}/unknown_{int(time.time())}"
    )

    cust_pcm = b"".join(sess._customer_audio)
    bot_pcm  = b"".join(sess._bot_audio)

    if not cust_pcm and not bot_pcm:
        log.info("No audio captured — skipping WAV save")
        return

    # Pad to equal length (2-byte aligned)
    max_len = max(len(cust_pcm), len(bot_pcm))
    if max_len % 2:
        max_len += 1
    cust_pcm = cust_pcm.ljust(max_len, b"\x00")
    bot_pcm  = bot_pcm.ljust(max_len, b"\x00")

    def _write_mono(path: str, pcm: bytes) -> None:
        try:
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(8000)
                wf.writeframes(pcm)
            log.info("Audio saved: %s (%.1f s)", path, len(pcm) / 16000)
        except Exception as exc:
            log.warning("WAV write error %s: %s", path, exc)

    _write_mono(base + "_customer.wav", cust_pcm)
    _write_mono(base + "_bot.wav",      bot_pcm)

    # Stereo combined: interleave customer (L) + bot (R) samples
    try:
        cust_s = array.array("h", cust_pcm)
        bot_s  = array.array("h", bot_pcm)
        stereo = array.array("h")
        for c, b in zip(cust_s, bot_s):
            stereo.append(c)
            stereo.append(b)
        path = base + "_combined.wav"
        with wave.open(path, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(stereo.tobytes())
        log.info("Stereo audio saved: %s", path)
    except Exception as exc:
        log.warning("Stereo WAV error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket handler
# ─────────────────────────────────────────────────────────────────────────────
async def media_stream(twilio_ws: WebSocket) -> None:
    await twilio_ws.accept()
    log.info("Twilio media stream connected")

    from scripts import build_default_ctx
    sess  = CallSession(ctx=build_default_ctx())
    abort = [False]
    _fallback_task: list[asyncio.Task | None] = [None]
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
    _denoise_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    _loop = asyncio.get_event_loop()

    _vad_inst           = webrtcvad.Vad(VAD_MODE) if VAD_ENABLED else None
    _vad_hangover_left  = [0]

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
                    # Capture bot audio (µ-law → PCM16 for WAV)
                    try:
                        sess._bot_audio.append(audioop.ulaw2lin(frame, 2))
                    except Exception:
                        pass
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
            if sess.done or not sess.stream_sid:
                return
            try:
                await twilio_ws.send_json({
                    "event": "clear", "streamSid": sess.stream_sid,
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
            """POST_UTTERANCE_PAUSE_SEC silence timer — primary utterance trigger."""
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
            sess.barge_in_active = False
            sess.tts_started_at  = time.monotonic()
            sess.speaking = True
            try:
                # Use pipelined TTS for lower latency on long scripts
                ok = await tts_stream_pipelined(text, push, abort)
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
            script = SCRIPTS.get(state, "")
            if script:
                await speak(script)

        # ── Hangup ───────────────────────────────────────────────────────────
        async def hangup(reason: str = "unknown") -> None:
            if sess.done:
                return
            # Do NOT set sess.done yet — recv_twilio must keep running to
            # receive Twilio's mark event so drained can fire.
            abort[0] = True   # stop any future TTS from sending audio
            log.info("Hangup: %s", reason)
            record("hangup", reason=reason)
            if sess.marks_out > 0:
                try:
                    await asyncio.wait_for(drained.wait(), timeout=12.0)
                except asyncio.TimeoutError:
                    log.warning("Hangup: audio drain timeout")
            # Now safe to stop recv_twilio
            sess.done = True
            try:
                call_vars = await finalize_call_variables(reason, sess.ctx)
                if call_vars:
                    record("call_summary", **call_vars)
            except Exception as exc:
                log.warning("call vars error: %s", exc)
            # Save audio recordings
            try:
                await asyncio.to_thread(_save_call_audio, sess)
            except Exception as exc:
                log.warning("Audio save error: %s", exc)
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

        # ── FSM transition helper ─────────────────────────────────────────────
        async def go(state: str, *, reset_unclear: bool = True) -> None:
            log.info("FSM %s → %s", sess.state, state)
            sess.state = state
            if reset_unclear:
                sess.unclear_count = 0
            await speak_state(state)
            if state in TERMINAL:
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
                        if sess.call_sid in pending_ctx:
                            sess.ctx = pending_ctx.pop(sess.call_sid)
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
                            sess._customer_audio.append(pcm16)   # ← capture customer audio
                            if _denoiser is not None:
                                pcm16 = await _loop.run_in_executor(
                                    _denoise_pool, _denoiser.feed_sync, pcm16
                                )
                            if _vad_inst is not None and len(pcm16) == 320:
                                try:
                                    is_speech = _vad_inst.is_speech(pcm16, 8000)
                                except Exception:
                                    is_speech = True
                                if is_speech:
                                    _vad_hangover_left[0] = _VAD_HANGOVER_FRAMES
                                elif _vad_hangover_left[0] > 0:
                                    _vad_hangover_left[0] -= 1
                                else:
                                    pcm16 = b"\x00" * 320
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
                            if sess.state in BARGE_IN_LOCKED:
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
                            if sess.state in BARGE_IN_LOCKED:
                                log.debug("Barge-in suppressed (locked state %s): %s", sess.state, transcript)
                                continue
                            elif _guard_elapsed < BARGE_IN_GUARD_SEC:
                                log.debug("Barge-in suppressed (guard %.0f ms): %s", _guard_elapsed * 1000, transcript)
                                continue
                            else:
                                abort[0] = True
                                await send_clear()
                                sess.barge_in_active = True
                                log.info("Barge-in detected via is_final")
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
                sess.state = AUTO_ADVANCE.get(target, "opening")
                return False

            async def reroute_intent(utterance: str) -> tuple[bool, bool]:
                """Check if user changed intent mid-flow. Returns (handled, should_break)."""
                intent = await classify(utterance, "opening")
                record("classify", action=intent, context="reroute")
                if intent == "goto_pay_now":
                    await go("full_payment_today")
                    return True, True
                if intent in ("goto_refusal", "goto_financial_difficulty"):
                    await go("refusal_ask_reason")
                    return True, False
                if intent == "goto_callback":
                    if is_callback_time(utterance):
                        sess.ctx["callback_time"] = await extract_callback_time(utterance)
                        await go("callback_confirm")
                        return True, True
                    await go("callback_ask_time")
                    return True, False
                if intent == "goto_future_promise":
                    d = parse_date(utterance)
                    _today = _date.today()
                    if d and d > _today:
                        sess.ctx["payment_date"] = fmt_date(
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

                # Drain late follow-up fragments for same sentence
                while not utt_q.empty():
                    try:
                        utterance = utt_q.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                utterance = utterance.strip() or "[silence]"
                record("user_turn", text=utterance)
                log.debug("FSM state=%s utterance=%r", sess.state, utterance)

                # ── FAQ cross-state check ─────────────────────────────────────
                if utterance != "[silence]" and FAQ_TRIGGER_RE.search(utterance):
                    faq_key = await check_faq(utterance)
                    if faq_key and faq_key in SCRIPTS:
                        record("faq", key=faq_key)
                        log.info("FAQ detected: %s", faq_key)
                        faq_text = SCRIPTS[faq_key]
                        if sess.state not in ("opening",) and sess.state in SCRIPTS:
                            await speak(faq_text + " " + SCRIPTS[sess.state])
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
                        d = parse_date(utterance)
                        _today = _date.today()
                        if d and d > _today:
                            if d > _today + timedelta(days=90):
                                d = _today + timedelta(days=90)
                            sess.ctx["payment_date"] = fmt_date(d)
                            await go("future_confirm")
                            break
                        await go("future_ask_date")
                    elif action == "goto_already_paid":
                        await go("already_paid_ask_date")
                    elif action == "goto_callback":
                        if is_callback_time(utterance):
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
                            await go("refusal_ask_reason")
                    elif action == "goto_refusal":
                        await go("refusal_ask_reason")
                    elif action == "mark_unclear":
                        if await handle_unclear():
                            break

                # ══════════════════════════════════════════════════════════════
                # C2 BRIDGE: OFFER PARTIAL
                # ══════════════════════════════════════════════════════════════
                elif sess.state == "offer_partial":
                    action = await classify(utterance, "offer_partial")
                    record("classify", action=action)
                    if action == "goto_partial_yes":
                        await go("partial_ask_amount")
                    elif action == "goto_callback":
                        if is_callback_time(utterance):
                            sess.ctx["callback_time"] = await extract_callback_time(utterance)
                            await go("callback_confirm")
                            break
                        await go("callback_ask_time")
                    else:
                        await go("refusal_ask_reason")

                # ══════════════════════════════════════════════════════════════
                # C2: PARTIAL PAYMENT — capture amount
                # ══════════════════════════════════════════════════════════════
                elif sess.state == "partial_ask_amount":
                    _t_low = utterance.lower()
                    if any(k in _t_low for k in ["aadha", "aadhe", "aadhi", "आधा", "आधे", "half"]):
                        emi_int = int(re.sub(r"[^0-9]", "", sess.ctx.get("emi_amount_int", "8500")))
                        amount  = emi_int // 2
                    else:
                        amount = parse_amount(utterance)
                        if amount is None:
                            amount = await llm_extract_amount(utterance)

                    min_p = int(sess.ctx.get("min_partial_int", "1500"))

                    if amount is None:
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
                    sess.ctx["partial_amount"]    = f"{amount:,}"
                    sess.ctx["remaining_balance"] = f"{remaining:,}"
                    sess.partial_attempts = 0
                    await go("partial_ask_remaining_date")

                # ── C2: remaining balance date ────────────────────────────────
                elif sess.state == "partial_ask_remaining_date":
                    d = parse_date(utterance)
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
                        d = today + timedelta(days=90)
                    sess.ctx["payment_date"] = fmt_date(d)
                    sess.date_retries = 0
                    sess.beyond_90_warned = False
                    await go("partial_confirm")
                    break

                # ══════════════════════════════════════════════════════════════
                # C3: FUTURE PAYMENT PROMISE — capture date
                # ══════════════════════════════════════════════════════════════
                elif sess.state == "future_ask_date":
                    d = parse_date(utterance)
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
                            await speak_state("future_date_retry")
                            continue
                    if d < today:
                        d = today
                    if d > today + timedelta(days=90):
                        if not sess.beyond_90_warned:
                            sess.beyond_90_warned = True
                            await speak_state("future_date_beyond_90")
                            continue
                        d = today + timedelta(days=90)
                    sess.ctx["payment_date"] = fmt_date(d)
                    sess.date_retries = 0
                    sess.beyond_90_warned = False
                    await go("future_confirm")
                    break

                # ══════════════════════════════════════════════════════════════
                # C5: REFUSAL
                # ══════════════════════════════════════════════════════════════
                elif sess.state == "refusal_ask_reason":
                    action = await classify(utterance, "refusal_ask_reason")
                    record("classify", action=action)
                    if action == "got_reason":
                        sess.ctx["refusal_reason"] = utterance[:120]
                        await go("refusal_credit_warn")
                    else:
                        sess.refusal_attempts += 1
                        if sess.refusal_attempts >= 1:
                            await go("refusal_escalate")
                        else:
                            await speak_state("refusal_ask_reason")

                elif sess.state == "refusal_escalate":
                    action = await classify(utterance, "refusal_escalate")
                    record("classify", action=action)
                    if action == "got_reason":
                        sess.ctx["refusal_reason"] = utterance[:120]
                    else:
                        sess.ctx.setdefault("refusal_reason", "Unspecified")
                    await go("refusal_credit_warn")

                elif sess.state == "refusal_credit_warn":
                    if is_callback_time(utterance):
                        sess.ctx["callback_time"] = await extract_callback_time(utterance)
                    else:
                        sess.ctx["callback_time"] = "जल्द ही"
                    await go("refusal_close")
                    break

                # ══════════════════════════════════════════════════════════════
                # C6: ALREADY PAID
                # ══════════════════════════════════════════════════════════════
                elif sess.state == "already_paid_ask_date":
                    d = parse_date(utterance)
                    today = _date.today()
                    if d is None:
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
                    sess.ctx["payment_date"] = fmt_date(d)
                    sess.date_retries = 0
                    await go("already_paid_ask_mode")

                elif sess.state == "already_paid_ask_mode":
                    sess.ctx["payment_mode"] = utterance[:80] if utterance != "[silence]" else "not specified"
                    await go("already_paid_confirm")
                    break

                # ══════════════════════════════════════════════════════════════
                # C7: CALLBACK
                # ══════════════════════════════════════════════════════════════
                elif sess.state == "callback_ask_time":
                    if is_callback_time(utterance):
                        sess.ctx["callback_time"] = await extract_callback_time(utterance)
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
