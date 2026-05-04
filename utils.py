"""
utils.py — Date/amount parsing, formatting and callback-time helpers.
"""
from __future__ import annotations
import re
import sys
from datetime import date as _date, timedelta

# ─────────────────────────────────────────────────────────────────────────────
# Month name → number map  (Hindi romanised + English)
# ─────────────────────────────────────────────────────────────────────────────
_MONTH_MAP: dict[str, int] = {
    "jan": 1, "january": 1, "janvari": 1,
    "feb": 2, "february": 2, "farvari": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5, "mai": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "sitambar": 9,
    "oct": 10, "october": 10, "aktubar": 10,
    "nov": 11, "november": 11, "navambar": 11,
    "dec": 12, "december": 12, "disambar": 12,
}


def parse_date(text: str) -> _date | None:
    """Extract a date from a Hindi/Hinglish utterance. Returns None if unparseable."""
    t = text.lower().strip()
    today = _date.today()

    # Devanagari → Roman so the regexes match both scripts
    t = (t.replace("आज", "aaj")
          .replace("कल", "kal")
          .replace("परसों", "parso").replace("परसो", "parso")
          .replace("अगले साल", "agle saal").replace("अगले वर्ष", "agle saal")
          .replace("अगली साल", "agle saal").replace("अगली वर्ष", "agle saal")  # feminine form
          .replace("अगले हफ़्ते", "agle hafte")
          .replace("अगले हफ्ते", "agle hafte")
          .replace("अगली हफ्ते", "agle hafte").replace("अगली हफ़्ते", "agle hafte")
          .replace("इस हफ्ते", "is hafte").replace("इस हफ़्ते", "is hafte")
          .replace("अगले महीने", "agle mahine").replace("अगली महीने", "agle mahine")
          .replace("महीने में", "mahine mein").replace("महीने बाद", "mahine baad")
          .replace("हफ़्ते में", "hafte mein").replace("हफ्ते में", "hafte mein")
          .replace("दिनों में", "dinon mein").replace("दिन में", "din mein")
          .replace("जनवरी", "january")
          .replace("फरवरी", "february").replace("फ़रवरी", "february")
          .replace("मार्च", "march")
          .replace("अप्रैल", "april")
          .replace("मई", "may")
          .replace("जून", "june")
          .replace("जुलाई", "july")
          .replace("अगस्त", "august")
          .replace("सितंबर", "september").replace("सितम्बर", "september")
          .replace("अक्टूबर", "october").replace("अक्तूबर", "october")
          .replace("नवंबर", "november").replace("नवम्बर", "november")
          .replace("दिसंबर", "december").replace("दिसम्बर", "december"))

    if re.search(r"\baaj\b|today", t):
        return today
    if re.search(r"\bkal\b|tomorrow", t):
        return today + timedelta(days=1)
    if re.search(r"\bparso\b|parson\b", t):
        return today + timedelta(days=2)
    if re.search(r"agle\s+saal|next\s+year", t):
        return today + timedelta(days=366)
    if re.search(r"agle\s+hafte|next\s+week", t):
        return today + timedelta(days=7)
    if re.search(r"agle\s+mahine|next\s+month|mahine\s+(?:mein|baad)", t):
        return today + timedelta(days=30)

    # "2-3 din" → take first
    m = re.search(r"(\d+)\s*[-–]\s*\d+\s*din", t)
    if m:
        return today + timedelta(days=int(m.group(1)))

    # "X din" / "X days"
    m = re.search(r"(\d+)\s*(?:din\b|days?\b)", t)
    if m:
        return today + timedelta(days=int(m.group(1)))

    # "15 March" / "March 15" (with optional year)
    for name, num in _MONTH_MAP.items():
        # \b after name prevents "mar" matching within "march", "oct" within "october", etc.
        m = re.search(rf"(\d{{1,2}})\s+{name}\b(?:\s+(\d{{4}}))?", t)
        if m:
            day, year = int(m.group(1)), int(m.group(2)) if m.group(2) else today.year
            try:
                d = _date(year, num, day)
                if d < today and not m.group(2):
                    d = _date(year + 1, num, day)
                return d
            except ValueError:
                return None
        m = re.search(rf"{name}\b\s+(\d{{1,2}})(?:\s+(\d{{4}}))?", t)
        if m:
            day, year = int(m.group(1)), int(m.group(2)) if m.group(2) else today.year
            try:
                d = _date(year, num, day)
                if d < today and not m.group(2):
                    d = _date(year + 1, num, day)
                return d
            except ValueError:
                return None

    # Bare number 1–31 → treat as day of current/next month
    m = re.search(r"\b(\d{1,2})\b", t)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            try:
                d = _date(today.year, today.month, day)
                if d <= today:
                    nm = today.month % 12 + 1
                    ny = today.year + (1 if today.month == 12 else 0)
                    d  = _date(ny, nm, day)
                return d
            except ValueError:
                pass

    return None


