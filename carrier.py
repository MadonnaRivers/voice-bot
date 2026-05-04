"""
carrier.py — Carrier abstraction layer (Twilio / Exotel).

Switch carriers by setting CARRIER=twilio (default) or CARRIER=exotel in .env.
All carrier-specific protocol details live here — call_handler.py, routes.py,
and main.py are 100% carrier-agnostic after this abstraction.

When ready to switch to Exotel:
  1. Fill in the EXOTEL section below (≈ 50 lines).
  2. Set CARRIER=exotel in .env.
  3. Nothing else needs changing.

Normalised WebSocket event types produced by parse_ws_frame():
  "call_start"  → payload: {"stream_sid": str, "call_sid": str}
  "audio_frame" → payload: {"mulaw": bytes}      8 kHz µ-law PCM
  "mark_ack"    → payload: {}                    carrier confirmed audio played
  "ignore"      → payload: {}                    unhandled / irrelevant frame
"""
from __future__ import annotations

import asyncio
import logging

from config import CARRIER

log = logging.getLogger("aditi")


# ═════════════════════════════════════════════════════════════════════════════
# EXOTEL  (stub — fill in when switching from Twilio)
# ═════════════════════════════════════════════════════════════════════════════
if CARRIER == "exotel":
    # Exotel docs:
    #   Calls API  → https://developer.exotel.com/api/#calls
    #   ExoStream  → https://developer.exotel.com/api/#exostream  (WebSocket)
    #   ExoML      → https://developer.exotel.com/api/#exoml      (call flow XML)

    from config import (
        EXOTEL_API_KEY, EXOTEL_API_TOKEN,
        EXOTEL_SID, EXOTEL_PHONE_NUMBER,
    )

    async def make_call(to: str, webhook_url: str) -> str:
        """
        Initiate an outbound call via Exotel REST API.
        Returns the Exotel Call SID string.

        TODO — replace the stub below with real implementation:

        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://api.exotel.com/v1/Accounts/{EXOTEL_SID}/Calls/connect",
                auth=(EXOTEL_API_KEY, EXOTEL_API_TOKEN),
                data={
                    "From":     EXOTEL_PHONE_NUMBER,
                    "To":       to,
                    "CallerId": EXOTEL_PHONE_NUMBER,
                    "Url":      webhook_url,       # points to /outgoing-call
                },
            )
            r.raise_for_status()
            return r.json()["Call"]["Sid"]
        """
        raise NotImplementedError("Exotel make_call — fill in carrier.py")

    async def hangup(call_sid: str) -> None:
        """
        Terminate an active Exotel call.

        TODO:
        import httpx
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.exotel.com/v1/Accounts/{EXOTEL_SID}/Calls/{call_sid}",
                auth=(EXOTEL_API_KEY, EXOTEL_API_TOKEN),
                data={"Status": "completed"},
            )
        """
        raise NotImplementedError("Exotel hangup — fill in carrier.py")

    def connect_response(ws_url: str) -> str:
        """
        Return ExoML XML that tells Exotel to stream bidirectional audio to ws_url.

        TODO — verify exact ExoML tag name from Exotel docs:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response>'
            f'  <Stream bidirectional="true"><URL>{ws_url}</URL></Stream>'
            '</Response>'
        )
        """
        raise NotImplementedError("Exotel connect_response — fill in carrier.py")

    def parse_ws_frame(data: dict) -> tuple[str, dict]:
        """
        Translate a raw Exotel WebSocket JSON frame into a normalised event.

        TODO — map Exotel field names (verify from their latest stream docs):

        evt = data.get("event", "")
        if evt == "start":
            return "call_start", {
                "stream_sid": data["start"]["streamSid"],   # confirm field name
                "call_sid":   data["start"].get("callSid", ""),
            }
        if evt == "media":
            import base64
            return "audio_frame", {"mulaw": base64.b64decode(data["media"]["payload"])}
        if evt == "stop":
            return "ignore", {}
        return "ignore", {}

        NOTE: Exotel may not support mark/clear events.
              If so, send_mark() / send_clear() already handle None gracefully.
        """
        raise NotImplementedError("Exotel parse_ws_frame — fill in carrier.py")

    def media_msg(audio_mulaw: bytes, stream_sid: str) -> dict:
        """
        Build the JSON payload to send µ-law audio back to Exotel.

        TODO — confirm Exotel's expected send format from their stream docs:
        import base64
        return {
            "event":     "media",
            "streamSid": stream_sid,
            "media":     {"payload": base64.b64encode(audio_mulaw).decode()},
        }
        """
        raise NotImplementedError("Exotel media_msg — fill in carrier.py")

    def mark_msg(stream_sid: str) -> dict | None:
        """
        Return mark message dict, or None if Exotel does not support marks.
        Returning None causes send_mark() to treat audio as immediately drained.

        TODO: check Exotel docs. If they do support marks, return the payload.
        For now, assume no mark support:
        """
        return None  # update once Exotel mark support is confirmed

    def clear_msg(stream_sid: str) -> dict | None:
        """
        Return clear/flush message dict, or None if Exotel does not support it.

        TODO: check Exotel docs.
        """
        return None  # update once Exotel clear support is confirmed


