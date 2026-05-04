"""
classifier.py — LLM-based intent classification, entity extraction,
                FAQ detection, and call-variable finalisation.

All calls use OpenAI GPT-4.1-mini via clients.oai_llm.
"""
from __future__ import annotations
import json
import logging
import re
from datetime import date as _date

from clients import oai_llm
from config import LLM_MODEL

log = logging.getLogger("aditi")


# ─────────────────────────────────────────────────────────────────────────────
# Intent classification
# ─────────────────────────────────────────────────────────────────────────────
_LLM_CLASSIFY_PROMPTS: dict[str, str] = {
    "opening": """\
Classify a Hindi/Hinglish customer reply in an EMI collection call.
Agent asked: "Aapka EMI pending hai, kab tak kar paayenge?"
Customer said: "{utterance}"

LABEL DEFINITIONS — read carefully before choosing:
GOTO_PAY_NOW              — agrees to pay today / same day, no future date mentioned
                            ("हाँ", "okay", "करूंगा", "ठीक है", "bilkul", "ji haan",
                             "NEFT kar deta hoon", "GPay kar deta hoon",
                             "शाम तक कर दूंगा", "रात तक भेज दूंगा" ← same-day time = PAY_NOW)
                            NOTE: a SPECIFIC rupee amount without "baki baad mein" = PAY_NOW not partial
GOTO_PARTIAL              — explicitly offers PARTIAL amount, mentions rest will come later
                            ("thoda de sakta hoon", "aadha abhi", "poora nahi thoda-thoda",
                             "kuch aaj baki baad mein", "installment mein dunga",
                             "3000 de sakta hoon baki baad" ← partial only if rest-later is implied)
GOTO_FUTURE_PROMISE       — names a future day/date beyond today
                            ("kal", "agle hafte", "10 tarikh", "salary ke baad",
                             "thoda waqt do", "kuch din de do", "parso")
GOTO_ALREADY_PAID         — claims payment was already made
                            ("kar diya", "UPI se bhej diya", "ho gaya payment")
GOTO_CALLBACK             — temporally busy RIGHT NOW, asks to talk later
                            ("baad mein", "bahar hoon", "abhi time nahi", "driving",
                             "office mein hoon", "meeting mein hoon")
GOTO_DEATH                — death of borrower or close family member
GOTO_FINANCIAL_DIFFICULTY — financially unable to pay
                            ("paise nahi", "mushkil hai", "naukri gayi", bare "nahi")
GOTO_REFUSAL              — aggressive flat refusal or legal threat
                            ("nahi dunga", "band karo", "court mein jao", "RBI complaint",
                             "harassment band karo", "legal action lunga")
MARK_UNCLEAR              — completely unintelligible, random noise, no meaning

CRITICAL DISAMBIGUATION RULES (apply in order):
1. Same-day time of day (शाम, रात, सुबह, दोपहर + "तक"/"को") WITHOUT kal/parso/agle
   → GOTO_PAY_NOW   e.g. "शाम तक कर दूंगा" = PAY_NOW, NOT future_promise
2. "करूंगा" / "kar dunga" alone (no future date) → GOTO_PAY_NOW
3. "kal", "agle hafte", "10 tarikh", "salary ke baad" → GOTO_FUTURE_PROMISE
4. "thoda waqt do" / "kuch din do" → GOTO_FUTURE_PROMISE (asking for time ≠ inability)
5. "अभी time नहीं" / "abhi nahi kar paunga" → GOTO_CALLBACK (temporal busy, not broke)
6. Bare "नहीं" / "nahi" without aggression → GOTO_FINANCIAL_DIFFICULTY
7. GOTO_REFUSAL ONLY for explicit aggression, legal threats, or "nahi dunga" with finality
8. WON'T vs CAN'T — critical distinction:
   "nahi karunga" / "nahi dunga" / "nahi kar raha" / "nahi karunga main"
   (active CHOICE not to pay, present/future tense) → GOTO_REFUSAL
   "nahi kar paunga" / "nahi kar sakta" / "capable nahi" / "mushkil hai"
   (INABILITY / capacity issue) → GOTO_FINANCIAL_DIFFICULTY

Output ONLY the label, nothing else.""",

    "offer_partial": """\
This is an EMI collection call. The agent offered the customer a partial payment option.
Customer said: "{utterance}"

Output EXACTLY ONE label:
GOTO_PARTIAL_YES — customer agrees to make a partial payment today
                   ("haan", "thoda de sakta hoon", "2000 abhi", "aadha kar deta hoon", "okay",
                    "kar deta hoon", "theek hai", "haan kar dunga")
GOTO_CALLBACK    — customer is EXPLICITLY busy/unavailable RIGHT NOW and wants to be called back
                   ("baad mein", "bahar hoon", "kal baat karte hain", "phir baat karte hain",
                    "abhi time nahi", "meeting mein hoon", "driving kar raha hoon")
                   NOTE: GOTO_CALLBACK requires a clear temporal unavailability signal.
GOTO_PARTIAL_NO  — customer declines partial payment or gives any other response including
                   bare greetings, confusion, silence, or unrelated speech
                   ("nahi", "sambhav nahi", "nahi kar sakta", "partial bhi nahi hoga",
                    "hello", "हेलो", "kya", "haan?" ← confusion/greeting without payment intent)

CRITICAL: Bare greetings ("hello", "हेलो", "haan?") with no payment intent → GOTO_PARTIAL_NO, NOT GOTO_CALLBACK.
GOTO_CALLBACK ONLY if the customer explicitly says they are busy/unavailable right now.

Output ONLY the label, nothing else.""",

    "refusal_ask_reason": """\
This is an EMI collection call. The agent asked the customer WHY they cannot pay.
Agent asked: "Aap payment kyun nahi kar pa rahe hain?"
Customer said: "{utterance}"

Output EXACTLY ONE label:
GOT_REASON     — customer gave ANY explanation for why they can't pay, even indirect or one word.
                 Counts as a reason: job loss, illness, salary delayed, business loss, family expense,
                 wedding, school fees, floods, "haath mein nahi hai" (circumstances beyond control),
                 "paise nahi hain" repeated, "kuch nahi kar sakta", any hardship mentioned.
STILL_REFUSING — customer refuses to explain at all. Signs: anger, dismissal, deflection without
                 any content ("band karo", "chodo", "nahi bataunga", "aapko kya",
                 "personal baat hai" ← privacy deflection with NO reason = STILL_REFUSING,
                 "mat pucho", "call kaat raha hoon").

IMPORTANT: "personal baat hai" / "personal matter" alone = STILL_REFUSING (no reason given).
IMPORTANT: Any word hinting at a financial/health/family situation = GOT_REASON.
IMPORTANT: "pichle mahine EMI bhari thi, double nahi hoga" = GOT_REASON (claiming prior payment = reason).
IMPORTANT: Any claim of prior payment, dispute, or disagreement with the amount = GOT_REASON.

Output ONLY the label, nothing else.""",

    "refusal_escalate": """\
This is an EMI collection call. The agent asked the customer a second time for a specific reason.
Agent asked: "Koi specific wajah hai jiske liye aap payment nahi kar pa rahe?"
Customer said: "{utterance}"

Output EXACTLY ONE label:
GOT_REASON     — customer gave ANY reason or hint at circumstances, even indirect.
                 Counts: "business slow hai", "patni bimaar hai", "haath mein nahi hai"
                 (= circumstances beyond control), "paise nahi hain", "salary nahi aayi",
                 "mera control nahi hai", any hardship — even one word.
STILL_REFUSING — customer completely refuses to engage, gives no content at all.
                 Signs: "band karo", "koi baat nahi karunga", "call kaat raha hoon"
                 ← explicitly disconnecting = STILL_REFUSING, anger/dismissal, silence.

IMPORTANT: Indirect reasons like "mere haath mein nahi hai", "kuch nahi kar sakta"
           = GOT_REASON (they are explaining circumstances beyond their control).
IMPORTANT: "call kaat raha hoon" / threatening to disconnect = STILL_REFUSING.

Output ONLY the label, nothing else.""",
}

