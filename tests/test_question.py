"""The question in Telegram (action model).

A question the check asked is posted by plugin code from the app's view as ONE card: the
question's screenshot with the check's question as its caption and [Yes, confirm] [extras]
[Not now] [Open live view] (the deposit records which delivery carried that revision's picture). Each answer runs once: a tap is
persisted `in_progress` with its exact body before the app is called, resumed with that body
after a lost reply, and settled with a receipt every later press of its card returns.
[Open live view] answers nothing; it posts the same question again with the live-view link,
which no tool result, receipt, note or continuation ever carries.
"""
from __future__ import annotations

import json
import time

import pytest

import cards
import copy_text
import state
from conftest import admitted_run, casa_eval

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
SAME = {"verdict": "same", "reason": "Nothing material changed.", "text_incomplete": False}
CHANGED = {"verdict": "changed", "reason": "A fee appeared.", "text_incomplete": False}
ASK = {"verdict": "ask", "category": "payment", "reason": "It pays the invoice.",
       "question": {"text": "Pay €84.20 for the October invoice to Utility Co?",
                    "confirm_label": "Pay €84.20", "extra_options": ["Store credit"],
                    "screenshot": "required", "highlight": ["€84.20"],
                    "text_incomplete": False}}
HA = "https://ha.example/hassio/ingress/abc_casa_browser"
CONFIRM = "Yes, pay €84.20"


@pytest.fixture
def live_url(monkeypatch):
    """The tap_live callback path (a card posted before the link existed, or with no link to make)."""
    monkeypatch.setenv("CASA_PLUGIN_BROWSER_HA_URL", "https://ha.example")
    import tools_ops
    monkeypatch.setattr(tools_ops, "_page_url", lambda: HA)
    monkeypatch.setattr(tools_ops, "_live_url", lambda: None)
    return HA


def test_the_card_links_to_casa_browsers_page(env, monkeypatch):
    """[Open live view] is a link to Casa Browser's page (which offers the question's
    live view); it stores no call and no tap token. Without a link it stays the callback."""
    import tools_ops
    monkeypatch.setattr(tools_ops, "_page_url", lambda: HA)
    run, _ = _asked(env)
    env.ok("post_question", run_id=run)
    live = [b for b in env.card()["buttons"] if b["label"] == copy_text.LIVE_LABEL]
    assert live == [{"label": copy_text.LIVE_LABEL, "url": HA}]
    monkeypatch.setattr(tools_ops, "_page_url", lambda: "http://not-linkable.example")
    assert tools_ops._live_url() is None


def _asked(env, *, shot="shot1", verdict=ASK, **view):
    """A run whose clicked step the check asked about; returns (run, question id)."""
    (run,) = admitted_run(env)
    env.app.judge = True
    env.app.question_shot = shot
    env.app.question_view = view
    if shot:
        env.app.screenshots[(run, shot)] = PNG
    env.casa.judge_script = [verdict]
    env.ok("run_act", run_id=run, kind="click", handle="e1",
           generation=env.app.observation["generation"])
    qid = env.app.runs[run]["pending_question"]
    assert qid and env.app.runs[run]["state"] == "waiting_approval"
    return run, qid


def _posted(env, run, qid, *, status="delivered"):
    out = json.loads(env.ok("post_question", run_id=run))
    assert out["card"], out
    if status is not None:
        env.casa.deliver(out["card"], status)
    return env.card(), out


def _photo(card) -> bytes:
    with open(card["photo"], "rb") as fh:
        return fh.read()


def _labels(card):
    return [b["label"] for b in card["buttons"]]


def _tap(env, card, label):
    text = env.ok("apply_tap", t=env.token(card, label))
    return json.loads(text)["receipt"]


def _continues(env):
    return [(b["run_ref"], b["reason"]) for p, b in env.casa.calls if p.endswith("/continue")]


# -- posting --------------------------------------------------------------------------

def test_question_card_buttons_and_order(env):
    """One card — the question's picture with the check's question as its
    caption (the lead line bold), and [Yes, confirm] [extras…] [Not now] [Open live view]."""
    run, qid = _asked(env)
    card, out = _posted(env, run, qid)
    assert out["note"] == copy_text.QUESTION_POSTED.format(ending=copy_text.HANDED_OVER_SILENT)
    assert _labels(card) == [CONFIRM, "Store credit", "Not now", "Open live view"]
    assert card["text"] == "**" + cards.md(ASK["question"]["text"]) + "**"
    assert _photo(card) == PNG
    assert [b["call"]["tool"] for b in card["buttons"]] == ["apply_tap", "apply_tap", "apply_tap",
                                                             "tap_live"]
    assert card["revision"].startswith("q-")
    # no separate picture: the card is the only deposit of the question
    assert [d["slot"] for d in env.casa.deposits] == ["card"]


