"""
scripts.py — Scripted bot lines, FSM state constants, customer defaults,
             and context builder.
"""
from __future__ import annotations
import os
from datetime import date as _date, timedelta
from utils import fmt_date

# ─────────────────────────────────────────────────────────────────────────────
# Default customer data  (.env fallbacks for testing; override per-call via API)
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_CUSTOMER: dict[str, str] = {
    # ── Core caller inputs ────────────────────────────────────────────────────
    "customer_name":       os.getenv("DEFAULT_CUSTOMER_NAME",       "Rahul"),
    "phone_number":        os.getenv("DEFAULT_PHONE_NUMBER",        ""),
    "loan_id":             os.getenv("DEFAULT_LOAN_ID",             "EH12345"),
    "emi_overdue_amt":     os.getenv("DEFAULT_EMI_OVERDUE_AMT",     "8,500"),
    "emi_overdue_amt_int": os.getenv("DEFAULT_EMI_OVERDUE_AMT_INT", "8500"),
    "emi_overdue_date":    os.getenv("DEFAULT_EMI_OVERDUE_DATE",    "5 March 2026"),

    # ── Supporting fields ─────────────────────────────────────────────────────
    "lender":              os.getenv("DEFAULT_LENDER",              "Easy Home Finance"),
    "loan_type":           os.getenv("DEFAULT_LOAN_TYPE",           "Home Loan"),
    "min_partial":         os.getenv("DEFAULT_MIN_PARTIAL",         "1,500"),
    "min_partial_int":     os.getenv("DEFAULT_MIN_PARTIAL_INT",     "1500"),
    "payment_deadline":    os.getenv("DEFAULT_PAYMENT_DEADLINE",    ""),

    # ── Backward-compat aliases ───────────────────────────────────────────────
    "emi_amount":     os.getenv("DEFAULT_EMI_OVERDUE_AMT",     "8,500"),
    "emi_amount_int": os.getenv("DEFAULT_EMI_OVERDUE_AMT_INT", "8500"),
    "emi_due_date":   os.getenv("DEFAULT_EMI_OVERDUE_DATE",    "5 March 2026"),
}


def build_default_ctx() -> dict[str, str]:
    """Return a fresh per-call context dict with all defaults filled in."""
    ctx = dict(DEFAULT_CUSTOMER)
    if not ctx.get("payment_deadline"):
        ctx["payment_deadline"] = fmt_date(_date.today() + timedelta(days=7))
    # Keep aliases in sync with primary fields
    if ctx.get("emi_overdue_amt"):
        ctx.setdefault("emi_amount",     ctx["emi_overdue_amt"])
    if ctx.get("emi_overdue_amt_int"):
        ctx.setdefault("emi_amount_int", ctx["emi_overdue_amt_int"])
    if ctx.get("emi_overdue_date"):
        ctx.setdefault("emi_due_date",   ctx["emi_overdue_date"])
    return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Mandatory closing (appended to several terminal scripts)
# ─────────────────────────────────────────────────────────────────────────────
_MANDATORY_CLOSING = (
    "भुगतान पूरा करने के लिए आपको भेजे गए सुरक्षित लिंक का उपयोग करें। "
    "कृपया {payment_deadline} तक शेष राशि चुकाने की कोशिश करें "
    "ताकि आपका क्रेडिट स्कोर सुरक्षित रहे। "
    "आपके सहयोग के लिए धन्यवाद, और आपका दिन शुभ हो।"
)