# ═════════════════════════════════════════════════════════════════════════════
# TWILIO  (default — fully implemented)
# ═════════════════════════════════════════════════════════════════════════════
else:
    import base64 as _b64

    from config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER

    async def make_call(to: str, webhook_url: str) -> str:
        """Initiate a Twilio outbound call. Returns the Call SID."""
        from twilio.rest import Client as TwilioClient
        call = await asyncio.to_thread(
            lambda: TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).calls.create(
                url=webhook_url, to=to, from_=TWILIO_PHONE_NUMBER,
            )
        )
        log.info("Twilio call SID: %s", call.sid)
        return call.sid

    async def hangup(call_sid: str) -> None:
        """Terminate a Twilio call by SID."""
        from twilio.rest import Client as TwilioClient
        await asyncio.to_thread(
            lambda: TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                    .calls(call_sid).update(status="completed")
        )
        log.info("Twilio call %s terminated", call_sid)

    def connect_response(ws_url: str) -> str:
        """Return TwiML XML that connects the call to the media-stream WebSocket."""
        from twilio.twiml.voice_response import Connect, VoiceResponse
        resp = VoiceResponse()
        resp.pause(length=1)
        conn = Connect()
        conn.stream(url=ws_url)
        resp.append(conn)
        return str(resp)

    def parse_ws_frame(data: dict) -> tuple[str, dict]:
        """Translate a raw Twilio WebSocket JSON frame into a normalised event."""
        evt = data.get("event", "")
        if evt == "start":
            start = data.get("start", {})
            return "call_start", {
                "stream_sid": start.get("streamSid", ""),
                "call_sid":   start.get("callSid",   ""),
            }
        if evt == "media":
            return "audio_frame", {
                "mulaw": _b64.b64decode(data["media"]["payload"]),
            }
        if evt == "mark":
            return "mark_ack", {}
        return "ignore", {}

    def media_msg(audio_mulaw: bytes, stream_sid: str) -> dict:
        """Build the Twilio JSON payload to send µ-law audio to the caller."""
        return {
            "event":     "media",
            "streamSid": stream_sid,
            "media":     {"payload": _b64.b64encode(audio_mulaw).decode()},
        }

    def mark_msg(stream_sid: str) -> dict | None:
        """Return a Twilio mark event (used to detect when audio has finished playing)."""
        return {
            "event":     "mark",
            "streamSid": stream_sid,
            "mark":      {"name": "tts_done"},
        }

    def clear_msg(stream_sid: str) -> dict | None:
        """Return a Twilio clear event (flushes buffered audio on barge-in)."""
        return {
            "event":     "clear",
            "streamSid": stream_sid,
        }