def test_a_question_after_a_lapsed_yes_says_his_last_yes_did_not_run(env):
    """A question the app marks `after_lapse` opens with the plugin's fixed line, whatever the
    check could name of the change; any other question does not."""
    run, qid = _asked(env, after_lapse=True)
    card, _ = _posted(env, run, qid)
    lines = card["text"].split("\n")
    assert lines[0] == cards.md(copy_text.AFTER_LAPSE)
    assert lines[1] == "**" + cards.md(ASK["question"]["text"]) + "**"


def test_the_checks_list_is_shown_as_a_list(env):
    """A lead line, the values as a list, the key fact; form keys never invented here."""
    text = ("Submit the pizza order on pizza.example.com?\nIt will send:\n- Name: Jan\n"
            "- Telephone: 0612345678\n\nNo price shown. Your name and phone go to this public site.")
    run, qid = _asked(env, verdict={**ASK, "question": {**ASK["question"], "text": text}})
    card, _ = _posted(env, run, qid)
    assert card["text"].split("\n") == [
        "**" + cards.md("Submit the pizza order on pizza.example.com?") + "**",
        cards.md("It will send:"), "• " + cards.md("Name: Jan"),
        "• " + cards.md("Telephone: 0612345678"),
        cards.md("No price shown. Your name and phone go to this public site.")]
    assert cards.display_len(card["text"]) <= cards.CAPTION_BUDGET


def test_a_long_question_is_cut_to_fit_the_caption(env):
    """Telegram's caption limit — the check's lines are cut, never the buttons."""
    text = "Pay?\n" + "\n".join(f"- Line {i}: " + "x" * 90 for i in range(5))
    run, qid = _asked(env, verdict={**ASK, "question": {**ASK["question"], "text": text}})
    card, _ = _posted(env, run, qid)
    assert cards.display_len(card["text"]) <= cards.CAPTION_BUDGET and card["photo"]
    assert _labels(card)[0] == CONFIRM


def test_confirm_label_reads_as_approval(env):
    """The fixed "Yes, " before the check's label, cut as one label."""
    assert cards.confirm_label("Submit order") == "Yes, submit order"
    assert cards.confirm_label("PDF downloaden") == "Yes, PDF downloaden"
    assert cards.confirm_label("Pay €84.20") == CONFIRM
    long = cards.confirm_label("Submit the very long order for the whole family")
    assert len(long) == cards.LABEL_CHARS and long.startswith("Yes, submit") and long.endswith("…")


def test_a_re_post_waits_while_the_picture_card_is_in_flight(env):
    """While the picture card's delivery is not known, post_question posts nothing."""
    run, qid = _asked(env)
    _posted(env, run, qid, status=None)                     # deposited, not yet delivered
    out = json.loads(env.ok("post_question", run_id=run))
    assert out == {"card": None, "note": copy_text.CARD_IN_FLIGHT}
    assert len([d for d in env.casa.deposits if d["slot"] == "card"]) == 1


def test_each_question_carries_its_own_picture(env):
    """An older question's picture card and a step screenshot, both
    delivered, stand for nothing — the new question's card is its own picture."""
    run, old_q = _asked(env, shot="shotA")
    _posted(env, run, old_q)                                 # an older question, delivered
    env.app.screenshots[(run, "step7")] = PNG
    ref = json.loads(env.ok("show_screenshot", run_id=run, screenshot_id="step7",
                            caption="The page"))["file"]
    env.casa.deliver(ref)
    env.app.questions[old_q]["state"] = "lapsed"
    newer = b"\x89PNG\r\n\x1a\n" + b"\1" * 32
    env.app.screenshots[(run, "shotB")] = newer
    env.app.question_shot = "shotB"
    env.app.runs[run]["state"] = "running"
    env.casa.judge_script = [ASK]
    env.ok("run_act", run_id=run, kind="click", handle="e1",
           generation=env.app.observation["generation"])
    qid = env.app.runs[run]["pending_question"]
    assert qid != old_q
    card, _ = _posted(env, run, qid)
    assert _labels(card)[0] == CONFIRM and _photo(card) == newer


def test_withheld_picture_card_posts_no_confirm(env):
    """The question's picture card was withheld at the delivery layer, so the question goes
    again as text, without the confirm, with `PICTURE_WITHHELD_COPY` — never a confirm without
    its picture."""
    run, qid = _asked(env)
    _posted(env, run, qid, status="withheld")
    out = json.loads(env.ok("post_question", run_id=run))
    assert out["card"] and out["note"] == copy_text.QUESTION_POSTED_NO_CONFIRM.format(
        ending=copy_text.HANDED_OVER_SILENT)
    card = env.card()
    assert _labels(card) == ["Store credit", "Not now", "Open live view"]
    assert cards.md(copy_text.PICTURE_WITHHELD_COPY) in card["text"] and "photo" not in card


