"""
Classifier accuracy test — LLM-primary classify()
All 62 test cases go through Sarvam-M. No keyword heuristic.
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

from main import classify  # noqa: E402

TEST_CASES: list[tuple[str, str, str, str]] = [

    # ── OPENING — C4: Full payment today ──────────────────────────────────────
    ("हाँ, करूंगा",                              "opening", "goto_pay_now",              "Generic yes"),
    ("ठीक है, आज कर दूंगा",                      "opening", "goto_pay_now",              "Today explicit"),
    ("बिल्कुल, अभी pay करता हूँ",                "opening", "goto_pay_now",              "Immediate"),
    ("हाँ हाँ, ठीक है",                          "opening", "goto_pay_now",              "Double yes"),
    ("okay करूंगा",                               "opening", "goto_pay_now",              "Hinglish yes"),

    # ── OPENING — C2: Partial ─────────────────────────────────────────────────
    ("थोड़ा दे सकता हूँ",                        "opening", "goto_partial",              "Explicit partial"),
    ("आधा अभी दे देता हूँ",                      "opening", "goto_partial",              "Half now"),
    ("कुछ amount आज दे सकता हूँ",                "opening", "goto_partial",              "Some amount today"),

    # ── OPENING — C3: Future promise ──────────────────────────────────────────
    ("अगले हफ्ते तक कर दूंगा",                  "opening", "goto_future_promise",       "Next week"),
    ("10 तारीख को करूंगा",                       "opening", "goto_future_promise",       "Specific date"),
    ("कल तक भेज दूंगा",                          "opening", "goto_future_promise",       "By tomorrow"),
    ("अगले महीने कर दूंगा",                      "opening", "goto_future_promise",       "Next month"),
    ("3 दिन में कर दूंगा",                       "opening", "goto_future_promise",       "In 3 days"),
    ("इस हफ्ते में हो जाएगा",                    "opening", "goto_future_promise",       "This week"),
    ("थोड़ा वक्त दीजिए",                         "opening", "goto_future_promise",       "Give me some time"),

    # ── OPENING — C6: Already paid ────────────────────────────────────────────
    ("पहले ही दे दिया",                          "opening", "goto_already_paid",         "Already paid"),
    ("हो गया पेमेंट",                            "opening", "goto_already_paid",         "Payment done"),
    ("UPI से भेज दिया",                          "opening", "goto_already_paid",         "Paid via UPI"),
    ("कर दिया है",                               "opening", "goto_already_paid",         "Done"),
    ("हाँ paid है, पिछले हफ्ते",                 "opening", "goto_already_paid",         "Paid last week"),

    # ── OPENING — C7: Busy / callback ─────────────────────────────────────────
    ("मैं अभी बाहर हूँ, बाद में बात करूंगा",    "opening", "goto_callback",             "Outside + later"),
    ("अभी बाहर हूँ तो बात नहीं कर पाऊंगा",     "opening", "goto_callback",             "THE ORIGINAL BUG CASE"),
    ("बाद में call करो",                         "opening", "goto_callback",             "Call later"),
    ("व्यस्त हूँ अभी",                           "opening", "goto_callback",             "Busy"),
    ("बात नहीं कर पाऊंगा अभी",                  "opening", "goto_callback",             "Can't talk"),
    ("बाद में बात करते हैं",                     "opening", "goto_callback",             "Talk later"),
    ("अभी time नहीं है",                         "opening", "goto_callback",             "No time"),
    ("drive कर रहा हूँ अभी",                    "opening", "goto_callback",             "Driving now"),
    ("और मैं अभी तो नहीं कर पाऊंगा",           "opening", "goto_callback",             "Temporal not-now"),

    # ── OPENING — C1: Death ───────────────────────────────────────────────────
    ("घर में मौत हो गई",                        "opening", "goto_death",                "Death"),
    ("मेरे पापा गुज़र गए",                       "opening", "goto_death",                "Father passed"),
    ("उनका निधन हो गया",                         "opening", "goto_death",                "Passed away"),
    ("वो नहीं रहे",                              "opening", "goto_death",                "Not alive"),

    # ── OPENING — C5a: Financial difficulty ───────────────────────────────────
    ("पैसे नहीं हैं",                            "opening", "goto_financial_difficulty", "No money"),
    ("अभी मुश्किल है",                           "opening", "goto_financial_difficulty", "Difficulty"),
    ("नहीं कर पाऊंगा भुगतान",                   "opening", "goto_financial_difficulty", "Can't pay"),
    ("तकलीफ़ में हूँ",                           "opening", "goto_financial_difficulty", "In trouble"),
    ("नहीं",                                     "opening", "goto_financial_difficulty", "Bare no"),
    ("नहीं हो पाएगा अभी पैसे की तंगी है",      "opening", "goto_financial_difficulty", "Tight on money"),

    # ── OPENING — C5b: Refusal ────────────────────────────────────────────────
    ("बिल्कुल नहीं करूंगा",                     "opening", "goto_refusal",              "Hard refusal"),
    ("बंद करो यह सब",                            "opening", "goto_refusal",              "Stop this"),
    ("मुझे कोई मतलब नहीं",                      "opening", "goto_refusal",              "Don't care"),
    ("नहीं दूंगा",                               "opening", "goto_refusal",              "Won't give"),

    # ── OPENING — tricky LLM-dependent ───────────────────────────────────────
    ("meeting में हूँ, baad mein baat karte hain", "opening", "goto_callback",           "In meeting"),

    # ── OFFER_PARTIAL — Accept ────────────────────────────────────────────────
    ("हाँ, थोड़ा दे सकता हूँ",                  "offer_partial", "goto_partial_yes",    "Accept partial"),
    ("ठीक है, कुछ दे देता हूँ",                 "offer_partial", "goto_partial_yes",    "Accept some"),
    ("हाँ, okay",                                "offer_partial", "goto_partial_yes",    "Simple yes"),
    ("कर सकता हूँ partial",                      "offer_partial", "goto_partial_yes",    "Can do partial"),

    # ── OFFER_PARTIAL — Callback ──────────────────────────────────────────────
    ("नहीं, मैं बाद में बात करूंगा",            "offer_partial", "goto_callback",       "Call later"),
    ("बाहर हूँ, बाद में",                        "offer_partial", "goto_callback",       "Outside+later"),
    ("अभी नहीं, कल बात करते हैं",              "offer_partial", "goto_callback",       "Tomorrow"),
    ("फिर बात करते हैं",                         "offer_partial", "goto_callback",       "Talk again"),

    # ── OFFER_PARTIAL — Decline ───────────────────────────────────────────────
    ("नहीं, बिल्कुल नहीं",                      "offer_partial", "goto_partial_no",     "Hard no"),
    ("संभव नहीं है",                             "offer_partial", "goto_partial_no",     "Not possible"),
    ("नहीं कर सकता",                             "offer_partial", "goto_partial_no",     "Can't do"),

    # ── REFUSAL_ASK_REASON — Got reason ───────────────────────────────────────
    ("नौकरी गई है मेरी",                         "refusal_ask_reason", "got_reason",     "Lost job"),
    ("बीमारी की वजह से खर्चे बढ़ गए",           "refusal_ask_reason", "got_reason",     "Medical"),
    ("घर में शादी है, बड़े खर्चे हैं",          "refusal_ask_reason", "got_reason",     "Family wedding"),
    ("सैलरी नहीं आई इस महीने",                  "refusal_ask_reason", "got_reason",     "Salary delayed"),
    ("बिज़नेस में नुकसान हुआ",                  "refusal_ask_reason", "got_reason",     "Business loss"),

    # ── REFUSAL_ASK_REASON — Still refusing ───────────────────────────────────
    ("कोई कारण नहीं बताऊंगा",                   "refusal_ask_reason", "still_refusing", "Refuses reason"),
    ("छोड़ो",                                    "refusal_ask_reason", "still_refusing", "Dismissal"),
    ("बंद करो",                                  "refusal_ask_reason", "still_refusing", "Stop"),
    ("नहीं बताऊंगा",                             "refusal_ask_reason", "still_refusing", "Won't tell"),
]


async def run_tests() -> None:
    total    = len(TEST_CASES)
    correct  = 0
    latencies: list[float] = []

    class_tp: dict[str, int] = defaultdict(int)
    class_fp: dict[str, int] = defaultdict(int)
    class_fn: dict[str, int] = defaultdict(int)
    failures: list[tuple[str, str, str, str, str]] = []

    print(f"\n{'━'*105}")
    print(f"ADITI CLASSIFIER TEST — LLM-primary (Sarvam-M)  •  {total} cases")
    print(f"{'━'*105}")
    print(f"{'#':>3}  {'Utterance':<47} {'Expected':<28} {'Got':<28} {'ms':>6} {'✓?':>3}")
    print(f"{'─'*105}")

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

        utt_d  = utterance[:45] + "…" if len(utterance) > 45 else utterance
        ok_tag = "✓" if ok else "✗"
        print(f"{i:>3}  {utt_d:<47} {expected:<28} {got:<28} {ms:>6.0f} {ok_tag:>3}")

    accuracy = correct / total * 100
    all_classes = sorted({e for _, _, e, _ in TEST_CASES})

    print(f"\n{'━'*80}")
    print("PER-CLASS BREAKDOWN")
    print(f"{'━'*80}")
    print(f"{'Class':<30} {'TP':>4} {'FP':>4} {'FN':>4}   {'Precision':>9} {'Recall':>7} {'F1':>6}")
    print(f"{'─'*80}")
    precisions, recalls, f1s = [], [], []
    for cls in all_classes:
        tp = class_tp[cls]; fp = class_fp[cls]; fn = class_fn[cls]
        p  = tp / (tp + fp) if (tp + fp) > 0 else 1.0
        r  = tp / (tp + fn) if (tp + fn) > 0 else 1.0
        f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
        precisions.append(p); recalls.append(r); f1s.append(f1)
        print(f"{cls:<30} {tp:>4} {fp:>4} {fn:>4}   {p:>9.2f} {r:>7.2f} {f1:>6.2f}")

    macro_p  = sum(precisions) / len(precisions)
    macro_r  = sum(recalls)    / len(recalls)
    macro_f1 = sum(f1s)        / len(f1s)
    tn_approx = sum(total - class_tp[c] - class_fp[c] - class_fn[c] for c in all_classes) // len(all_classes)

    avg_ms = sum(latencies) / len(latencies)
    p95_ms = sorted(latencies)[int(len(latencies) * 0.95)]

    print(f"{'─'*80}")
    print(f"{'MACRO':<30} {'':>4} {'':>4} {'':>4}   {macro_p:>9.2f} {macro_r:>7.2f} {macro_f1:>6.2f}")

    print(f"\n{'━'*80}")
    print("OVERALL RESULTS")
    print(f"{'━'*80}")
    print(f"  Total cases             : {total}")
    print(f"  TP  (correct)           : {correct}")
    print(f"  FP+FN (wrong)           : {total - correct}")
    print(f"  TN  (avg per class)     : {tn_approx}")
    print(f"  LLM calls               : {total}/{total}  (100%)")
    print(f"  Avg LLM latency         : {avg_ms:.0f} ms")
    print(f"  p95 LLM latency         : {p95_ms:.0f} ms")
    print(f"")
    print(f"  Accuracy                : {correct}/{total}  =  {accuracy:.1f} / 100")
    print(f"  Macro Precision         : {macro_p:.2f}")
    print(f"  Macro Recall            : {macro_r:.2f}")
    print(f"  Macro F1                : {macro_f1:.2f}")

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
    grade = "EXCELLENT" if accuracy >= 90 else "GOOD" if accuracy >= 75 else "FAIR" if accuracy >= 60 else "POOR"
    print(f"  GRADE: {grade}  ({accuracy:.1f}/100)")
    print(f"{'━'*80}\n")


if __name__ == "__main__":
    asyncio.run(run_tests())
