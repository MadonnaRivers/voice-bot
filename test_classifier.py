"""
test_classifier.py — Comprehensive classifier accuracy test for Aditi EMI Bot.

Tests all 7 collection conditions with real-world Hindi/Hinglish utterances.
Run: python test_classifier.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

from classifier import classify   # updated import for new module structure

# ─────────────────────────────────────────────────────────────────────────────
# (utterance, state, expected_label, description)
# ─────────────────────────────────────────────────────────────────────────────
TEST_CASES: list[tuple[str, str, str, str]] = [

    # ══════════════════════════════════════════════════════════════════════════
    # STATE: opening
    # ══════════════════════════════════════════════════════════════════════════

    # ── C4: Full payment today ────────────────────────────────────────────────
    ("हाँ, करूंगा",                                   "opening", "goto_pay_now", "Generic yes"),
    ("ठीक है, आज कर दूंगा",                           "opening", "goto_pay_now", "Today explicit"),
    ("बिल्कुल, अभी pay करता हूँ",                     "opening", "goto_pay_now", "Immediate"),
    ("हाँ हाँ, ठीक है",                               "opening", "goto_pay_now", "Double yes"),
    ("okay करूंगा",                                    "opening", "goto_pay_now", "Hinglish yes"),
    ("जी हाँ भर दूंगा",                               "opening", "goto_pay_now", "Ji haan"),
    ("अभी NEFT कर देता हूँ",                          "opening", "goto_pay_now", "NEFT now"),
    ("Google Pay से कर देता हूँ",                     "opening", "goto_pay_now", "GPay now"),
    ("शाम तक कर दूंगा",                               "opening", "goto_pay_now", "By evening today"),
    ("हाँ, आज ही transfer करूंगा",                    "opening", "goto_pay_now", "Transfer today"),
    ("ऑनलाइन कर दूंगा आज",                           "opening", "goto_pay_now", "Online today"),
    ("ji bilkul aaj kar dunga",                        "opening", "goto_pay_now", "Roman Hindi yes"),

    # ── C2: Partial payment ───────────────────────────────────────────────────
    ("थोड़ा दे सकता हूँ",                             "opening", "goto_partial", "Explicit partial"),
    ("आधा अभी दे देता हूँ",                           "opening", "goto_partial", "Half now"),
    ("कुछ amount आज दे सकता हूँ",                     "opening", "goto_partial", "Some amount today"),
    ("पूरा नहीं, थोड़ा-थोड़ा करके देता हूँ",         "opening", "goto_partial", "Installment style"),
    ("3000 अभी दे सकता हूँ",                          "opening", "goto_partial", "Specific partial amount"),
    ("आधी EMI आज दे दूंगा",                           "opening", "goto_partial", "Half EMI"),
    ("partial payment कर सकता हूँ",                   "opening", "goto_partial", "Partial English"),
    ("कुछ पैसे आज, बाकी बाद में",                    "opening", "goto_partial", "Some now rest later"),
    ("दो हज़ार दे सकता हूँ अभी",                     "opening", "goto_partial", "2000 partial"),

    # ── C3: Future payment promise ────────────────────────────────────────────
    ("अगले हफ्ते तक कर दूंगा",                       "opening", "goto_future_promise", "Next week"),
    ("10 तारीख को करूंगा",                            "opening", "goto_future_promise", "Specific date"),
    ("कल तक भेज दूंगा",                               "opening", "goto_future_promise", "By tomorrow"),
    ("अगले महीने कर दूंगा",                           "opening", "goto_future_promise", "Next month"),
    ("3 दिन में कर दूंगा",                            "opening", "goto_future_promise", "In 3 days"),
    ("इस हफ्ते में हो जाएगा",                         "opening", "goto_future_promise", "This week"),
    ("थोड़ा वक्त दीजिए",                              "opening", "goto_future_promise", "Give time"),
    ("सैलरी आने पर कर दूंगा",                        "opening", "goto_future_promise", "After salary"),
    ("2-3 दिन में हो जाएगा",                          "opening", "goto_future_promise", "2-3 days"),
    ("परसों तक कर देता हूँ",                          "opening", "goto_future_promise", "Day after tomorrow"),
    ("15 तारीख तक भेज दूंगा",                        "opening", "goto_future_promise", "15th date"),
    ("शुक्रवार को कर दूंगा",                          "opening", "goto_future_promise", "Friday"),
    ("इस महीने की 20 तारीख को",                      "opening", "goto_future_promise", "20th this month"),
    ("कुछ दिन रुकिए, हो जाएगा",                      "opening", "goto_future_promise", "Wait a few days"),
    ("अगले हफ्ते salary आएगी तब कर दूंगा",           "opening", "goto_future_promise", "Salary next week"),

    # ── C6: Already paid ──────────────────────────────────────────────────────
    ("पहले ही दे दिया",                               "opening", "goto_already_paid", "Already paid"),
    ("हो गया पेमेंट",                                 "opening", "goto_already_paid", "Payment done"),
    ("UPI से भेज दिया",                               "opening", "goto_already_paid", "Paid via UPI"),
    ("कर दिया है",                                    "opening", "goto_already_paid", "Done already"),
    ("हाँ paid है, पिछले हफ्ते",                      "opening", "goto_already_paid", "Paid last week"),
    ("मैंने तो कल ही भेज दिया था",                   "opening", "goto_already_paid", "Sent yesterday"),
    ("Google Pay से transfer हो गया",                 "opening", "goto_already_paid", "GPay transferred"),
    ("bank से NEFT कर दिया था",                       "opening", "goto_already_paid", "NEFT done"),
    ("receipt भी है मेरे पास",                        "opening", "goto_already_paid", "Has receipt"),
    ("3 दिन पहले clear हो गया था",                    "opening", "goto_already_paid", "Cleared 3 days ago"),
    ("online kar diya tha maine",                      "opening", "goto_already_paid", "Roman Hindi paid"),
    ("भुगतान हो चुका है",                             "opening", "goto_already_paid", "Payment completed"),

    # ── C7: Busy / callback ───────────────────────────────────────────────────
    ("मैं अभी बाहर हूँ, बाद में बात करूंगा",         "opening", "goto_callback", "Outside + later"),
    ("अभी बाहर हूँ तो बात नहीं कर पाऊंगा",          "opening", "goto_callback", "Original bug case"),
    ("बाद में call करो",                               "opening", "goto_callback", "Call later"),
    ("व्यस्त हूँ अभी",                                "opening", "goto_callback", "Busy"),
    ("बात नहीं कर पाऊंगा अभी",                       "opening", "goto_callback", "Can't talk now"),
    ("बाद में बात करते हैं",                          "opening", "goto_callback", "Talk later"),
    ("अभी time नहीं है",                              "opening", "goto_callback", "No time now"),
    ("drive कर रहा हूँ अभी",                         "opening", "goto_callback", "Driving"),
    ("और मैं अभी तो नहीं कर पाऊंगा",                "opening", "goto_callback", "Temporal not-now"),
    ("meeting में हूँ, baad mein baat karte hain",    "opening", "goto_callback", "In meeting"),
    ("ऑफिस में हूँ, शाम को call करो",                "opening", "goto_callback", "In office"),
    ("अभी खाना खा रहा हूँ, थोड़ी देर में",          "opening", "goto_callback", "Eating"),
    ("बच्चों को school छोड़ने जा रहा हूँ",          "opening", "goto_callback", "Dropping kids"),
    ("थोड़ी देर में call back करूं?",                "opening", "goto_callback", "Will call back"),
    ("अभी मैं गाड़ी चला रहा हूँ",                   "opening", "goto_callback", "Driving Hindi"),
    ("बाद में बात होगी",                              "opening", "goto_callback", "Will talk later"),

    # ── C1: Death ─────────────────────────────────────────────────────────────
    ("घर में मौत हो गई",                             "opening", "goto_death", "Death in family"),
    ("मेरे पापा गुज़र गए",                            "opening", "goto_death", "Father passed"),
    ("उनका निधन हो गया",                              "opening", "goto_death", "Passed away"),
    ("वो नहीं रहे",                                   "opening", "goto_death", "Not alive anymore"),
    ("मेरी माँ का देहांत हो गया",                    "opening", "goto_death", "Mother passed"),
    ("घर में शोक है अभी",                            "opening", "goto_death", "In mourning"),
    ("account holder का इंतकाल हो गया",              "opening", "goto_death", "Account holder died"),
    ("पति नहीं रहे",                                  "opening", "goto_death", "Husband passed"),

    # ── C5a: Financial difficulty ──────────────────────────────────────────────
    ("पैसे नहीं हैं",                                 "opening", "goto_financial_difficulty", "No money"),
    ("अभी मुश्किल है",                                "opening", "goto_financial_difficulty", "Difficulty"),
    ("नहीं कर पाऊंगा भुगतान",                        "opening", "goto_financial_difficulty", "Can't pay"),
    ("तकलीफ़ में हूँ",                                "opening", "goto_financial_difficulty", "In trouble"),
    ("नहीं",                                          "opening", "goto_financial_difficulty", "Bare no"),
    ("नहीं हो पाएगा, पैसे की तंगी है",              "opening", "goto_financial_difficulty", "Tight on money"),
    ("नौकरी गई है, कैसे भरूं",                       "opening", "goto_financial_difficulty", "Lost job"),
    ("account में पैसे ही नहीं हैं",                 "opening", "goto_financial_difficulty", "No balance"),
    ("बहुत परेशानी है इस वक्त",                      "opening", "goto_financial_difficulty", "Very troubled"),
    ("EMI भरने की हालत में नहीं हूँ",                "opening", "goto_financial_difficulty", "Not in position"),
    ("paise nahi hain abhi",                           "opening", "goto_financial_difficulty", "Roman Hindi broke"),
    ("कर्ज़ में दबा हूँ",                             "opening", "goto_financial_difficulty", "In debt"),

    # ── C5b: Hard refusal ─────────────────────────────────────────────────────
    ("बिल्कुल नहीं करूंगा",                          "opening", "goto_refusal", "Hard refusal"),
    ("बंद करो यह सब",                                 "opening", "goto_refusal", "Stop this"),
    ("मुझे कोई मतलब नहीं",                           "opening", "goto_refusal", "Don't care"),
    ("नहीं दूंगा",                                    "opening", "goto_refusal", "Won't give"),
    ("court में ले जाओ जो करना है",                  "opening", "goto_refusal", "Court threat"),
    ("harassment बंद करो",                             "opening", "goto_refusal", "Harassment claim"),
    ("मत करो call, नहीं भरूंगा",                     "opening", "goto_refusal", "Don't call + won't pay"),
    ("legal action लूंगा मैं",                       "opening", "goto_refusal", "Legal threat"),
    ("RBI में complaint करूंगा",                      "opening", "goto_refusal", "RBI complaint"),

    # ── Tricky disambiguation ─────────────────────────────────────────────────
    ("कर दूंगा",                                      "opening", "goto_pay_now",              "Kar dunga = pay now NOT future"),
    ("हाँ करूंगा पर थोड़ा time दो",                  "opening", "goto_future_promise",        "Yes but give time"),
    ("अभी नहीं कर पाऊंगा",                           "opening", "goto_callback",              "Temporal can't = callback"),
    ("नहीं भर पाऊंगा",                               "opening", "goto_financial_difficulty",  "Can't pay = difficulty NOT refusal"),
    ("बाद में",                                       "opening", "goto_callback",              "Baad mein = callback"),
    ("thoda waqt do",                                  "opening", "goto_future_promise",        "Give time = future promise"),
    ("salary aane do",                                 "opening", "goto_future_promise",        "Wait for salary"),

    # ══════════════════════════════════════════════════════════════════════════
    # STATE: offer_partial
    # ══════════════════════════════════════════════════════════════════════════

    # Accept
    ("हाँ, थोड़ा दे सकता हूँ",                       "offer_partial", "goto_partial_yes", "Accept partial"),
    ("ठीक है, कुछ दे देता हूँ",                      "offer_partial", "goto_partial_yes", "Accept some"),
    ("हाँ, okay",                                     "offer_partial", "goto_partial_yes", "Simple yes"),
    ("कर सकता हूँ partial",                           "offer_partial", "goto_partial_yes", "Can do partial"),
    ("हाँ 2000 दे सकता हूँ",                          "offer_partial", "goto_partial_yes", "Amount + yes"),
    ("ठीक है, 3 हज़ार अभी",                           "offer_partial", "goto_partial_yes", "3k now"),
    ("चलो, आधा दे देता हूँ",                          "offer_partial", "goto_partial_yes", "Half"),

    # Callback
    ("नहीं, मैं बाद में बात करूंगा",                 "offer_partial", "goto_callback", "Call later"),
    ("बाहर हूँ, बाद में",                             "offer_partial", "goto_callback", "Outside+later"),
    ("अभी नहीं, कल बात करते हैं",                   "offer_partial", "goto_callback", "Tomorrow"),
    ("फिर बात करते हैं",                              "offer_partial", "goto_callback", "Talk again"),

    # Decline
    ("नहीं, बिल्कुल नहीं",                           "offer_partial", "goto_partial_no", "Hard no"),
    ("संभव नहीं है",                                  "offer_partial", "goto_partial_no", "Not possible"),
    ("नहीं कर सकता",                                 "offer_partial", "goto_partial_no", "Can't do"),
    ("partial भी नहीं होगा",                          "offer_partial", "goto_partial_no", "No partial either"),
    ("कुछ भी नहीं दे सकता अभी",                     "offer_partial", "goto_partial_no", "Nothing at all"),

    # ══════════════════════════════════════════════════════════════════════════
    # STATE: refusal_ask_reason
    # ══════════════════════════════════════════════════════════════════════════

    # Got reason
    ("नौकरी गई है मेरी",                              "refusal_ask_reason", "got_reason", "Lost job"),
    ("बीमारी की वजह से खर्चे बढ़ गए",                "refusal_ask_reason", "got_reason", "Medical"),
    ("घर में शादी है, बड़े खर्चे हैं",               "refusal_ask_reason", "got_reason", "Family wedding"),
    ("सैलरी नहीं आई इस महीने",                       "refusal_ask_reason", "got_reason", "Salary delayed"),
    ("बिज़नेस में नुकसान हुआ",                       "refusal_ask_reason", "got_reason", "Business loss"),
    ("घर में किसी की तबीयत ठीक नहीं",               "refusal_ask_reason", "got_reason", "Health issue"),
    ("बच्चे की fees भरी है, पैसे नहीं बचे",         "refusal_ask_reason", "got_reason", "School fees"),
    ("पिछले महीने EMI भरी थी, double नहीं होगा",   "refusal_ask_reason", "got_reason", "Already paid last month"),
    ("कंपनी ने salary रोक ली है",                    "refusal_ask_reason", "got_reason", "Company held salary"),
    ("दुकान बंद है कोरोना से नहीं उठी अभी तक",     "refusal_ask_reason", "got_reason", "Business down"),
    ("बाढ़ में सब कुछ बह गया",                       "refusal_ask_reason", "got_reason", "Flood damage"),
    ("property बेच रहे हैं, थोड़ा time चाहिए",      "refusal_ask_reason", "got_reason", "Selling property"),

    # Still refusing
    ("कोई कारण नहीं बताऊंगा",                        "refusal_ask_reason", "still_refusing", "Refuses reason"),
    ("छोड़ो",                                         "refusal_ask_reason", "still_refusing", "Dismissal"),
    ("बंद करो",                                       "refusal_ask_reason", "still_refusing", "Stop"),
    ("नहीं बताऊंगा",                                  "refusal_ask_reason", "still_refusing", "Won't tell"),
    ("आपको क्या",                                     "refusal_ask_reason", "still_refusing", "None of your business"),
    ("personal बात है",                               "refusal_ask_reason", "still_refusing", "Personal"),
    ("मैं कुछ नहीं बताऊंगा आपको",                   "refusal_ask_reason", "still_refusing", "Tell nothing"),

    # ══════════════════════════════════════════════════════════════════════════
    # STATE: refusal_escalate
    # ══════════════════════════════════════════════════════════════════════════

    ("बिजनेस slow है",                               "refusal_escalate", "got_reason", "Business slow"),
    ("पत्नी की बीमारी है",                           "refusal_escalate", "got_reason", "Wife sick"),
    ("बस इतना बता सकता हूँ कि पैसे नहीं हैं",      "refusal_escalate", "got_reason", "No money minimal"),
    ("बताया ना, salary नहीं आई",                    "refusal_escalate", "got_reason", "Repeats salary"),
    ("मेरे हाथ में नहीं है",                        "refusal_escalate", "got_reason", "Not in my hands"),
    ("बंद करो",                                      "refusal_escalate", "still_refusing", "Stop escalate"),
    ("कोई बात नहीं करूंगा",                         "refusal_escalate", "still_refusing", "Won't talk"),
    ("call काट रहा हूँ मैं",                        "refusal_escalate", "still_refusing", "Cutting call"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────
async def run_tests() -> None:
    total     = len(TEST_CASES)
    correct   = 0
    latencies: list[float] = []

    class_tp: dict[str, int] = defaultdict(int)
    class_fp: dict[str, int] = defaultdict(int)
    class_fn: dict[str, int] = defaultdict(int)
    failures: list[tuple[str, str, str, str, str]] = []

    W = 115
    print(f"\n{'━'*W}")
    print(f"  ADITI EMI BOT — CLASSIFIER TEST   {total} cases   (GPT-4.1-mini)")
    print(f"{'━'*W}")
    print(f"{'#':>3}  {'Utterance':<48} {'Expected':<28} {'Got':<28} {'ms':>6}  {'':>2}")
    print(f"{'─'*W}")

    for i, (utterance, state, expected, notes) in enumerate(TEST_CASES, 1):
        t0  = time.perf_counter()
        got = await classify(utterance, state)
        ms  = (time.perf_counter() - t0) * 1000
        latencies.append(ms)

        ok = got == expected
        if ok:
            correct += 1
            class_tp[expected] += 1
        else:
            class_fp[got]      += 1
            class_fn[expected] += 1
            failures.append((utterance, state, expected, got, notes))

        utt_d  = utterance[:46] + "…" if len(utterance) > 46 else utterance
        ok_tag = "✓" if ok else "✗ FAIL"
        print(f"{i:>3}  {utt_d:<48} {expected:<28} {got:<28} {ms:>6.0f}  {ok_tag}")

    accuracy = correct / total * 100
    all_classes = sorted({e for _, _, e, _ in TEST_CASES})

    print(f"\n{'━'*80}")
    print("PER-CLASS BREAKDOWN")
    print(f"{'━'*80}")
    print(f"{'Class':<32} {'TP':>4} {'FP':>4} {'FN':>4}   {'Precision':>9} {'Recall':>7} {'F1':>6}")
    print(f"{'─'*80}")
    precisions, recalls, f1s = [], [], []
    for cls in all_classes:
        tp = class_tp[cls]; fp = class_fp[cls]; fn = class_fn[cls]
        p  = tp / (tp + fp) if (tp + fp) > 0 else 1.0
        r  = tp / (tp + fn) if (tp + fn) > 0 else 1.0
        f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
        precisions.append(p); recalls.append(r); f1s.append(f1)
        print(f"{cls:<32} {tp:>4} {fp:>4} {fn:>4}   {p:>9.2f} {r:>7.2f} {f1:>6.2f}")

    macro_p  = sum(precisions) / len(precisions)
    macro_r  = sum(recalls)    / len(recalls)
    macro_f1 = sum(f1s)        / len(f1s)

    avg_ms = sum(latencies) / len(latencies)
    p95_ms = sorted(latencies)[int(len(latencies) * 0.95)]

    print(f"{'─'*80}")
    print(f"{'MACRO':<32} {'':>4} {'':>4} {'':>4}   {macro_p:>9.2f} {macro_r:>7.2f} {macro_f1:>6.2f}")

    print(f"\n{'━'*80}")
    print("OVERALL RESULTS")
    print(f"{'━'*80}")
    print(f"  Total cases         : {total}")
    print(f"  Correct             : {correct}")
    print(f"  Wrong               : {total - correct}")
    print(f"  Accuracy            : {correct}/{total}  =  {accuracy:.1f}%")
    print(f"  Macro Precision     : {macro_p:.2f}")
    print(f"  Macro Recall        : {macro_r:.2f}")
    print(f"  Macro F1            : {macro_f1:.2f}")
    print(f"  Avg latency         : {avg_ms:.0f} ms")
    print(f"  p95 latency         : {p95_ms:.0f} ms")

    if failures:
        print(f"\n{'━'*80}")
        print(f"FAILURES  ({len(failures)})")
        print(f"{'━'*80}")
        for utt, st, exp, got, notes in failures:
            print(f"  [{notes}]  state={st}")
            print(f"    utterance : {utt}")
            print(f"    expected  : {exp}")
            print(f"    got       : {got}")
            print()

    print(f"{'━'*80}")
    grade = (
        "🏆 EXCELLENT" if accuracy >= 95 else
        "✅ GOOD"      if accuracy >= 85 else
        "⚠️  FAIR"     if accuracy >= 70 else
        "❌ POOR"
    )
    print(f"  GRADE: {grade}  ({accuracy:.1f}%)")
    print(f"{'━'*80}\n")


if __name__ == "__main__":
    asyncio.run(run_tests())