def test_no_card_offers_the_confirm_without_the_questions_picture(env, live_url):
    """As a property: across posting, withholding, re-posting and the
    live-view re-post, every card that offers the confirm IS the question's picture."""
    run, qid = _asked(env)
    _posted(env, run, qid, status="withheld")
    card, _ = _posted(env, run, qid)                        # the text card, no confirm
    out = json.loads(env.ok("tap_live", t=env.token(card, "Open live view")))
    assert out["card"]
    env.casa.deliver(out["card"])
    env.app.runs[run]["state"] = "waiting_approval"
    _posted(env, run, qid)
    for dep in [d for d in env.casa.deposits if d["slot"] == "card"]:
        c = json.loads(dep["value"])
        if CONFIRM in _labels(c):
            assert _photo(c) == PNG


def test_live_only_question_has_no_confirm(env):
    run, qid = _asked(env, shot=None, verdict={**ASK, "question": {
        **ASK["question"], "text_incomplete": True, "extra_options": []}})
    card, _ = _posted(env, run, qid)
    assert _labels(card) == ["Not now", "Open live view"] and "photo" not in card
    assert "finish it in the live view" in card["text"]


def test_six_buttons_with_three_extras_and_long_labels(env):
    long = "Refund to the original payment card ending in 4411, please"
    run, qid = _asked(env, verdict={**ASK, "question": {
        **ASK["question"], "extra_options": ["Store credit", "Exchange", long]}})
    card, _ = _posted(env, run, qid)
    assert len(card["buttons"]) == 6
    assert _labels(card)[-2:] == ["Not now", "Open live view"]
    assert all(len(lbl) <= cards.LABEL_CHARS for lbl in _labels(card))
    assert _labels(card)[3].endswith("…") and cards.md(long) in card["text"]


# -- taps -----------------------------------------------------------------------------

def test_confirm_relays_the_rejudge_and_presses_once(env):
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.casa.judge_script = [SAME]
    env.casa.calls.clear()
    assert _tap(env, card, CONFIRM) == copy_text.R_DONE.format(label=cards.md("Pay €84.20"))
    assert [j["kind"] for j in env.casa.judgements()] == ["rejudge"]
    assert len(env.app.dispatches) == 1 and env.app.questions[qid]["state"] == "executed"
    # the confirmed step's receipt is the tapped card's last line, not a message
    raw = json.loads(env.ok("apply_tap", t=env.token(card, CONFIRM)))
    assert raw == {"receipt": copy_text.R_DONE.format(label=cards.md("Pay €84.20")),
                   "on_card": True}
    assert _continues(env) == [(run, "operator_answer")]
    # the same button again, and another button of the card: the first receipt, nothing run
    assert _tap(env, card, CONFIRM) == copy_text.R_DONE.format(label=cards.md("Pay €84.20"))
    assert _tap(env, card, "Not now") == copy_text.R_DONE.format(label=cards.md("Pay €84.20"))
    assert len(env.app.dispatches) == 1 and len(env.app.answers) == 1


@pytest.mark.parametrize("script, results, receipt", [
    ([CHANGED, ASK], [], copy_text.R_ASK_AGAIN),
    ([SAME], ["unknown"], copy_text.R_UNKNOWN.format(label=cards.md("Pay €84.20"))),
    (["check_unavailable", "check_unavailable", ASK], [], copy_text.R_ASK_AGAIN),
], ids=["changed", "outcome_unknown", "no_verdict"])
def test_confirm_receipts(env, script, results, receipt):
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.casa.judge_script = list(script)
    env.app.rejudge_results = list(results)
    assert _tap(env, card, CONFIRM) == receipt
    assert len(env.app.dispatches) == (1 if results else 0)


def test_operator_did_it_in_the_live_view(env):
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.app.answer_gone = True
    assert _tap(env, card, CONFIRM) == copy_text.R_GONE
    assert env.app.dispatches == []


def test_not_now_and_extra_receipts(env):
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    assert _tap(env, card, "Not now") == copy_text.R_DECLINED.format(label=cards.md("Pay €84.20"))
    assert env.app.questions[qid]["state"] == "declined"
    assert "on_card" not in json.loads(env.ok("apply_tap", t=env.token(card, "Not now")))
    run2, qid2 = _asked(env)
    card2, _ = _posted(env, run2, qid2)
    assert _tap(env, card2, "Store credit") == copy_text.R_CHOSEN.format(label="Store credit")
    assert env.app.answers[-1]["choice"] == {"extra": 0}
    assert env.app.dispatches == []


def test_a_superseded_cards_token_is_no_longer_valid(env):
    run, qid = _asked(env)
    old, _ = _posted(env, run, qid)
    env.ok("post_question", run_id=run)                      # posted again: a new card
    assert _tap(env, old, CONFIRM) == copy_text.UNKNOWN_TAP
    assert env.app.answers == []


