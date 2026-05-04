"""
clients.py — Shared HTTP and LLM clients (one instance per process).
"""
from __future__ import annotations
import httpx
from openai import AsyncOpenAI
from config import SARVAM_API_KEY, SARVAM_LLM_BASE_URL, OPENAI_API_KEY

# Async HTTP client — used for TTS requests.
# - 10 keepalive slots so multiple concurrent TTS pipelines reuse TCP+TLS connections
#   (avoids ~100-200 ms handshake on every request after the first)
# - connect=2.0 s — fail-fast on bad network; doesn't affect successful connections
http = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=2.0, read=15.0, write=5.0, pool=3.0),
    limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
)

# Sarvam LLM via OpenAI-compatible endpoint (kept for future use)
oai_sarvam = AsyncOpenAI(api_key=SARVAM_API_KEY, base_url=SARVAM_LLM_BASE_URL)

# OpenAI GPT-4.1-mini — used for classification and extraction
oai_llm = AsyncOpenAI(api_key=OPENAI_API_KEY)