_LLM_KEYWORD_MAP: dict[str, str] = {
    "GOTO_PAY_NOW":              "goto_pay_now",
    "GOTO_PARTIAL":              "goto_partial",
    "GOTO_FUTURE_PROMISE":       "goto_future_promise",
    "GOTO_ALREADY_PAID":         "goto_already_paid",
    "GOTO_CALLBACK":             "goto_callback",
    "GOTO_DEATH":                "goto_death",
    "GOTO_FINANCIAL_DIFFICULTY": "goto_financial_difficulty",
    "GOTO_REFUSAL":              "goto_refusal",
    "GOTO_PARTIAL_YES":          "goto_partial_yes",
    "GOTO_PARTIAL_NO":           "goto_partial_no",
    "GOT_REASON":                "got_reason",
    "STILL_REFUSING":            "still_refusing",
    "MARK_UNCLEAR":              "mark_unclear",
}

# Keywords that are strict prefixes of longer keywords in the map.
# The streaming early-exit must NOT return on these — it must wait for the
# disambiguating suffix tokens before deciding.
# e.g. "GOTO_PARTIAL" arrives before "_YES" / "_NO" and must not match early.
_AMBIGUOUS_PREFIXES: frozenset[str] = frozenset(
    kw for kw in _LLM_KEYWORD_MAP
    if any(other != kw and other.startswith(kw) for other in _LLM_KEYWORD_MAP)
)  # → frozenset({'GOTO_PARTIAL'})