def test_confirm_tap_lost_response_resumes_same_payload(env):
    """The app's reply to the answer is lost three times; the tap stays in
    progress with its exact body; the next run tool resumes it — one execution, one answer
    body, one receipt."""
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.app.lose_answer_replies = 3
    assert _tap(env, card, CONFIRM) == copy_text.R_LATER
    assert env.app.questions[qid]["state"] == "confirming" and env.app.dispatches == []
    (ans,) = state.answers_in_progress(run)
    env.casa.judge_script = [SAME]
    text = env.ok("run_wait", run_id=run, max_wait_s=1)
    receipt = copy_text.R_DONE.format(label=cards.md("Pay €84.20"))
    assert copy_text.ANSWER_APPLIED.format(receipt=receipt) in text
    assert len(env.app.dispatches) == 1 and state.answers_in_progress(run) == []
    assert all(a == ans.body for a in env.app.answers) and len(env.app.answers) == 4
    # the card's buttons answer the settled receipt
    assert _tap(env, card, CONFIRM) == receipt


def test_prelude_resumes_in_progress_tap(env):
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.app.lose_answer_replies = 3
    _tap(env, card, "Not now")
    assert state.answers_in_progress(run)
    text = env.ok("run_observe", run_id=run)
    assert copy_text.ANSWER_APPLIED.format(
        receipt=copy_text.R_DECLINED.format(label=cards.md("Pay €84.20"))) in text
    assert env.app.questions[qid]["state"] == "declined"


def test_continuation_resumes_in_progress_tap(env):
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.app.lose_answer_replies = 3
    _tap(env, card, "Store credit")
    text = env.ok("continue_run", run_ref=run)
    assert copy_text.ANSWER_APPLIED.format(
        receipt=copy_text.R_CHOSEN.format(label="Store credit")) in text
    assert state.answers_in_progress(run) == []


def test_duplicate_press_during_in_progress_resumes_not_reanswers(env):
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.app.lose_answer_replies = 3
    _tap(env, card, CONFIRM)
    (ans,) = state.answers_in_progress(run)
    env.casa.judge_script = [SAME]
    # a second press of ANOTHER button of the same card resumes the first answer's body
    assert _tap(env, card, "Not now") == copy_text.R_DONE.format(label=cards.md("Pay €84.20"))
    assert {json.dumps(a, sort_keys=True) for a in env.app.answers} == {
        json.dumps(ans.body, sort_keys=True)}
    assert len(env.app.dispatches) == 1 and env.app.questions[qid]["state"] == "executed"


# -- [Open live view] ----------------------------------

def test_tap_live_posts_the_question_again_with_the_link_and_answers_nothing(env, live_url,
                                                                            casa_optional):
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.casa.calls.clear()
    out = json.loads(env.ok("tap_live", t=env.token(card, "Open live view")))
    assert out["card"] and out["note"] == copy_text.LIVE_POSTED
    assert env.app.runs[run]["state"] == "waiting_live"
    assert env.app.answers == [] and env.app.questions[qid]["state"] == "open"
    new = env.card()
    assert _labels(new) == _labels(card) and _photo(new) == PNG
    import tools_ops
    assert new["text"].endswith(copy_text.LIVE_LINE.format(url=tools_ops._escape_all(HA)))
    if casa_optional is not None:
        # Casa's own markdown reader gives one link, to exactly the live-view page
        spans = casa_eval(casa_optional, f"""
from channels.tg_richtext import parse_markdown
display, spans = parse_markdown(json.loads({json.dumps(new["text"])!r}))
print(json.dumps([[display[a:b], k] for a, b, k in spans if k.startswith("link:")]))
""")
        assert spans == [["Open Casa Browser", "link:" + HA]]
    # only the newest card acts: the tapped card's buttons are retired, unanswered
    assert _tap(env, card, CONFIRM) == copy_text.UNKNOWN_TAP
    # the operator presses Done; the new card's confirm runs the frozen press once
    env.app.runs[run]["state"] = "waiting_approval"
    env.casa.judge_script = [SAME]
    assert _tap(env, new, CONFIRM) == copy_text.R_DONE.format(label=cards.md("Pay €84.20"))
    assert len(env.app.dispatches) == 1


def test_live_link_reaches_no_model_receipt_or_record(env, live_url):
    """Casa's INV-PLUG-025's reason: the live-view URL is only inside the
    card deposit — never in a tool result, a receipt, a note, a continuation or a stored
    receipt."""
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    texts = [env.call("tap_live", t=env.token(card, "Open live view"))[0]]
    env.casa.deliver(json.loads(texts[0])["card"])          # the operator taps on the card they received
    new = env.card()
    texts.append(env.call("tap_live", t=env.token(new, "Open live view"))[0])   # live_open
    env.casa.deliver(json.loads(texts[1])["card"])
    newer = env.card()
    env.app.runs[run]["state"] = "live"
    texts.append(env.call("apply_tap", t=env.token(newer, CONFIRM))[0])
    env.app.runs[run]["state"] = "waiting_approval"
    texts.append(env.ok("run_wait", run_id=run, max_wait_s=1))
    texts.append(env.ok("continue_run", run_ref=run))
    texts.append(env.call("post_question", run_id=run)[0])
    blob = "\n".join(texts) + json.dumps([b for _, b in env.casa.calls
                                          if not _ == "/internal/broker/deposit"])
    con = state._connect()
    blob += json.dumps([list(r) for r in con.execute("SELECT * FROM taps").fetchall()])
    blob += json.dumps([list(r) for r in con.execute("SELECT * FROM deliveries").fetchall()])
    for needle in ("ha.example", "ingress", "hassio"):
        assert needle not in blob
    assert copy_text.R_LIVE_FIRST in texts[2]
    links = [d for d in env.casa.deposits
             if d["slot"] == "card" and "ha\\.example" in json.loads(d["value"])["text"]]
    assert len(links) == 2


