"""
main.py — Entry point for Aditi EMI Collection Voice Bot.

Usage:
  python main.py +91XXXXXXXXXX   # dial a number and start server
  python main.py                  # start server only (use /make-call API)
"""
from __future__ import annotations
import logging
import os
import sys
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("aditi")

# Import app + config after logging is set up
from routes import app
from config import NGROK_URL, PORT
from scripts import build_default_ctx
from session import pending_ctx


def _place_call(to: str, ctx: dict) -> str:
    import asyncio
    import carrier as _carrier
    call_sid = asyncio.run(_carrier.make_call(to, f"{NGROK_URL}/outgoing-call"))
    pending_ctx[call_sid] = ctx
    log.info("Call SID: %s", call_sid)
    return call_sid


if __name__ == "__main__":
    import uvicorn

    to = sys.argv[1].strip() if len(sys.argv) > 1 else os.getenv("CALL_TO", "")
    if not to:
        print("Usage: python main.py <phone_number>")
        sys.exit(1)

    log.info("aditi — Dialing %s …", to)

    def _dial() -> None:
        time.sleep(2)
        try:
            _place_call(to, build_default_ctx())
        except Exception as exc:
            log.error("Dial error: %s", exc)

    threading.Thread(target=_dial, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