# ─────────────────────────────────────────────────────────────────────────────
# Scripted responses  (Hinglish)
# ─────────────────────────────────────────────────────────────────────────────
SCRIPTS: dict[str, str] = {
    # ── Opening ──────────────────────────────────────────────────────────────
    "opening": (
        "नमस्ते {customer_name} जी, मैं Aditi बोल रही हूँ Easy Home Finance से। "
        "आपकी {emi_amount} रुपये की EMI, जो {emi_due_date} को देय थी, अभी तक बाकी है। "
        "कृपया बताइए आप कब तक भुगतान कर पाएंगे?"
    ),

    # ── C1: Death ────────────────────────────────────────────────────────────
    "death_response": (
        "मुझे आपके नुकसान के लिए बहुत दुख है। "
        "इस कठिन समय में हम आपके साथ हैं। "
        "हमारी टीम का एक सदस्य जल्द ही आपसे व्यक्तिगत रूप से संपर्क करेगा। "
        "धन्यवाद।"
    ),

    # ── C2: Partial payment ──────────────────────────────────────────────────
    "offer_partial": (
        "मैं समझती हूँ कि आप अभी आर्थिक कठिनाई में हैं। "
        "क्या आप आज आंशिक भुगतान कर सकते हैं? "
        "न्यूनतम {min_partial} रुपये होना चाहिए।"
    ),
    "partial_ask_amount": (
        "धन्यवाद। आप आज कितनी राशि का भुगतान कर सकते हैं? "
        "न्यूनतम {min_partial} रुपये होना चाहिए।"
    ),
    "partial_amount_too_low": (
        "यह राशि न्यूनतम से कम है। "
        "कृपया {min_partial} रुपये या उससे अधिक बताइए।"
    ),
    "partial_amount_unclear": (
        "मुझे राशि समझ नहीं आई। "
        "कृपया रुपये में बताइए, जैसे दो हजार या तीन हजार।"
    ),
    "partial_ask_remaining_date": (
        "धन्यवाद। आज {partial_amount} रुपये के भुगतान के बाद "
        "{remaining_balance} रुपये शेष रहेंगे। "
        "आप यह बाकी राशि कब तक चुकाएंगे?"
    ),
    "partial_date_retry": (
        "यह तारीख मान्य नहीं लगी। "
        "कृपया एक भविष्य की तारीख बताइए जो नब्बे दिनों के अंदर हो।"
    ),
    "date_beyond_90": (
        "भुगतान EMI की due date से 90 दिनों के भीतर ही स्वीकार किया जा सकता है। "
        "कृपया इस अवधि के अंदर एक तारीख बताइए।"
    ),
    "partial_confirm": (
        "ठीक है, हमने आपकी भुगतान जानकारी नोट कर ली है। "
        + _MANDATORY_CLOSING
    ),

    # ── C3: Future promise ────────────────────────────────────────────────────
    "future_ask_date":      "धन्यवाद। आप भुगतान किस तारीख तक कर पाएंगे?",
    "future_date_retry": (
        "यह तारीख मान्य नहीं लगी। "
        "कृपया एक भविष्य की तारीख बताइए जो नब्बे दिनों के अंदर हो।"
    ),
    "future_date_beyond_90": (
        "भुगतान EMI की due date से 90 दिनों के भीतर ही मान्य होगा। "
        "कृपया इस सीमा के अंदर एक तारीख बताइए, जैसे अगले महीने या दो महीने बाद।"
    ),
    "future_confirm": (
        "ठीक है, हमने आपकी भुगतान तारीख {payment_date} नोट कर ली है। "
        + _MANDATORY_CLOSING
    ),

    # ── C4: Full payment today ────────────────────────────────────────────────
    "full_payment_today": (
        "धन्यवाद। कृपया SMS के माध्यम से भेजे गए सुरक्षित लिंक का उपयोग करके "
        "या अपनी नजदीकी शाखा में जाकर आज भुगतान पूरा करें। "
        "आपका दिन शुभ हो।"
    ),

    # ── C5: Refusal ───────────────────────────────────────────────────────────
    "refusal_ask_reason":  "समझ गई। क्या आप बता सकते हैं कि आप भुगतान क्यों नहीं कर पा रहे हैं?",
    "refusal_escalate": (
        "मैं समझती हूँ। "
        "क्या कोई विशेष कारण है जिसकी वजह से आप अभी भुगतान नहीं कर पा रहे?"
    ),
    "refusal_credit_warn": (
        "ठीक है, हमने आपकी बात नोट कर ली है। "
        "कृपया ध्यान रखें कि बकाया EMI से जुर्माना और CIBIL स्कोर पर असर पड़ सकता है। "
        "आप चाहेंगे कि हम आपसे दोबारा कब संपर्क करें?"
    ),
    "refusal_close": (
        "ठीक है, हम आपसे {callback_time} संपर्क करेंगे। "
        "कृपया {payment_deadline} तक भुगतान करने की कोशिश करें — "
        "इससे आपका CIBIL स्कोर सुरक्षित रहेगा। "
        "आपका दिन शुभ हो।"
    ),

    # ── C6: Already paid ──────────────────────────────────────────────────────
    "already_paid_ask_date":    "कृपया बताइए कि आपने भुगतान किस तारीख को किया था?",
    "already_paid_date_invalid": (
        "यह भविष्य की तारीख है। "
        "कृपया वह सही तारीख बताइए जब आपने वास्तव में भुगतान किया था।"
    ),
    "already_paid_date_unclear": "तारीख समझ नहीं आई। कृपया फिर से बताइए।",
    "already_paid_ask_mode": "भुगतान किस माध्यम से किया था?",
    "already_paid_confirm": (
        "धन्यवाद। हमने आपकी जानकारी प्राप्त कर ली है। "
        "हम इसे सत्यापित करके अपने रिकॉर्ड अपडेट कर देंगे। "
        "आपका दिन शुभ हो।"
    ),

    # ── C7: Callback ──────────────────────────────────────────────────────────
    "callback_ask_time":    "कोई बात नहीं। आप बताइए कि मैं आपको कब कॉल करूँ?",
    "callback_time_unclear": (
        "समय समझ नहीं आया। कृपया फिर से बताइए, जैसे 'कल दोपहर' या 'तीन बजे'।"
    ),
    "callback_confirm": (
        "ठीक है, हम आपसे {callback_time} संपर्क करेंगे। "
        "कृपया कोशिश करें कि EMI जल्दी चुका दें ताकि जुर्माना न लगे।"
    ),

    # ── FAQ answers ───────────────────────────────────────────────────────────
    "faq_emi_amount": "आपकी EMI {emi_amount} रुपये है, जो {emi_due_date} को देय थी।",
    "faq_due_date":   "आपकी EMI की due date {emi_due_date} थी।",
    "faq_loan_id":    "आपका Loan ID {loan_id} है।",

    # ── Beyond-90-day penalty close ───────────────────────────────────────────
    "beyond_90_penalty_close": (
        "हम समझते हैं, लेकिन नियमों के अनुसार भुगतान EMI due date के 90 दिनों के बाद "
        "स्वीकार नहीं किया जा सकता। "
        "इस अवधि के बाद जुर्माना लगेगा और आपके CIBIL स्कोर पर गंभीर नकारात्मक प्रभाव पड़ेगा। "
        "हमारी टीम जल्द ही आपसे व्यक्तिगत रूप से संपर्क करेगी। "
        "धन्यवाद, आपका दिन शुभ हो।"
    ),

    # ── Unclear handler ───────────────────────────────────────────────────────
    "unclear_sm1": (
        "माफी चाहती हूँ, आवाज़ साफ नहीं आई। "
        "क्या आप आज EMI भुगतान कर पाएंगे — हाँ या नहीं?"
    ),
    "unclear_sm2": (
        "एक बार और कोशिश करते हैं — "
        "बस हाँ या नहीं बोलिए, क्या आज EMI भर पाएंगे?"
    ),
    "unclear_close": (
        "ठीक है, लगता है अभी बात नहीं हो पा रही। "
        "हम जल्द दोबारा संपर्क करेंगे। "
        "धन्यवाद, आपका दिन शुभ हो।"
    ),
}

# ── Terminal states — speak and hang up ──────────────────────────────────────
TERMINAL: set[str] = {
    "death_response", "partial_confirm", "future_confirm",
    "full_payment_today", "refusal_close", "already_paid_confirm",
    "callback_confirm", "unclear_close", "beyond_90_penalty_close",
}

# States where barge-in is suppressed (opening must play in full)
BARGE_IN_LOCKED: set[str] = TERMINAL | {"opening"}

# States that auto-advance to "opening" after speaking (no user input needed)
AUTO_ADVANCE: dict[str, str] = {
    "unclear_sm1": "opening",
    "unclear_sm2": "opening",
}
