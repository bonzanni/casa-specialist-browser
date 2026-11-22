"""Operator cards: question, sign-in, login, forget-login and keep-going cards.

A card is a Casa `operator_proposal`: text and at most six buttons, each
storing one call of this plugin's `apply_tap` or `tap_live` with a
single-use tap token as its only argument (or a link; the keep-going card's
Close button and its stored `stop_runs` call). Tokens are minted
here and travel only inside the card value deposited with Casa, never in a
tool result.

Casa renders the text in its markdown dialect, so every variable part — the
site's own words, the check's question — has each ASCII punctuation
character backslash-escaped and is kept on one line; the fixed copy is ours.

Casa posts the card text and each plan page as a Telegram rich message, headed
by Fido's label line, and refuses a proposal unless (Casa's `result_broker`):

* each text holds at most `MESSAGE_CHARS` characters (`MAX_MESSAGE_CHARS`,
  `_message_ok`);
* each text, with the label line, is one rich message that leaves the settle
  line free: at most `CARD_BYTES` prepared UTF-8 bytes (`card_fits`:
  `tg_rich.RICH_BUDGET_BYTES − PROPOSAL_SETTLE_RESERVE_BYTES`). Fido cannot run
  Casa's preparation, so it counts with `prepared_bytes`, which never counts
  less, and keeps `LABEL_BYTES` free for the label line;
* there are at most `MAX_PLAN_PAGES` pages (`PROPOSAL_MAX_PAGES`);
* the whole deposited value is at most `VALUE_BYTES` (`MAX_VALUE_BYTES`).

A photo card's text is its caption instead, measured by `CAPTION_BUDGET`.
"""
from __future__ import annotations

import hashlib
import json
import string

import copy_text
import state

MESSAGE_CHARS = 12_000           # Casa's `MAX_MESSAGE_CHARS`
CARD_BYTES = 32_000 - 169        # `RICH_BUDGET_BYTES − PROPOSAL_SETTLE_RESERVE_BYTES`
LABEL_BYTES = 256                # room for Casa's label line ("🐕 Fido" and a newline today)
VALUE_BYTES = 64 * 1024          # Casa's `MAX_VALUE_BYTES`, the whole proposal as deposited
PLAN_VALUE_BYTES = 40_000        # the plan pages' share of it; the rest is the card text,
                                 # its buttons and its revision
MAX_BUTTONS = 6
LABEL_CHARS = 32
MAX_PLAN_PAGES = 6               # Casa's `PROPOSAL_MAX_PAGES`
TOKEN_TTL_S = 3600
_PUNCT = frozenset(string.punctuation)


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def prepared_bytes(text: str) -> int:
    """At least the UTF-8 bytes Casa's `tg_rich.prepare` makes of `text` (escaped by `md`):
    an escaped `_` is sent as `&#95;` and an escaped `<` as `&lt;`, and Casa from 0.376.1 puts a
    backslash before each line break so Telegram keeps it."""
    return (len(text.encode("utf-8")) + 3 * text.count("_") + 2 * text.count("<")
            + text.count("\n"))


def fits_message(text: str) -> bool:
    """`text` passes Casa's per-message checks on a card text or page."""
    return len(text) <= MESSAGE_CHARS and prepared_bytes(text) <= CARD_BYTES - LABEL_BYTES


def _value_bytes(obj) -> int:
    """UTF-8 bytes of `obj` encoded as `casa_broker.deposit_proposal` encodes a card."""
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def value_fits(card: dict) -> bool:
    """The whole card, as deposited, within Casa's `MAX_VALUE_BYTES`."""
    return _value_bytes(card) <= VALUE_BYTES


def md(text: str, limit: int | None = None) -> str:
    """One line of plain text as Casa's markdown shows it verbatim."""
    one = " ".join("".join(ch if ch.isprintable() else " " for ch in (text or "")).split())
    if limit is not None and len(one) > limit:
        one = one[:limit - 1].rstrip() + "…"
    return "".join("\\" + ch if ch in _PUNCT else ch for ch in one)


def plan_pages(plan: str) -> list[str] | None:
    """A plan as the plain messages Casa posts before its card: its lines in order, each shown
    verbatim through `md`, blank lines dropped, as many to a message as `fits_message` allows.
    None past `MAX_PLAN_PAGES` or `PLAN_VALUE_BYTES`, or for one line longer than a message."""
    pages: list[str] = []
    for line in filter(None, map(md, plan.splitlines())):
        if not fits_message(line):
            return None
        if pages and fits_message(pages[-1] + "\n" + line):
            pages[-1] += "\n" + line
        else:
            pages.append(line)
    if len(pages) > MAX_PLAN_PAGES or _value_bytes(pages) > PLAN_VALUE_BYTES:
        return None
    return pages


def _button(label: str, tool: str, token: str) -> dict:
    assert len(label) <= LABEL_CHARS
    return {"label": label, "call": {"tool": tool, "arguments": {"t": token}}}


def _card(text: str, pages: list[str], buttons: list[dict], revision: str) -> dict:
    assert 1 <= len(buttons) <= MAX_BUTTONS
    card = {"text": text, "buttons": buttons, "revision": revision}
    if pages:
        card["pages"] = pages
    return card