async def classify(utterance: str, state: str) -> str:
    """
    GPT-4.1-mini intent classifier with streaming early-exit.

    Returns as soon as the label token is identified in the stream,
    typically after 1-3 tokens (~80-120 ms) instead of waiting for the
    full response (~250 ms).  Saves ~100-150 ms per classify call.
    """
    prompt_template = _LLM_CLASSIFY_PROMPTS.get(state)
    if not prompt_template:
        log.info("CLASSIFY[no-prompt] %s → mark_unclear", state)
        return "mark_unclear"

    content = prompt_template.format(utterance=utterance)
    _kws_by_len = sorted(_LLM_KEYWORD_MAP, key=len, reverse=True)  # longest-first to avoid prefix match

    try:
        accumulated = ""
        # stream=True: compatible with all openai SDK v1.x versions
        stream = await oai_llm.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": content}],
            temperature=0,
            max_tokens=32,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                accumulated += delta
                upper = accumulated.upper()
                for kw in _kws_by_len:
                    if kw in upper:
                        if kw in _AMBIGUOUS_PREFIXES:
                            # e.g. "GOTO_PARTIAL" matched before "_YES"/"_NO" arrived —
                            # keep accumulating until the full label is present.
                            break
                        result = _LLM_KEYWORD_MAP[kw]
                        log.info("CLASSIFY[stream-early] %s → %s (%d chars)",
                                 state, result, len(accumulated))
                        return result

        # Stream ended without early match — scan full accumulated text
        upper = accumulated.upper()
        for kw in _kws_by_len:
            if kw in upper:
                result = _LLM_KEYWORD_MAP[kw]
                log.info("CLASSIFY[stream-full] %s → %s", state, result)
                return result

    except Exception as exc:
        log.error("LLM classify error: %s", exc)

    log.info("CLASSIFY[default] %s → mark_unclear", state)
    return "mark_unclear"


# ─────────────────────────────────────────────────────────────────────────────
# Amount extraction
# ─────────────────────────────────────────────────────────────────────────────
async def llm_extract_amount(utterance: str) -> int | None:
    """LLM fallback — catches Hindi word-numerals regex misses (teen hazaar, etc.)."""
    try:
        resp = await oai_llm.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": (
                "Extract the rupee amount (integer) from this Hindi/Hinglish text.\n"
                "Examples: 'teen hazaar' → 3000, 'paanch sau' → 500, '₹4,500' → 4500\n"
                "Return ONLY the integer. If no amount present, write: NONE\n\n"
                f"Text: {utterance}\nAmount:"
            )}],
            temperature=0,
            max_tokens=15,
        )
        r = (resp.choices[0].message.content or "").strip()
        if not r or "NONE" in r.upper():
            return None
        m = re.search(r"\d+", r.replace(",", ""))
        result = int(m.group()) if m else None
        log.info("LLM_AMOUNT %r → %s", utterance[:50], result)
        return result
    except Exception as exc:
        log.warning("llm_extract_amount error: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Callback-time extraction
# ─────────────────────────────────────────────────────────────────────────────
async def extract_callback_time(utterance: str) -> str:
    """Extract a clean, speakable callback-time phrase from the utterance."""
    from utils import clean_callback_time
    try:
        resp = await oai_llm.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": (
                "Extract ONLY the callback time/day from this Hindi/Hinglish text as a short phrase.\n"
                "Good examples: 'आज रात 9 बजे', 'कल शाम 5 बजे', 'शनिवार को', 'अगले हफ्ते', 'कल'\n"
                "Do NOT include verbs like 'call karo', 'sampark karo', 'kijiye', 'dijiye' etc.\n"
                "Output ONLY the time phrase, nothing else. If completely unclear write: जल्द ही\n\n"
                f"Text: {utterance}\nTime phrase:"
            )}],
            temperature=0,
            max_tokens=25,
        )
        result = (resp.choices[0].message.content or "").strip().strip('।.,\'"')
        log.info("EXTRACT_TIME[llm] %r → %r", utterance[:60], result)
        return result if result else "जल्द ही"
    except Exception as exc:
        log.warning("extract_callback_time LLM error: %s", exc)
        return clean_callback_time(utterance)


# ─────────────────────────────────────────────────────────────────────────────
# FAQ detection
# ─────────────────────────────────────────────────────────────────────────────
import re as _re