def parse_amount(text: str) -> int | None:
    """Extract a rupee amount (integer) from Hindi/Hinglish speech."""
    t = (text.lower()
         .replace(",", "")
         .replace("₹", " ")
         .replace("rs.", " ")
         .replace("rs ", " ")
         .replace("रुपये", " ").replace("रुपए", " ").replace("रूपये", " ")
         .replace("rupaye", " ").replace("rupees", " ").replace("rupiya", " ")
         .replace("हज़ार", "hazaar").replace("हजार", "hazaar")
         .replace("सौ", "sau").replace("सो", "sau")
         .replace("लाख", "lakh"))

    # Decimal-aware lakh and hazaar — e.g. "1.5 lakh" → 150000, "2.5 hazaar" → 2500
    m = re.search(r"(\d+(?:\.\d+)?)\s*lakh", t)
    if m:
        return int(float(m.group(1)) * 100_000)

    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:hazaar|hazar|hajar|thousand)\s*(?:(\d+)\s*(?:sau|so|hundred))?", t)
    if m:
        base = int(float(m.group(1)) * 1000)
        extra = int(m.group(2)) * 100 if m.group(2) else 0
        return base + extra

    m = re.search(r"(\d+)\s*(?:sau|so|hundred)", t)
    if m:
        return int(m.group(1)) * 100

    m = re.search(r"\b(\d{3,6})\b", t)
    if m:
        return int(m.group(1))

    return None


def fmt_date(d: _date) -> str:
    """Format date without leading zero — cross-platform (Windows & Linux)."""
    fmt = "%#d %B %Y" if sys.platform == "win32" else "%-d %B %Y"
    return d.strftime(fmt)


# ─────────────────────────────────────────────────────────────────────────────
# Callback-time helpers
# ─────────────────────────────────────────────────────────────────────────────
_TRAILING_VERBS = re.compile(
    r"\s*(?:करना|करूंगा|करूँगा|कर दूंगा|कर दूँगा|कर दें|कर दीजिए|कर दीजिएगा"
    r"|कीजिएगा|कीजिए|करिएगा|करिए|कर लीजिए|कर लीजिएगा"
    r"|हो जाएगा|हो जायेगा|बाद में|ठीक है|okay|ok)[।.,\s]*$",
    re.IGNORECASE,
)

_BARE_ACK = re.compile(
    r"^(haan?|han|ha|nahi?|nai|okay?|ok|theek\s*hai|bilkul|zaroor|"
    r"ji+|sahi\s*hai|acha|accha|"
    r"हाँ?|हां|नहीं|नहीं|ठीक\s*है|बिल्कुल|जरूर|जी+|अच्छा|सही\s*है)[।.,!?\s]*$",
    re.IGNORECASE | re.UNICODE,
)

_TIME_WORD = re.compile(
    r"\d"
    r"|kal\b|parso\b|agle\b|agli\b|hafte?\b|mahine?\b|ghante?\b|baje?\b|saal\b|"
    r"shaam\b|raat\b|subah\b|dopahar\b|savere\b|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"somvar|mangal|budhvar|gurvar|shukra|shanivar|ravivar|"
    r"week|month|year|morning|evening|night|afternoon|hour|minute|"
    r"कल|परसों|अगले|अगली|हफ्ते?|महीने?|घंटे?|बजे?|साल|वर्ष|"
    r"शाम|रात|सुबह|दोपहर|सवेरे|"
    r"सोमवार|मंगलवार|बुधवार|गुरुवार|शुक्रवार|शनिवार|रविवार|"
    r"दिन\b|सप्ताह|तारीख",
    re.IGNORECASE | re.UNICODE,
)


def clean_callback_time(text: str) -> str:
    """Strip trailing filler verbs from a callback-time utterance."""
    cleaned = _TRAILING_VERBS.sub("", text.strip()).strip("।., ")
    return cleaned if cleaned else text[:60]


def is_callback_time(text: str) -> bool:
    """Return True only if the utterance plausibly contains a time/date reference."""
    t = text.strip()
    if not t or bool(_BARE_ACK.match(t)):
        return False
    return bool(_TIME_WORD.search(t))