def test_confirm_while_the_live_view_is_open(env, live_url):
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.ok("tap_live", t=env.token(card, "Open live view"))
    new = env.card()
    env.app.runs[run]["state"] = "live"
    assert _tap(env, new, CONFIRM) == copy_text.R_LIVE_FIRST
    assert env.app.dispatches == [] and env.app.questions[qid]["state"] == "open"
    assert _tap(env, new, CONFIRM) == copy_text.R_LIVE_FIRST     # its stored receipt
    # Done: the question stands with no live card; the next turn is told to post it again
    env.app.runs[run]["state"] = "waiting_approval"
    assert copy_text.QUESTION_WAITING in env.ok("continue_run", run_ref=run)


def test_tap_live_on_a_question_no_longer_current(env, live_url):
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.app.questions[qid]["state"] = "expired"
    out = json.loads(env.ok("tap_live", t=env.token(card, "Open live view")))
    assert out == {"card": None, "note": copy_text.R_NOT_CURRENT}


# -- the sweep and the notes follow the question --------------------------------------

def test_sweep_leaves_a_delivered_question_card_to_the_operator(env, monkeypatch):
    import continuation
    run, qid = _asked(env)
    monkeypatch.setattr(state, "lease_live", lambda run_id, now: False)   # no turn drives it
    swept = {r["run_ref"] for r in continuation.sweep()}
    assert run in swept                                      # never posted
    card, _ = _posted(env, run, qid)
    # the card's delivery settles at the next Fido call; then it holds the run
    text = env.ok("continue_run", run_ref=run)
    assert copy_text.QUESTION_OPEN in text
    assert run not in {r["run_ref"] for r in continuation.sweep()}
    # expired at the app: the run runs again and is continued
    env.app.questions[qid]["state"] = "expired"
    env.app.set_state(run, "running")
    assert run in {r["run_ref"] for r in continuation.sweep()}


def test_withheld_question_card_is_posted_again(env):
    run, qid = _asked(env)
    out = json.loads(env.ok("post_question", run_id=run))
    env.casa.deliver(out["card"], "withheld")
    text = env.ok("continue_run", run_ref=run)
    assert copy_text.QUESTION_WITHHELD in text


def test_a_second_settlement_keeps_the_first_receipt(env):
    """Two processes finishing the same answer — the later receipt never
    replaces the first, and the later caller is given the first."""
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.app.lose_answer_replies = 3
    _tap(env, card, "Store credit")
    (ans,) = state.answers_in_progress(run)
    first = copy_text.R_CHOSEN.format(label="Store credit")
    assert state.finish_answer(ans.hash, first) == first
    assert state.finish_answer(ans.hash, copy_text.R_NOT_TAKEN, card_stays_open=True) == first
    assert _tap(env, card, "Store credit") == first
    assert state.card_state(ans.group).voided is False


def test_a_step_judged_afresh_after_a_lapse_is_not_reported_as_the_confirm(env):
    """The page changed, the question lapsed, and the step judged afresh ran a
    reading step; the receipt must not say the confirmed action was done, and it
    must not say nothing ran either."""
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.casa.judge_script = [CHANGED, {"verdict": "go", "category": "reading",
                                       "reason": "It opens the receipt."}]
    assert _tap(env, card, CONFIRM) == copy_text.R_RAN_AFRESH
    assert env.app.questions[qid]["state"] == "lapsed" and len(env.app.dispatches) == 1


@pytest.mark.parametrize("lapse, receipt", [
    ({"reason": "incomplete", "copy": "This page is too long to check again."},
     cards.md("This page is too long to check again.")),
    ({"reason": "too_much", "copy": "Too much of it changed, so I didn't press it."},
     cards.md("Too much of it changed, so I didn't press it.")),
    ({"reason": "target_gone", "copy": "It is gone from the page."},
     cards.md("It is gone from the page.")),
    ({"reason": "unreadable", "copy": "I couldn't read the page."},
     cards.md("I couldn't read the page.")),
    # an app with no sentence of its own: never a cause the app did not state
    ({"reason": "incomplete"}, copy_text.R_NOT_CHECKED),
    ({"reason": "too_much"}, copy_text.R_NOT_CHECKED),
    ({"reason": "unreadable"}, copy_text.R_NOT_CHECKED),
    ({"reason": "target_gone"}, copy_text.R_GONE),
    ({"reason": "changed"}, copy_text.R_CHANGED),
])
def test_a_lapsed_yes_says_what_the_app_says_and_is_no_success(env, lapse, receipt):
    """A Yes that lapses (on a long page, `incomplete`) pressed nothing, so its receipt is
    neither "The page changed" under a check mark nor a success: it is the app's own
    sentence for the lapse, and it tells Casa the tapped card is no success."""
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.app.answer_lapse = lapse
    out = json.loads(env.ok("apply_tap", t=env.token(card, CONFIRM)))
    assert out == {"receipt": receipt, "not_done": True}
    assert env.app.dispatches == []
    # a second press of the card gets the same answer
    assert json.loads(env.ok("apply_tap", t=env.token(card, CONFIRM))) == out