def forget_card(site_id: str, site_name: str, group: str) -> dict:
    payload = {"site_id": site_id, "site": site_name}
    return _card(copy_text.FORGET_TEXT.format(site=md(site_name)), [], [
        _button(copy_text.FORGET_LOGIN, "apply_tap",
                state.mint_tap("forget", payload, group, TOKEN_TTL_S)),
        _button(copy_text.KEEP, "apply_tap", state.mint_tap("keep", payload, group,
                                                            TOKEN_TTL_S))],
        "f-" + hashlib.sha256(group.encode()).hexdigest()[:32])


def keep_going_card(run_id: str, task: str, minutes: int, done_text: str, group: str,
                    request_key: str) -> dict:
    """At each 20 minutes: [Keep going] is a Close button (Casa only removes
    the buttons: the run carries on), [Stop] the stored call of `stop_runs` for the run's
    request, which Casa runs directly — no desk, no model turn."""
    text = copy_text.KEEP_GOING_TEXT.format(task=md(task, 80), minutes=minutes)
    if done_text.strip():
        text += " " + copy_text.SO_FAR.format(done=md(done_text, 200))
    return _card(text, [], [
        {"label": copy_text.KEEP_GOING, "close": True},
        {"label": copy_text.STOP, "call": {"tool": "stop_runs",
                                           "arguments": {"ref": f"request:{request_key}"}}}],
        "k-" + hashlib.sha256(group.encode()).hexdigest()[:32])


# -- the question card (action model) ----------------------

def question_card_id(q: dict) -> str:
    raw = f"{q['question_id']}\0{q['revision']}\0{q['digest']}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def button_label(text: str) -> str:
    """One line of at most `LABEL_CHARS` characters (a longer one ends in "…")."""
    one = " ".join("".join(ch if ch.isprintable() else " " for ch in (text or "")).split())
    return one if len(one) <= LABEL_CHARS else one[:LABEL_CHARS - 1].rstrip() + "…"


def _login_card(q: dict, group: str, base: dict) -> dict:
    """The login question: one item is [Yes] [No]; several are
    their titles and [None of these]. The host is the browser's real address and the titles are
    the vault's, whole: in the text when a button had to cut one. Yes or a title answers
    `{"item": i}`; No and None of these answer `not_now`. No live view and no confirm here."""
    view = q["view"]
    host, options = md(view["host"], 253), view["options"]
    tap = lambda kind, payload, text: _button(text, "apply_tap", state.mint_tap(  # noqa: E731
        kind, dict(base, login=True, **payload), group, TOKEN_TTL_S))
    if len(options) == 1:
        lines = [copy_text.LOGIN_ONE.format(host=host, title=md(options[0]))]
        buttons = [tap("q_answer", {"choice": {"item": 0}, "label": options[0]}, copy_text.YES),
                   tap("q_not_now", {"choice": "not_now", "label": ""}, copy_text.NO)]
    else:
        lines = [copy_text.LOGIN_MANY.format(host=host)]
        if any(button_label(o) != " ".join(o.split()) for o in options):
            lines.append(copy_text.OPTIONS.format(
                options="; ".join(f"{i}\\. {md(o)}" for i, o in enumerate(options, start=1))))
        buttons = [tap("q_answer", {"choice": {"item": i}, "label": o}, button_label(o))
                   for i, o in enumerate(options)]
        buttons.append(tap("q_not_now", {"choice": "not_now", "label": ""},
                           copy_text.NONE_OF_THESE))
    text = "\n".join(lines)
    card = _card(text, [], buttons, "q-" + question_card_id(q))
    if not fits_message(text) or not value_fits(card):
        raise ValueError("question card too long")
    return card


def _site_card(q: dict, group: str, base: dict, photo: str | None) -> dict:
    """The sign-in card for a site that is not set up: every word on it is the
    APP's view — the host read from the browser, the site's label, the one fitting item's
    whole title — never a tool argument. [Yes] answers `confirm` (registers that host and
    signs in), [No] `not_now`. With `photo` (the app's screenshot) the card is that picture."""
    view = q["view"]
    host, name = md(view["host"], 259), md(view["name"], 80)
    first = (copy_text.SITE_ONE.format(host=host, name=name, title=md(view["item"]))
             if view.get("item") else copy_text.SITE_PLAIN.format(host=host, name=name))
    text = "\n".join([first, copy_text.SITE_SAVES])
    tap = lambda kind, payload, label: _button(label, "apply_tap", state.mint_tap(  # noqa: E731
        kind, dict(base, site=True, **payload), group, TOKEN_TTL_S))
    buttons = [tap("q_answer", {"choice": "confirm", "label": view["host"]}, copy_text.YES),
               tap("q_not_now", {"choice": "not_now", "label": view["host"]}, copy_text.NO)]
    card = _card(text, [], buttons, "q-" + question_card_id(q))
    if (not fits_message(text) or not value_fits(card)
            or (photo is not None and display_len(text) > CAPTION_BUDGET)):
        raise ValueError("question card too long")
    if photo is not None:
        card["photo"] = photo
    return card


