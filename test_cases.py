"""
test_cases.py — Automated FSM + classifier test runner for Aditi voice bot.

Simulates all 7 test cases end-to-end using real LLM classify() calls.
No audio / WebSocket / TTS involved — pure FSM logic validation.

Run:
    python test_cases.py
"""
from __future__ import annotations
import asyncio
import sys
import os
import io

# Force UTF-8 stdout/stderr so Devanagari prints correctly on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
from datetime import date as _date, timedelta
from dataclasses import dataclass, field

# ── Make sure imports resolve from the project root ──────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from classifier import classify, llm_extract_amount, extract_callback_time
from utils import parse_date, parse_amount, fmt_date, is_callback_time
from scripts import build_default_ctx, SCRIPTS, TERMINAL

# ─────────────────────────────────────────────────────────────────────────────
# Minimal session state (mirrors CallSession fields used by FSM)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class FakeSession:
    state: str = "opening"
    ctx: dict = field(default_factory=build_default_ctx)
    partial_offered: bool = False
    partial_attempts: int = 0
    date_retries: int = 0
    refusal_attempts: int = 0
    beyond_90_warned: bool = False
    unclear_count: int = 0
    done: bool = False
    log: list[str] = field(default_factory=list)

    def emit(self, msg: str):
        print(f"    {msg}")
        self.log.append(msg)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _max_date(ctx: dict) -> _date:
    from utils import parse_date as _pd
    raw = ctx.get("emi_overdue_date") or ctx.get("emi_due_date", "")
    base = _pd(raw) if raw else None
    if base is None:
        base = _date.today()
    return base + timedelta(days=90)


async def _go(sess: FakeSession, state: str) -> None:
    sess.emit(f"FSM {sess.state} → {state}")
    sess.state = state
    if state in TERMINAL:
        script = SCRIPTS.get(state, "")
        if script:
            sess.emit(f"SPEAK [{state}]: {script[:80].format_map(sess.ctx)}...")
        sess.done = True


async def _reroute(sess: FakeSession, utterance: str) -> tuple[bool, bool]:
    intent = await classify(utterance, "opening")
    sess.emit(f"REROUTE classify → {intent}")
    today = _date.today()
    if intent == "goto_pay_now":
        await _go(sess, "full_payment_today"); return True, True
    if intent in ("goto_refusal", "goto_financial_difficulty"):
        await _go(sess, "refusal_ask_reason"); return True, False
    if intent == "goto_callback":
        if is_callback_time(utterance):
            sess.ctx["callback_time"] = await extract_callback_time(utterance)
            await _go(sess, "callback_confirm"); return True, True
        await _go(sess, "callback_ask_time"); return True, False
    if intent == "goto_future_promise":
        d = parse_date(utterance)
        if d and d > today:
            sess.ctx["payment_date"] = fmt_date(min(d, _max_date(sess.ctx)))
            await _go(sess, "future_confirm"); return True, True
        await _go(sess, "future_ask_date"); return True, False
    if intent == "goto_death":
        await _go(sess, "death_response"); return True, True
    if intent == "goto_already_paid":
        await _go(sess, "already_paid_ask_date"); return True, False
    return False, False


