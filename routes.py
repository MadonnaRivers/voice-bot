"""
routes.py — FastAPI application and HTTP/WebSocket route definitions.
"""
from __future__ import annotations
import asyncio
import logging

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from twilio.rest import Client as TwilioClient

from config import (
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER,
    NGROK_URL, MAKE_CALL_API_KEY,
)
from scripts import build_default_ctx
from session import pending_ctx
from call_handler import media_stream

log = logging.getLogger("aditi")

app = FastAPI(title="Aditi — Hindi EMI Collection Voice Bot")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    from config import LLM_MODEL, SARVAM_VOICE
    return HTMLResponse(
        f"<h3>Aditi — Sarvam STT · {LLM_MODEL} LLM · Sarvam TTS ({SARVAM_VOICE})</h3>"
    )


@app.get("/health")
async def health() -> JSONResponse:
    from config import LLM_MODEL, SARVAM_VOICE
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

    ctx = {**build_default_ctx(), **{k: str(v) for k, v in body.items() if k != "to"}}
    ctx.setdefault("phone_number", to)

    # Keep aliases in sync if caller used the primary field names
    if ctx.get("emi_overdue_amt"):
        ctx["emi_amount"]     = ctx["emi_overdue_amt"]
    if ctx.get("emi_overdue_amt_int"):
        ctx["emi_amount_int"] = ctx["emi_overdue_amt_int"]
    if ctx.get("emi_overdue_date"):
        ctx["emi_due_date"]   = ctx["emi_overdue_date"]

    call = await asyncio.to_thread(
        lambda: TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).calls.create(
            url=f"{NGROK_URL}/outgoing-call", to=to, from_=TWILIO_PHONE_NUMBER,
        )
    )
    pending_ctx[call.sid] = ctx
    log.info("Outbound call initiated — SID=%s to=%s", call.sid, to)
    return JSONResponse({"call_sid": call.sid})


@app.api_route("/outgoing-call", methods=["GET", "POST"])
async def outgoing_call(request: Request) -> HTMLResponse:
    from twilio.twiml.voice_response import Connect, VoiceResponse
    resp = VoiceResponse()
    resp.pause(length=1)
    conn = Connect()
    conn.stream(url=f"wss://{request.url.hostname}/media-stream")
    resp.append(conn)
    return HTMLResponse(str(resp), media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream_route(twilio_ws: WebSocket) -> None:
    await media_stream(twilio_ws)