#: The card text of a question posted with its screenshot is the photo's caption: Telegram takes
#: 1,024 characters after entities parsing, and Casa keeps room for its label line and the
#: settle line. Measured on the display text (escapes and bold markers do not count).
CAPTION_BUDGET = 900
_LIST_MARKS = ("- ", "• ", "* ")


def display_len(text: str) -> int:
    """UTF-16 length of `text` as Casa shows it: escapes and bold markers removed."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
        elif text.startswith("**", i):
            i += 2
        else:
            out.append(text[i])
            i += 1
    return utf16_len("".join(out))


def question_lines(text: str, limit: int = 600) -> list[str]:
    """The check's question as the card shows it: its first line in bold, each
    line the check began with "- " as a list item, every other line as written; each line
    escaped by `md` and cut at `limit` characters, blank lines dropped."""
    lines = []
    for raw in (text or "").splitlines():
        one = " ".join(raw.split())
        if not one:
            continue
        if one.startswith(_LIST_MARKS) and len(one) > 2:
            lines.append("• " + md(one[2:], limit))
        elif not lines:
            lines.append("**" + md(one, limit) + "**")
        else:
            lines.append(md(one, limit))
    return lines or [md(text, limit)]


def confirm_label(label: str) -> str:
    """The confirm button: the fixed "Yes, " before the check's label, its first letter
    lower-cased unless the word is an acronym, cut as one label of `LABEL_CHARS`."""
    one = " ".join((label or "").split())
    if one[:1].isupper() and not one[1:2].isupper():
        one = one[0].lower() + one[1:]
    return button_label(copy_text.YES_PREFIX + one)


def question_card(run_id: str, q: dict, group: str, *, confirm: bool,
                  copy: str | None = None, live_line: str | None = None,
                  photo: str | None = None, live_url: str | None = None) -> dict:
    """The card for the app's question view `q`: the check's text, the app's copy when the
    question offers only the live view, and the buttons, in order — [Yes, confirm] when
    `confirm`, the check's extra options (0-3), [Not now], [Open live view] (always, with the
    app's labels; with `live_url` a link to Casa Browser's page, else the `tap_live` callback). Every word but `live_line` (the plugin's own fixed line) is shown verbatim
    through `md`. At most six buttons. With `photo` (an outbox path) the card is that picture
    with the text as its caption, cut to `CAPTION_BUDGET`. A question with a plan brings
    the plan as pages before the card; `live_line` is its repost, and the operator has the pages."""
    view = q["view"]
    base = {"run_id": run_id, "question_id": q["question_id"], "revision": q["revision"],
            "digest": q["digest"]}
    if view.get("kind") == "login_item":
        return _login_card(q, group, base)
    if view.get("kind") == "site_signin":
        return _site_card(q, group, base, photo)
    label = view["confirm_label"]
    tail = []
    for extra in (view["live_only_copy"] if not view["confirm_offered"] else None, copy):
        if extra and md(extra) not in tail:
            tail.append(md(extra))
    options = view["extra_options"]
    if any(button_label(o) != " ".join(o.split()) for o in options):
        tail.append(copy_text.OPTIONS.format(
            options="; ".join(f"{i}\\. {md(o)}" for i, o in enumerate(options, start=1))))
    if live_line:
        tail.append(live_line)
    head = [md(copy_text.AFTER_LAPSE)] if view.get("after_lapse") else []
    limit = 600
    lines = head + question_lines(view["text"], limit) + tail
    while photo is not None and display_len("\n".join(lines)) > CAPTION_BUDGET and limit > 40:
        limit -= 40
        lines = head + question_lines(view["text"], limit) + tail
    buttons = []
    if confirm:
        buttons.append(_button(confirm_label(label), "apply_tap", state.mint_tap(
            "q_confirm", dict(base, choice="confirm", label=label), group, TOKEN_TTL_S)))
    for i, option in enumerate(options):
        buttons.append(_button(button_label(option), "apply_tap", state.mint_tap(
            "q_answer", dict(base, choice={"extra": i}, label=option), group, TOKEN_TTL_S)))
    buttons.append(_button(copy_text.NOT_NOW, "apply_tap", state.mint_tap(
        "q_not_now", dict(base, choice="not_now", label=label), group, TOKEN_TTL_S)))
    if live_url is not None:
        # a link to Casa Browser's page, which offers this question's live view
        buttons.append({"label": copy_text.LIVE_LABEL, "url": live_url})
    else:
        buttons.append(_button(copy_text.LIVE_LABEL, "tap_live",
                               state.mint_tap("q_live", base, group, TOKEN_TTL_S)))
    text = "\n".join(lines)
    pages = plan_pages(view["plan"]) if view.get("plan") and live_line is None else []
    if pages is None:
        raise ValueError("question card too long")
    card = _card(text, pages, buttons, "q-" + question_card_id(q))
    if (not fits_message(text) or not value_fits(card)
            or (photo is not None and display_len(text) > CAPTION_BUDGET)):
        raise ValueError("question card too long")
    if photo is not None:
        card["photo"] = photo
    return card
