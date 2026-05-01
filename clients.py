"""
clients.py — Shared HTTP and LLM clients (one instance per process).
"""
from __future__ import annotations
import httpx
from openai import AsyncOpenAI
from config import SARVAM_API_KEY, SARVAM_LLM_BASE_URL, OPENAI_API_KEY

# Async HTTP client — used for TTS requests
http = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=4.0, read=15.0, write=5.0, pool=5.0),
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)

# Sarvam LLM via OpenAI-compatible endpoint (kept for future use)
oai_sarvam = AsyncOpenAI(api_key=SARVAM_API_KEY, base_url=SARVAM_LLM_BASE_URL)

# OpenAI GPT-4.1-mini — used for classification and extraction
oai_llm = AsyncOpenAI(api_key=OPENAI_API_KEY)