# Quick keyword gate — only call LLM when these words appear (saves latency)
FAQ_TRIGGER_RE = _re.compile(
    r"kitni|kitna|कितनी|कितना|कितने|"
    r"due\s*date|emi\s*kab|kab\s*deni|"
    r"loan\s*id|account\s*number|loan\s*number|"
    r"EMI\s*hai|emi\s*amount|emi\s*kitni",
    _re.IGNORECASE | _re.UNICODE,
)


async def check_faq(utterance: str) -> str | None:
    """Returns FAQ script key or None."""
    try:
        resp = await oai_llm.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": (
                "Is this Hindi/Hinglish utterance asking a factual question about their loan?\n"
                "EMI_AMOUNT — asking about EMI amount (कितनी EMI है, emi kitni hai)\n"
                "DUE_DATE   — asking about due date (due date kya hai, kab deni thi)\n"
                "LOAN_ID    — asking about loan ID or account number\n"
                "NONE       — not a factual FAQ question\n\n"
                f"Utterance: {utterance}\nAnswer:"
            )}],
            temperature=0,
            max_tokens=15,
        )
        raw = (resp.choices[0].message.content or "").strip().upper()
        if "EMI_AMOUNT" in raw:
            return "faq_emi_amount"
        if "DUE_DATE" in raw:
            return "faq_due_date"
        if "LOAN_ID" in raw:
            return "faq_loan_id"
        return None
    except Exception as exc:
        log.warning("check_faq error: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Call variable finalisation
# ─────────────────────────────────────────────────────────────────────────────
async def finalize_call_variables(final_state: str, ctx: dict[str, str]) -> dict:
    """
    Normalise all captured call variables into structured data via LLM.
    Returns a dict with any of: pay_later_date, cannot_pay_reason,
    target_date, call_back_time, already_paid_date, summary.
    """
    today_str = _date.today().isoformat()
    raw: dict[str, str] = {}

    if final_state in ("future_confirm", "partial_confirm"):
        if ctx.get("payment_date"):
            raw["pay_later_date"] = ctx["payment_date"]
    if final_state == "already_paid_confirm":
        if ctx.get("payment_date"):
            raw["already_paid_date"] = ctx["payment_date"]
    if ctx.get("callback_time"):
        raw["call_back_time"] = ctx["callback_time"]
        raw["target_date"]    = ctx["callback_time"]
    if ctx.get("refusal_reason"):
        raw["cannot_pay_reason"] = ctx["refusal_reason"]

    customer = ctx.get("customer_name", "customer")
    phone    = ctx.get("phone_number", "")
    loan_id  = ctx.get("loan_id", "")
    emi      = ctx.get("emi_overdue_amt") or ctx.get("emi_amount", "")
    emi_date = ctx.get("emi_overdue_date") or ctx.get("emi_due_date", "")

    try:
        prompt = (
            f"EMI collection call ended. Today: {today_str}\n"
            f"Customer: {customer}, Phone: {phone}, Loan ID: {loan_id}\n"
            f"Overdue EMI: ₹{emi} (due {emi_date}), Final state: {final_state}\n"
            f"Captured data (raw): {json.dumps(raw, ensure_ascii=False)}\n\n"
            "Output a JSON object with ONLY the fields that apply to this final_state:\n"
            "  full_payment_today   → summary only. NEVER set already_paid_date here.\n"
            "  future_confirm       → pay_later_date (ISO), summary\n"
            "  partial_confirm      → pay_later_date (ISO), summary\n"
            "  already_paid_confirm → already_paid_date (ISO, from raw data), summary\n"
            "  callback_confirm     → call_back_time, target_date (ISO), summary\n"
            "  refusal_close        → cannot_pay_reason (5-10 word English), call_back_time, target_date (ISO), summary\n"
            "  death_response / no_response / unclear_close / stt_failure → summary only\n"
            "Field definitions:\n"
            "- pay_later_date: ISO date YYYY-MM-DD when customer promised to pay\n"
            "- cannot_pay_reason: 5-10 word English phrase explaining inability to pay\n"
            "- target_date: ISO date YYYY-MM-DD derived from the callback time phrase\n"
            "- call_back_time: callback time phrase exactly as captured\n"
            "- already_paid_date: ISO date YYYY-MM-DD of the claimed prior payment\n"
            "- summary: one-line English outcome summary\n"
            "Output ONLY valid JSON, no extra text."
        )
        resp = await oai_llm.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=200,
        )
        raw_out = (resp.choices[0].message.content or "").strip()
        m = re.search(r'\{.*?\}', raw_out, re.DOTALL)
        if m:
            result = json.loads(m.group())
            log.info("CALL_VARS %s", result)
            return result
    except Exception as exc:
        log.warning("finalize_call_variables error: %s", exc)

    return raw