def test_continue_run_opens_with_the_last_answer_and_what_became_of_it(env):
    """continue_run opens with the operator's last answer and what became of it (lapsed,
    ran), however far the run's steps have moved on."""
    run, qid = _asked(env)
    assert copy_text.LAST_ANSWER.split("{")[0] not in env.ok("continue_run", run_ref=run)
    card, _ = _posted(env, run, qid)
    env.app.answer_lapse = {"reason": "incomplete", "copy": "This page is too long to check."}
    env.ok("apply_tap", t=env.token(card, CONFIRM))
    for i in range(300):
        env.app.add_event(run, "action", text_masked=f"Step {i}.")
    first = env.ok("continue_run", run_ref=run).split("\n")[0]
    assert first == copy_text.LAST_ANSWER.format(
        ago=" (0 min ago)", answer="Yes, Pay €84.20",
        receipt="This page is too long to check.")
    # the newest answer stands: a later Not now replaces it
    env.app.answer_lapse = None
    env.casa.judge_script = [ASK]
    env.ok("run_act", run_id=run, kind="click", handle="e1",
           generation=env.app.observation["generation"])
    qid2 = env.app.runs[run]["pending_question"]
    card2, _ = _posted(env, run, qid2)
    env.ok("apply_tap", t=env.token(card2, "Not now"))
    first = env.ok("continue_run", run_ref=run).split("\n")[0]
    assert first == copy_text.LAST_ANSWER.format(
        ago=" (0 min ago)", answer="Not now", receipt="OK, I didn't press Pay €84.20.")


def test_only_a_yes_that_pressed_nothing_is_not_done(env):
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.casa.judge_script = [{"verdict": "same", "reason": "Unchanged.", "text_incomplete": False}]
    done = json.loads(env.ok("apply_tap", t=env.token(card, CONFIRM)))
    assert done["on_card"] is True and "not_done" not in done
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    assert "not_done" not in json.loads(env.ok("apply_tap", t=env.token(card, "Not now")))


def _other_live_process():
    """A live process that is not this one (our parent), with its real start time."""
    import os
    pid = os.getppid()
    return pid, state._start_time(pid)


def test_an_answer_owned_by_another_live_process_is_not_posted_here(env):
    """One owner per answer across processes. While another live process holds
    the answer's claim nothing is posted here; once that owner is gone, the answer is resumed;
    an answer settled meanwhile is returned, never posted again."""
    import time as _time
    import tools_ops
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.app.lose_answer_replies = 3
    _tap(env, card, "Not now")
    (ans,) = state.answers_in_progress(run)
    pid, start = _other_live_process()
    con = state._connect()
    con.execute("UPDATE taps SET owner_pid = ?, owner_start = ?, owner_at = ? WHERE hash = ?",
                (pid, start, _time.time(), ans.hash))
    con.close()
    posted = len(env.app.answers)
    assert copy_text.ANSWER_IN_PROGRESS in env.ok("run_observe", run_id=run)
    assert len(env.app.answers) == posted                      # nothing posted while owned
    assert _tap(env, card, "Not now") == copy_text.R_LATER and len(env.app.answers) == posted
    # the owner settles it (say: the live view was open); a process holding the stale row
    # gets that receipt and posts nothing
    first = state.finish_answer(ans.hash, copy_text.R_LIVE_FIRST, card_stays_open=True)
    assert tools_ops._apply_owned(ans, None) == first == copy_text.R_LIVE_FIRST
    assert len(env.app.answers) == posted


def test_a_dead_owners_answer_is_resumed(env):
    import time as _time
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.app.lose_answer_replies = 3
    _tap(env, card, "Not now")
    (ans,) = state.answers_in_progress(run)
    pid, _start = _other_live_process()
    con = state._connect()
    con.execute("UPDATE taps SET owner_pid = ?, owner_start = ?, owner_at = ? WHERE hash = ?",
                (pid, "not-its-start-time", _time.time(), ans.hash))
    con.close()
    text = env.ok("run_observe", run_id=run)
    assert copy_text.ANSWER_APPLIED.format(
        receipt=copy_text.R_DECLINED.format(label=cards.md("Pay €84.20"))) in text