async def drive_fsm(sess: FakeSession, turns: list[str]) -> None:
    """Feed utterances through the FSM one at a time."""
    import re
    today = _date.today()

    for utterance in turns:
        if sess.done:
            break

        sess.emit(f"USER [{sess.state}]: {utterance}")

        # ── opening ────────────────────────────────────────────────────────
        if sess.state == "opening":
            action = await classify(utterance, "opening")
            sess.emit(f"CLASSIFY opening → {action}")

            if action == "goto_pay_now":
                await _go(sess, "full_payment_today"); break
            elif action == "goto_partial":
                sess.partial_offered = True
                await _go(sess, "partial_ask_amount")
            elif action == "goto_future_promise":
                d = parse_date(utterance)
                if d and d > today:
                    if d > _max_date(sess.ctx): d = _max_date(sess.ctx)
                    sess.ctx["payment_date"] = fmt_date(d)
                    await _go(sess, "future_confirm"); break
                await _go(sess, "future_ask_date")
            elif action == "goto_already_paid":
                await _go(sess, "already_paid_ask_date")
            elif action == "goto_callback":
                if is_callback_time(utterance):
                    sess.ctx["callback_time"] = await extract_callback_time(utterance)
                    await _go(sess, "callback_confirm"); break
                await _go(sess, "callback_ask_time")
            elif action == "goto_death":
                await _go(sess, "death_response"); break
            elif action == "goto_financial_difficulty":
                if not sess.partial_offered:
                    sess.partial_offered = True
                    await _go(sess, "offer_partial")
                else:
                    await _go(sess, "refusal_ask_reason")
            elif action == "goto_refusal":
                await _go(sess, "refusal_ask_reason")
            else:
                sess.emit("UNCLEAR → handle_unclear")

        # ── offer_partial ─────────────────────────────────────────────────
        elif sess.state == "offer_partial":
            action = await classify(utterance, "offer_partial")
            sess.emit(f"CLASSIFY offer_partial → {action}")
            if action == "goto_partial_yes":
                _min_p   = int(sess.ctx.get("min_partial_int", "1500"))
                _emi_int = int(re.sub(r"[^0-9]", "", sess.ctx.get("emi_amount_int", "8500")))
                _amt = parse_amount(utterance) or await llm_extract_amount(utterance)
                if _amt is not None and _amt >= _emi_int:
                    await _go(sess, "full_payment_today"); break
                elif _amt is not None and _amt >= _min_p:
                    sess.ctx["partial_amount"]    = f"{_amt:,}"
                    sess.ctx["remaining_balance"] = f"{max(0, _emi_int - _amt):,}"
                    await _go(sess, "partial_ask_remaining_date")
                else:
                    await _go(sess, "partial_ask_amount")
            elif action == "goto_callback":
                if is_callback_time(utterance):
                    sess.ctx["callback_time"] = await extract_callback_time(utterance)
                    await _go(sess, "callback_confirm"); break
                await _go(sess, "callback_ask_time")
            else:
                handled, should_break = await _reroute(sess, utterance)
                if not handled:
                    await _go(sess, "refusal_ask_reason")
                elif should_break:
                    break

        # ── partial_ask_amount ────────────────────────────────────────────
        elif sess.state == "partial_ask_amount":
            _min_p   = int(sess.ctx.get("min_partial_int", "1500"))
            _emi_int = int(re.sub(r"[^0-9]", "", sess.ctx.get("emi_amount_int", "8500")))
            amount = parse_amount(utterance)
            if amount is None:
                amount = await llm_extract_amount(utterance)
            sess.emit(f"  parsed amount: {amount}")
            if amount is not None and amount >= _emi_int:
                await _go(sess, "full_payment_today"); break
            elif amount is not None and amount >= _min_p:
                sess.ctx["partial_amount"]    = f"{amount:,}"
                sess.ctx["remaining_balance"] = f"{max(0, _emi_int - amount):,}"
                await _go(sess, "partial_ask_remaining_date")
            else:
                sess.partial_attempts += 1
                if sess.partial_attempts >= 2:
                    await _go(sess, "refusal_ask_reason")
                else:
                    sess.emit("SPEAK partial_amount_too_low/unclear")

        # ── partial_ask_remaining_date ────────────────────────────────────
        elif sess.state == "partial_ask_remaining_date":
            d = parse_date(utterance)
            sess.emit(f"  parsed date: {d}")
            if d is None:
                sess.date_retries += 1
                if sess.date_retries >= 2:
                    d = today + timedelta(days=7)
                else:
                    sess.emit("SPEAK partial_date_retry")
                    continue
            if d < today: d = today + timedelta(days=7)
            if d > _max_date(sess.ctx):
                if not sess.beyond_90_warned:
                    sess.beyond_90_warned = True
                    sess.emit("SPEAK date_beyond_90")
                    continue
                await _go(sess, "beyond_90_penalty_close"); break
            sess.ctx["payment_date"] = fmt_date(d)
            await _go(sess, "partial_confirm"); break

        # ── future_ask_date ───────────────────────────────────────────────
        elif sess.state == "future_ask_date":
            d = parse_date(utterance)
            sess.emit(f"  parsed date: {d}")
            if d is None:
                sess.date_retries += 1
                if sess.date_retries >= 2:
                    d = today + timedelta(days=7)
                else:
                    sess.emit("SPEAK future_date_retry")
                    continue
            if d < today: d = today
            if d > _max_date(sess.ctx):
                if not sess.beyond_90_warned:
                    sess.beyond_90_warned = True
                    sess.emit("SPEAK future_date_beyond_90")
                    continue
                await _go(sess, "beyond_90_penalty_close"); break
            sess.ctx["payment_date"] = fmt_date(d)
            await _go(sess, "future_confirm"); break

        # ── refusal_ask_reason ────────────────────────────────────────────
        elif sess.state == "refusal_ask_reason":
            action = await classify(utterance, "refusal_ask_reason")
            sess.emit(f"CLASSIFY refusal_ask_reason → {action}")
            if action == "got_reason":
                sess.ctx["refusal_reason"] = utterance[:120]
                await _go(sess, "refusal_credit_warn")
            else:
                sess.refusal_attempts += 1
                if sess.refusal_attempts >= 1:
                    await _go(sess, "refusal_escalate")
                else:
                    sess.emit("SPEAK refusal_ask_reason")

        # ── refusal_escalate ──────────────────────────────────────────────
        elif sess.state == "refusal_escalate":
            action = await classify(utterance, "refusal_escalate")
            sess.emit(f"CLASSIFY refusal_escalate → {action}")
            if action == "got_reason":
                sess.ctx["refusal_reason"] = utterance[:120]
            else:
                sess.ctx.setdefault("refusal_reason", "Unspecified")
            await _go(sess, "refusal_credit_warn")

        # ── refusal_credit_warn ───────────────────────────────────────────
        elif sess.state == "refusal_credit_warn":
            _rcw_intent = await classify(utterance, "opening")
            sess.emit(f"CLASSIFY refusal_credit_warn(opening) → {_rcw_intent}")
            if _rcw_intent == "goto_death":
                await _go(sess, "death_response"); break
            if is_callback_time(utterance):
                sess.ctx["callback_time"] = await extract_callback_time(utterance)
                sess.emit(f"  callback_time = {sess.ctx['callback_time']}")
            else:
                sess.ctx["callback_time"] = "जल्द ही"
            await _go(sess, "refusal_close"); break

        # ── already_paid_ask_date ─────────────────────────────────────────
        elif sess.state == "already_paid_ask_date":
            import re as _re
            d = parse_date(utterance)
            _PAST_RE = _re.compile(
                r"kiy[ao]|kar[ao]|kar\s*diy[ao]|ho\s*gay[ao]|kar\s*chuk[ao]"
                r"|किया|किये|करा|कर\s*दिया|हो\s*गया|कर\s*चुका",
                _re.IGNORECASE | _re.UNICODE,
            )
            if d == today + timedelta(days=1) and _PAST_RE.search(utterance):
                d = today - timedelta(days=1)
            sess.emit(f"  parsed date: {d}")
            if d is None:
                sess.date_retries += 1
                if sess.date_retries >= 2:
                    sess.ctx["payment_date"] = "recently"
                    await _go(sess, "already_paid_ask_mode")
                else:
                    sess.emit("SPEAK already_paid_date_unclear")
                continue
            if d > today:
                sess.emit("SPEAK already_paid_date_invalid")
                continue
            sess.ctx["payment_date"] = fmt_date(d)
            await _go(sess, "already_paid_ask_mode")

        # ── already_paid_ask_mode ─────────────────────────────────────────
        elif sess.state == "already_paid_ask_mode":
            sess.ctx["payment_mode"] = utterance[:80]
            sess.emit(f"  payment_mode = {sess.ctx['payment_mode']}")
            await _go(sess, "already_paid_confirm"); break

        # ── callback_ask_time ─────────────────────────────────────────────
        elif sess.state == "callback_ask_time":
            if is_callback_time(utterance):
                sess.ctx["callback_time"] = await extract_callback_time(utterance)
                sess.emit(f"  callback_time = {sess.ctx['callback_time']}")
            else:
                handled, should_break = await _reroute(sess, utterance)
                if handled:
                    if should_break: break
                    continue
                sess.date_retries += 1
                if sess.date_retries >= 2:
                    sess.ctx["callback_time"] = "जल्द ही"
                else:
                    sess.emit("SPEAK callback_time_unclear")
                    continue
            await _go(sess, "callback_confirm"); break

        else:
            sess.emit(f"UNHANDLED state: {sess.state}")


