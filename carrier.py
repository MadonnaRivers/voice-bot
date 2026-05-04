"""
carrier.py — Plivo carrier implementation.

Translates Plivo WebSocket media-stream frames and REST API calls into
the normalised interface used by call_handler.py and routes.py.

Normalised WebSocket event types produced by parse_ws_frame():
  "call_start"  → payload: {"stream_sid": str, "call_sid": str}
  "audio_frame" → payload: {"mulaw": bytes}      8 kHz µ-law PCM
  "mark_ack"    → payload: {}                    (unused — Plivo has no mark events)
  "ignore"      → payload: {}                    unhandled / irrelevant frame

NOTE: Plivo does not emit mark acknowledgement events, so mark_msg() returns None.
send_mark() handles None by treating audio as immediately drained.
"""
from __future__ import annotations

import base64 as _b64
import logging

import httpx

from config import PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN, PLIVO_PHONE_NUMBER

log = logging.getLogger("aditi")

_BASE = f"https://api.plivo.com/v1/Account/{PLIVO_AUTH_ID}"
_AUTH = (PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN)


# ─────────────────────────────────────────────────────────────────────────────
# Outbound call
# ─────────────────────────────────────────────────────────────────────────────
async def make_call(to: str, webhook_url: str) -> str:
    """
    Initiate an outbound call via Plivo REST API.
    Returns the request_uuid (used as a temporary key until CallUUID is known).
    The routes.py /outgoing-call handler rekeys pending_ctx to the real CallUUID.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{_BASE}/Call/",
            auth=_AUTH,
            json={
                "from":          PLIVO_PHONE_NUMBER,
                "to":            to,
                "answer_url":    webhook_url,
                "answer_method": "POST",
            },
        )
        r.raise_for_status()
        body = r.json()
        log.debug("Plivo make_call response: %s", body)
        request_uuid = body["request_uuid"]
        log.info("Plivo call request_uuid: %s", request_uuid)
        return request_uuid


# ─────────────────────────────────────────────────────────────────────────────
# Hangup
# ─────────────────────────────────────────────────────────────────────────────
async def hangup(call_sid: str) -> None:
    """Terminate an active Plivo call by CallUUID."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.delete(
            f"{_BASE}/Call/{call_sid}/",
            auth=_AUTH,
        )
        if r.status_code >= 400:
            log.warning("Plivo hangup %s → HTTP %d: %s", call_sid, r.status_code, r.text[:200])
        else:
            log.info("Plivo call %s terminated", call_sid)


# ─────────────────────────────────────────────────────────────────────────────
# Plivo XML — tells Plivo to stream bidirectional audio to our WebSocket
# ─────────────────────────────────────────────────────────────────────────────
def connect_response(ws_url: str) -> str:
    """Return Plivo XML that connects the call to the media-stream WebSocket."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Stream keepCallAlive="true" bidirectional="true" contentType="audio/x-mulaw;rate=8000">'
        f"{ws_url}"
        "</Stream>"
        "</Response>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket frame parsing  (Plivo media stream format)
# ─────────────────────────────────────────────────────────────────────────────
def parse_ws_frame(data: dict) -> tuple[str, dict]:
    """Translate a raw Plivo WebSocket media-stream JSON frame into a normalised event."""
    evt = data.get("event", "")

    if evt == "start":
        start = data.get("start", {})
        return "call_start", {
            "stream_sid": start.get("streamId", ""),
            "call_sid":   start.get("callId",   ""),
        }

    if evt == "media":
        media = data.get("media", {})
        # Skip outbound echo frames (audio we sent back)
        if media.get("track", "inbound") == "outbound":
            return "ignore", {}
        payload = media.get("payload", "")
        if not payload:
            return "ignore", {}
        return "audio_frame", {"mulaw": _b64.b64decode(payload)}

    if evt == "stop":
        return "ignore", {}

    return "ignore", {}


# ─────────────────────────────────────────────────────────────────────────────
# Outgoing WebSocket messages
# ─────────────────────────────────────────────────────────────────────────────
def media_msg(audio_mulaw: bytes, stream_sid: str) -> dict:
    """Build the Plivo JSON payload to send µ-law audio to the caller."""
    return {
        "event": "playAudio",
        "media": {
            "contentType": "audio/x-mulaw",
            "sampleRate":  8000,
            "payload":     _b64.b64encode(audio_mulaw).decode(),
        },
    }


def mark_msg(stream_sid: str) -> dict | None:
    """
    Plivo does not support mark/acknowledgement events.
    Returning None causes send_mark() to treat audio as immediately drained.
    """
    return None


def clear_msg(stream_sid: str) -> dict | None:
    """Return a Plivo clearAudio event to flush buffered audio on barge-in."""
    return {
        "event": "clearAudio",
        "id":    stream_sid,
    }