def test_a_cut_after_the_press_gets_the_uncertainty_receipt(env):
    """The confirmed press was cut after mouse-down (the relay gets a typed
    `interrupted`); the app recorded the question `outcome_unknown`, and that record decides
    the receipt — its own sentence, never a generic one."""
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.casa.judge_script = [SAME]
    env.app.rejudge_results = ["cut"]
    assert _tap(env, card, CONFIRM) == copy_text.R_UNKNOWN.format(
        label=cards.md("Pay €84.20"))
    assert len(env.app.dispatches) == 1 and state.answers_in_progress(run) == []


def test_an_unreadable_question_record_never_claims_or_denies_the_press(env):
    """The question's record decides a confirm's receipt; when it cannot be read (not a
    transport failure) the receipt says Fido will check, never Done or "didn't press"."""
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    env.app.overrides[("GET", f"/v1/control/runs/{run}/questions/{qid}")] = (
        404, {"error": "not_found"}, "application/json")
    env.casa.judge_script = [SAME]
    assert _tap(env, card, CONFIRM) == copy_text.R_UNSURE
    assert len(env.app.dispatches) == 1


def test_an_answer_resumed_at_a_wake_is_not_a_stall(env):
    """The app took the operator's answer but every reply was lost; the wake that
    resumes it must not end the run as one where nothing happened."""
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    time.sleep(0.01)
    state.record_wake(run, time.time(),              # a wake recorded after the question
                      max([e["seq"] for e in env.app.events[run]], default=0) + 1000)
    env.app.lose_answer_replies = 3
    _tap(env, card, "Store credit")
    env.app.set_state(run, "running")
    text = env.ok("continue_run", run_ref=run)
    assert copy_text.STALLED not in text and env.app.runs[run]["state"] == "running"
    import tools_run                                 # nor at the next wake
    state._connect().execute("DELETE FROM leases").connection.close()
    tools_run._WOKEN.clear()
    import continuation
    continuation.TURN_STARTED_AT = time.time()
    assert copy_text.STALLED not in env.ok("continue_run", run_ref=run)
    assert env.app.runs[run]["state"] == "running"


# -- Casa confirms the card's delivery; a first turn ends silently -----------------

def test_an_unconfirmed_question_card_names_its_deposit_for_casa(env):
    import continuation
    run, qid = _asked(env)
    card, out = _posted(env, run, qid, status=None)
    state._connect().execute("DELETE FROM leases").connection.close()
    (e,) = [r for r in continuation.sweep() if r["run_ref"] == run]
    assert e["unless_delivered"] == [out["card"]]
    env.casa.deliver(out["card"])
    env.ok("browser_status")                   # settled: the card now holds the run itself
    state._connect().execute("DELETE FROM leases").connection.close()
    assert run not in [r["run_ref"] for r in continuation.sweep()]


def test_a_question_for_the_operators_assistant_ends_the_turn_silently(env):
    run, qid = _asked(env)
    card, out = _posted(env, run, qid)
    assert out["note"] == copy_text.QUESTION_POSTED.format(ending=copy_text.HANDED_OVER_SILENT)
    assert "<silent/>" in out["note"] and "first turn too" in out["note"]


def test_a_question_for_a_specialist_ends_a_first_turn_with_a_line(env):
    env.casa.requester["requester_kind"] = "specialist"
    env.casa.requester["chain"] = ["assistant", "finance", "browser"]
    run, qid = _asked(env)
    card, out = _posted(env, run, qid)
    assert out["note"] == copy_text.QUESTION_POSTED.format(ending=copy_text.HANDED_OVER_LINE)
    assert "<silent/>" not in out["note"]


# -- the plan on the card: one Yes for one task ---------------------------------------------------

PLAN = "Create the wishlist \"Yennett deck\" on CardHouse.\nAdd these 3 cards:\n1x Sol Ring\n\n1x Arcane Signet\n1x Yennett, Cryptic Sovereign"
PLAN_ASK = {"verdict": "ask", "category": "account_change",
            "reason": "It changes the account.",
            "question": {"text": "Create the wishlist and add the 3 listed cards?",
                         "confirm_label": "Add 3 cards", "extra_options": [],
                         "screenshot": "none", "highlight": [], "text_incomplete": False}}


def _asked_plan(env, plan=PLAN, verdict=PLAN_ASK):
    (run,) = admitted_run(env)
    env.app.judge = True
    env.casa.judge_script = [verdict]
    env.ok("run_act", run_id=run, kind="click", handle="e1",
           generation=env.app.observation["generation"], plan=plan)
    return run, env.app.runs[run]["pending_question"]


def test_a_plan_card_has_its_pages_verbatim_and_no_photo(env, casa_optional):
    run, qid = _asked_plan(env)
    card, _ = _posted(env, run, qid)
    assert card["pages"] == ["\n".join(cards.md(line) for line in PLAN.splitlines() if line)]
    assert "photo" not in card
    assert _labels(card) == ["Yes, add 3 cards", "Not now", "Open live view"]
    assert card["text"] == "**" + cards.md(PLAN_ASK["question"]["text"]) + "**"


