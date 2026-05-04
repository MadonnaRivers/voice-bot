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
    "भुगतान पूरा करने के लिए आप दिए गए सुरक्षित भुगतान लिंक का उपयोग कर सकते हैं। "
    "यदि संभव हो, तो कृपया सुनिश्चित करें कि शेष राशि {target_date} तक चुका दी जाए ताकि आपका क्रेडिट स्कोर सुरक्षित रहे। "
    "आपके सहयोग के लिए धन्यवाद, और आपका दिन शुभ हो।"
)


# ─────────────────────────────────────────────────────────────────────────────
# Scripted responses  (Hinglish/Hindi)
# ─────────────────────────────────────────────────────────────────────────────
SCRIPTS: dict[str, str] = {
    # ── Opening ──────────────────────────────────────────────────────────────
    "opening": (
        "नमस्ते {customer_name} जी, मैं Aditi बोल रही हूँ Easy Home Finance से। "
        "आपकी {emi_amount} रुपये की EMI, जो {emi_due_date} को देय थी, अभी तक बाकी है। "
        "कृपया बताइए आप कब तक भुगतान कर पाएंगे?"
    ),

    # ── Deceased Report ──────────────────────────────────────────────────────
    "death_response": (
        "मुझे आपके नुकसान के लिए वास्तव में बहुत दुख है। "
        "कृपया इस कठिन समय में हमारी हार्दिक संवेदना स्वीकार करें। "
        "हमारी टीम का एक समर्पित सदस्य आगे आपकी सहायता के लिए व्यक्तिगत रूप से आपसे संपर्क करेगा। "
        "हमें बताने के लिए धन्यवाद।"
    ),

    # ── Partial payment ──────────────────────────────────────────────────
    "offer_partial": (
        "ठीक है, आंशिक भुगतान (partial payment) के लिए सहमत होने के लिए धन्यवाद। "
        "कृपया मुझे बताएं कि आप आज कितना भुगतान कर सकते हैं? "
        "न्यूनतम आंशिक भुगतान राशि ₹1,500 है।"
    ),
    "partial_ask_amount": (
        "कृपया मुझे बताएं कि आप आज कितना भुगतान कर सकते हैं? "
        "न्यूनतम आंशिक भुगतान राशि ₹1,500 है।"
    ),
    "partial_amount_too_low": (
        "आंशिक भुगतान ₹1,500 से कम स्वीकार्य नहीं है। "
        "कृपया एक वैध राशि साझा करें।"
    ),
    "partial_amount_unclear": (
        "माफी चाहती हूँ, मुझे राशि समझ नहीं आई। "
        "कृपया बताएं कि आप आज कितना भुगतान कर पाएंगे? न्यूनतम राशि ₹1,500 है।"
    ),
    "partial_ask_remaining_date": (
        "ठीक है, नोट कर लिया गया है। आज {partial_amount} रुपये के भुगतान के बाद, "
        "आपकी शेष राशि ₹{remaining_balance} होगी। "
        "आप शेष राशि कब तक चुकाएंगे?"
    ),
    "partial_date_retry": (
        "यह तारीख मान्य नहीं लगी। "
        "कृपया एक वैध तारीख बताएं जो due date के 90 दिनों के भीतर हो।"
    ),
    "date_beyond_90": (
        "क्षमा करें, यह तारीख स्वीकार्य नहीं है। "
        "कृपया due date से 90 दिनों के भीतर की कोई तारीख बताएं।"
    ),
    "partial_confirm": (
        "ठीक है, नोट कर लिया गया है। हमने आपकी भुगतान तारीख रिकॉर्ड कर ली है। "
        + _MANDATORY_CLOSING
    ),

    # ── Future promise ────────────────────────────────────────────────────
    "future_ask_date": (
        "पुष्टि करने के लिए धन्यवाद। आप किस तारीख तक भुगतान कर पाएंगे?"
    ),
    "future_date_retry": (
        "क्षमा करें, यह तारीख मान्य नहीं है। कृपया एक सही तारीख बताएं।"
    ),
    "future_date_beyond_90": (
        "क्षमा करें, यह तारीख स्वीकार्य नहीं है। "
        "कृपया due date से 90 दिनों के भीतर की कोई तारीख बताएं।"
    ),
    "future_confirm": (
        "ठीक है, नोट कर लिया गया है। हमने आपकी भुगतान तारीख रिकॉर्ड कर ली है। "
        "कृपया सुनिश्चित करें कि भुगतान इस तारीख तक हो जाए ताकि आपके क्रेडिट स्कोर पर कोई असर न पड़े। "
        + _MANDATORY_CLOSING
    ),

    # ── Full payment today ────────────────────────────────────────────────
    "full_payment_today": (
        "पुष्टि करने के लिए धन्यवाद। कृपया SMS के माध्यम से साझा किए गए भुगतान विकल्प का उपयोग करके "
        "या अपनी नजदीकी शाखा में जाकर आज ही EMI भुगतान पूरा करें। "
        "कृपया सुनिश्चित करें कि शेष राशि {payment_date} तक चुका दी जाए ताकि आपका क्रेडिट स्कोर सुरक्षित रहे और पेनल्टी चार्ज से बचा जा सके। "
        "आपके सहयोग के लिए धन्यवाद, और आपका दिन शुभ हो।"
    ),

    # ── Cannot Pay (Refusal) ─────────────────────────────────────────────────
    "refusal_ask_reason": (
        "बताने के लिए धन्यवाद। क्या मैं पूछ सकती हूँ कि आप अभी भुगतान क्यों नहीं कर पा रहे हैं?"
    ),
    "refusal_escalate": (
        "यह जानकारी हमें आपकी बेहतर सहायता करने में मदद करती है। "
        "क्या कोई विशेष वजह है जिसके लिए आप भुगतान नहीं कर पा रहे?"
    ),
    "refusal_credit_warn": (
        "ठीक है, नोट कर लिया गया है। हमने इसे अपने सिस्टम में रिकॉर्ड कर लिया है। "
        "कृपया ध्यान दें कि भुगतान न करने से आपके क्रेडिट स्कोर पर असर पड़ सकता है और पेनल्टी चार्ज लग सकते हैं। "
        "आप क्या चाहेंगे कि हम आपसे दोबारा आपके EMI भुगतान के संबंध में कब संपर्क करें?"
    ),
    "refusal_close": (
        "ठीक है, नोट कर लिया गया है। हमने इसे अपने सिस्टम में रिकॉर्ड कर लिया है। "
        + _MANDATORY_CLOSING
    ),

    # ── Already paid ──────────────────────────────────────────────────────
    "already_paid_ask_date": (
        "हमें बताने के लिए धन्यवाद। हमारे रिकॉर्ड को सत्यापित करने के लिए, "
        "कृपया मुझे वह तारीख बताएं जिस दिन आपने बकाया EMI का भुगतान किया था?"
    ),
    "already_paid_date_invalid": (
        "क्षमा करें, आपके द्वारा साझा की गई तारीख भविष्य की है और मान्य नहीं है। "
        "कृपया वह तारीख साझा करें जो आज या उससे पहले की हो।"
    ),
    "already_paid_date_unclear": (
        "माफी चाहती हूँ, तारीख समझ नहीं आई। कृपया फिर से बताएं।"
    ),
    "already_paid_ask_mode": (
        "पुष्टि करने के लिए धन्यवाद। क्या आप मुझे बता सकते हैं कि आपने किस भुगतान माध्यम का उपयोग किया था, "
        "जैसे कि SMS, शाखा जाकर, या मोबाइल ऐप के माध्यम से?"
    ),
    "already_paid_confirm": (
        "विवरण के लिए धन्यवाद। हमें आपकी भुगतान जानकारी प्राप्त हो गई है और हम अपने रिकॉर्ड को सत्यापित और अपडेट करेंगे। "
        "यदि हमें किसी और चीज़ की आवश्यकता होगी, तो हम आपसे संपर्क करेंगे। धन्यवाद।"
    ),

    # ── Callback ──────────────────────────────────────────────────────────
    "callback_ask_time": (
        "कोई समस्या नहीं है, मैं समझती हूँ। "
        "कृपया मुझे कॉल बैक के लिए कोई सुविधाजनक तारीख या समय बताएं।"
    ),
    "callback_time_unclear": (
        "समय समझ नहीं आया। कृपया फिर से बताइए।"
    ),
    "callback_confirm": (
        "धन्यवाद। आपने जो समय बताया है, हम उस समय आपको कॉल करेंगे। "
        "कृपया पेनल्टी चार्ज से बचने के लिए जल्द से जल्द बकाया EMI चुकाने की कोशिश करें। "
        "आपका दिन शुभ हो।"
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