# ─────────────────────────────────────────────────────────────────────────────
# Test cases
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TC:
    name: str
    turns: list[str]
    expect_final_state: str
    extra_checks: list[tuple[str, object]] = field(default_factory=list)
    # extra_checks: list of (ctx_key, expected_value_or_callable)


TEST_CASES: list[TC] = [
    TC(
        name="TC-1 | Future promise — 10 din",
        turns=["10 दिन में भुगतान करूँगा"],
        expect_final_state="future_confirm",
        extra_checks=[
            ("payment_date", lambda v: v != ""),
        ],
    ),
    TC(
        name="TC-2 | Financial difficulty → partial declined → refusal close",
        turns=[
            "मेरे पास पैसे नहीं हैं",           # opening → offer_partial
            "नहीं, मेरे पास 1500 रुपये भी नहीं हैं",  # offer_partial → should be PARTIAL_NO (no premature match)
            "नौकरी चली गई",                       # refusal_ask_reason → got_reason
            "अगले महीने कॉल करना",                # refusal_credit_warn → refusal_close
        ],
        expect_final_state="refusal_close",
        extra_checks=[
            ("callback_time", lambda v: v != ""),
        ],
    ),
    TC(
        name="TC-3 | Refusal + '10 साल baad' in credit_warn (must NOT be future_confirm)",
        # NOTE: got_reason fast-paths to refusal_credit_warn immediately (no escalate step).
        # So turns are: opening → refusal_ask_reason → refusal_credit_warn → refusal_close.
        turns=[
            "नहीं दूँगा भुगतान",                 # opening → goto_refusal → refusal_ask_reason
            "नौकरी चली गई",                       # refusal_ask_reason → got_reason → refusal_credit_warn
            "10 साल बाद संपर्क करें",             # refusal_credit_warn → refusal_close (must NOT be future_confirm)
        ],
        expect_final_state="refusal_close",
        extra_checks=[
            ("callback_time", lambda v: bool(v)),  # any non-empty callback_time is fine
        ],
    ),
    TC(
        name="TC-4 | 'Kabhi call mat karna' in credit_warn (must NOT restart refusal_ask_reason)",
        turns=[
            "नहीं दूँगा",                         # opening → refusal_ask_reason
            "personal baat hai",                   # refusal_ask_reason → still_refusing → refusal_escalate
            "band karo yeh sab",                   # refusal_escalate → refusal_credit_warn
            "कभी कॉल मत करना",                    # refusal_credit_warn → refusal_close (callback_time = जल्द ही)
        ],
        expect_final_state="refusal_close",
        extra_checks=[
            ("callback_time", lambda v: v is not None),
        ],
    ),
    TC(
        name="TC-5 | Death — immediate terminal",
        turns=["घर में डेथ हो गई है"],
        expect_final_state="death_response",
    ),
    TC(
        name="TC-6 | Already paid → date + mode",
        # After opening classifies goto_already_paid → already_paid_ask_date,
        # the bot asks for the payment date and THEN the mode. Three turns needed.
        # Use explicit year "2026" so parse_date doesn't roll to next year.
        turns=[
            "payment kar diya tha",                # opening → goto_already_paid → already_paid_ask_date
            "1 April 2026 ko kiya tha",            # already_paid_ask_date → explicit past date → already_paid_ask_mode
            "UPI se",                              # already_paid_ask_mode → already_paid_confirm
        ],
        expect_final_state="already_paid_confirm",
        extra_checks=[
            ("payment_mode", lambda v: "UPI" in v.upper()),
            ("payment_date", lambda v: "April" in v or "2026" in v),
        ],
    ),
    TC(
        name="TC-7 | Beyond-90-day date twice → penalty close",
        # utils.py fix: parse_date('5 March 2026') now correctly returns 2026-03-05
        # so _max_date = 2026-06-03 (not 2027).
        # In opening state, beyond-90 dates are CAPPED (not warned). The penalty flow
        # only fires from future_ask_date. Route there first via a no-date future intent.
        turns=[
            "thoda waqt chahiye",                  # opening → goto_future_promise, no parse_date → future_ask_date
            "agle saal karunga",                   # future_ask_date: today+366 (2027) > 2026-06-03 → warn
            "15 October 2027 mein",                # future_ask_date: 2027-10-15 > 2026-06-03 → beyond_90_penalty_close
        ],
        expect_final_state="beyond_90_penalty_close",
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────
async def run_all() -> None:
    passed = 0
    failed = 0
    results: list[str] = []

    for tc in TEST_CASES:
        print(f"\n{'='*70}")
        print(f"  {tc.name}")
        print(f"{'='*70}")
        sess = FakeSession()

        try:
            await drive_fsm(sess, tc.turns)
        except Exception as exc:
            sess.emit(f"EXCEPTION: {exc}")
            import traceback
            traceback.print_exc()

        # Check final state
        state_ok = sess.state == tc.expect_final_state
        status_lines = [
            f"  final state : {sess.state}  {'[OK]' if state_ok else f'[FAIL expected {tc.expect_final_state}]'}",
        ]

        # Check extra ctx assertions
        extra_ok = True
        for key, check in tc.extra_checks:
            val = sess.ctx.get(key, "")
            if callable(check):
                ok = check(val)
            else:
                ok = val == check
            extra_ok = extra_ok and ok
            status_lines.append(
                f"  ctx[{key}] = {repr(val)}  {'[OK]' if ok else '[FAIL]'}"
            )

        overall = state_ok and extra_ok
        if overall:
            passed += 1
            verdict = "PASS"
        else:
            failed += 1
            verdict = "FAIL"

        print()
        for line in status_lines:
            print(line)
        print(f"\n  >>> {verdict}")
        results.append(f"{'PASS' if overall else 'FAIL'}  {tc.name}")

    # Summary
    print(f"\n{'='*70}")
    print(f"  RESULTS: {passed} passed, {failed} failed out of {len(TEST_CASES)}")
    print(f"{'='*70}")
    for r in results:
        icon = "[PASS]" if r.startswith("PASS") else "[FAIL]"
        print(f"  {icon}  {r[5:]}")
    print()

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_all())
