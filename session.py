"""
session.py — CallSession dataclass and shared pending-context registry.
"""
from __future__ import annotations
import dataclasses
from typing import List

# Stores per-call context indexed by Twilio Call SID.
# Populated by /make-call route; consumed by the media-stream WebSocket handler.
pending_ctx: dict[str, dict[str, str]] = {}


@dataclasses.dataclass
class CallSession:
    ctx:             dict[str, str]  # customer data + dynamic per-turn values

    stream_sid:      str   = ""
    call_sid:        str   = ""
    state:           str   = "opening"
    done:            bool  = False
    speaking:        bool  = False
    tts_started_at:  float = 0.0    # monotonic time when current TTS play started
    unclear_count:   int   = 0
    marks_out:       int   = 0
    last_queued:     str   = ""
    last_interim:    str   = ""
    transcript_path: str   = ""

    # Workflow state
    partial_attempts:  int  = 0    # retries in partial_ask_amount
    date_retries:      int  = 0    # retries in date-capture states
    refusal_attempts:  int  = 0    # escalation counter in refusal flow
    partial_offered:   bool = False # ensure partial offered only once
    barge_in_active:   bool = False # True while collecting post-barge-in utterance
    beyond_90_warned:  bool = False # True after first "beyond 90 days" warning

    # Hangup guard — set synchronously before first await to prevent double-hangup
    _hangup_started: bool = False

    # Audio capture buffers (PCM16, 8000 Hz, mono)
    _customer_audio: List[bytes] = dataclasses.field(default_factory=list)
    _bot_audio:      List[bytes] = dataclasses.field(default_factory=list)