def test_a_card_without_a_plan_is_as_today(env):
    run, qid = _asked(env)
    card, _ = _posted(env, run, qid)
    assert "pages" not in card


def test_a_money_question_with_a_plan_keeps_its_picture_and_no_pages(env):
    """The app keeps the plan off a money question's view: it stays a picture and one step."""
    (run,) = admitted_run(env)
    env.app.judge = True
    env.app.question_shot = "shot1"
    env.app.screenshots[(run, "shot1")] = PNG
    env.casa.judge_script = [ASK]
    env.ok("run_act", run_id=run, kind="click", handle="e1",
           generation=env.app.observation["generation"], plan=PLAN)
    qid = env.app.runs[run]["pending_question"]
    card, _ = _posted(env, run, qid)
    assert "pages" not in card and _photo(card) == PNG


def test_the_live_view_repost_of_a_plan_card_does_not_send_the_list_again(env, live_url):
    run, qid = _asked_plan(env)
    card, _ = _posted(env, run, qid)
    assert card["pages"]
    out = json.loads(env.ok("tap_live", t=env.token(card, "Open live view")))
    assert out["card"]
    assert "pages" not in env.card()


def test_plan_pages_fit_casas_card_check(casa):
    """The largest plan the bounds admit, all punctuation so that every character is escaped,
    posts as pages Casa's own check takes: at most six, each one rich message that leaves the
    settle reserve free, under the label line Casa adds."""
    line = "(1x) [Card]! " * 20
    plan = "\n".join([line.strip()] * 46)[:12_000]
    pages = cards.plan_pages(plan)
    assert pages is not None and 1 <= len(pages) <= cards.MAX_PLAN_PAGES
    got = casa_eval(casa, f"""
import result_broker as rb
head = "\\N{{DOG}} Fido"
pages = json.loads({json.dumps(json.dumps(pages))})
ok = [rb._message_ok(p) and rb.card_fits(p) and rb.card_fits(rb.compose_operator_message(p, head))
      for p in pages]
print(json.dumps({{"ok": ok, "max": rb.PROPOSAL_MAX_PAGES, "chars": rb.MAX_MESSAGE_CHARS,
                  "value": rb.MAX_VALUE_BYTES,
                  "rich": rb.PROPOSAL_SETTLE_RESERVE_BYTES}}))
""")
    assert all(got["ok"]) and got["max"] == cards.MAX_PLAN_PAGES
    assert got["chars"] == cards.MESSAGE_CHARS and got["value"] == cards.VALUE_BYTES
    assert cards.CARD_BYTES == 32_000 - got["rich"]


@pytest.mark.parametrize("text", ["plain words, and more", "snake_case_name <b> a_b", "日本語の請求書",
                                  "🧾📄💶", "[x](https://evil.example) *now* `c` | # ~",
                                  "line one\nline_two\n\\_ <tag>"])
def test_prepared_bytes_bound_casas_prepare(casa, text):
    """The local measure never undercounts what Casa sends: the escaped text's prepared UTF-8
    bytes as `tg_rich.prepare` makes them."""
    body = "\n".join(cards.md(part) for part in text.split("\n"))
    got = casa_eval(casa, f"""
from channels import tg_rich
print(json.dumps(len(tg_rich.prepare(json.loads({json.dumps(json.dumps(body))})).encode("utf-8"))))
""")
    assert cards.prepared_bytes(body) >= got


def test_plan_pages_split_on_lines_and_refuse_what_cannot_fit(plugin_data, monkeypatch):
    assert cards.plan_pages("one\n\ntwo") == ["one\ntwo"]
    assert cards.plan_pages(" \n ") == []
    # a plan the old 3,500-character page cut in three is one rich message now
    assert len(cards.plan_pages("\n".join(f"line{i:04d}" + "x" * 90 for i in range(100)))) == 1
    # Casa's 12,000 characters per message
    pages = cards.plan_pages("\n".join(["x" * 6000] * 6))
    assert len(pages) == 6 and all(cards.fits_message(p) for p in pages)
    assert len(cards.plan_pages("\n".join(["x" * 5000] * 6))) == 3
    assert cards.plan_pages("x" * (cards.MESSAGE_CHARS + 1)) is None
    # one rich message's prepared bytes: 11,000 characters of three bytes each do not fit
    assert cards.plan_pages("日" * 11_000) is None
    assert cards.plan_pages("日" * 10_000) == ["日" * 10_000]
    # the whole card's value: six pages of 7,000 characters are more than one deposit carries
    assert cards.plan_pages("\n".join(["x" * 7000] * 6)) is None
    # and Casa's six pages, with a message that holds one line
    monkeypatch.setattr(cards, "MESSAGE_CHARS", 150)
    assert len(cards.plan_pages("\n".join(["x" * 100] * 6))) == 6
    assert cards.plan_pages("\n".join(["x" * 100] * 7)) is None

