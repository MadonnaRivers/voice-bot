"""
routes.py — FastAPI application and HTTP/WebSocket route definitions.
"""
from __future__ import annotations
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse

import carrier
from clients import http as _http_client
from config import NGROK_URL, MAKE_CALL_API_KEY
from urllib.parse import urlparse as _urlparse

# Pre-parse the ngrok hostname once at import time
# request.url.hostname returns "localhost" when behind ngrok — use NGROK_URL instead
_WS_HOST = _urlparse(NGROK_URL).netloc   # e.g. "a4d3-xxxx.ngrok-free.app"
from scripts import build_default_ctx
from session import pending_ctx
from call_handler import media_stream

log = logging.getLogger("aditi")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield                          # server is running
    await _http_client.aclose()   # clean up httpx on shutdown → no "Event loop is closed" warning
    log.info("HTTP client closed")


app = FastAPI(title="Aditi — Hindi EMI Collection Voice Bot", lifespan=_lifespan)


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

    call_sid = await carrier.make_call(to, f"{NGROK_URL}/outgoing-call")
    pending_ctx[call_sid] = ctx
    log.info("Outbound call initiated — SID=%s to=%s", call_sid, to)
    return JSONResponse({"call_sid": call_sid})


@app.api_route("/outgoing-call", methods=["GET", "POST"])
async def outgoing_call(request: Request) -> HTMLResponse:
    # Plivo sends CallUUID in the POST body when the call is answered.
    # make_call() stored pending_ctx under request_uuid (different from CallUUID),
    # so we rekey it here so call_handler.py can find the context via sess.call_sid.
    try:
        form = await request.form()
        call_uuid = form.get("CallUUID", "")
        if call_uuid and pending_ctx:
            for rid in list(pending_ctx.keys()):
                if rid != call_uuid:
                    pending_ctx[call_uuid] = pending_ctx.pop(rid)
                    log.info("pending_ctx rekeyed: %s → %s", rid, call_uuid)
                    break
    except Exception as exc:
        log.debug("outgoing-call rekey skipped: %s", exc)

    ws_url = f"wss://{_WS_HOST}/media-stream"
    log.info("connect_response ws_url=%s", ws_url)
    return HTMLResponse(carrier.connect_response(ws_url), media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream_route(ws: WebSocket) -> None:
    await media_stream(ws)
