"""Fido's role and doctrine, checked against Casa's own loaders."""
from __future__ import annotations

import json
import re

import yaml

from conftest import ROOT, casa_eval

ROLE = ROOT / "role" / "role.yaml"
DOCTRINE = ROOT / "role" / "doctrine.md"

EXACT_ROLE = """api_version: casa.role/v1
id: specialist:browser
kind: specialist
slot: browser
mission: Fetch, read, download and fill in what the task asks for on the operator's registered websites and the open web through Casa Browser, whose check asks the operator when a step needs it. Work out how to do the job, then do it.
enabled: true
model: {source: fixed, value: opus}
tools:
  allowed: [WebSearch, mcp__casa-framework__query_engager]
  disallowed: [Bash, Write, Edit, Read, Glob, Grep, WebFetch, NotebookEdit, Agent, Task]
  permission_mode: dontAsk
  max_turns: 500
  skills: all
  voice_guard: none
mcp_servers: [casa-framework]
channels: []
memory: {token_budget: 0, read_strategy: per_turn}
session: {strategy: ephemeral, idle_timeout_seconds: 0, long_runs: true}
own_chat:
  short_description: "I'm Fido. I browse the web for Casa: I find, read and fill in websites for you."
  description: "I'm Fido, Casa's browsing specialist. I look things up, read pages, download files and fill in forms on your registered websites and the open web. Give me a task here or through your assistant; I report back with my sources, and Casa Browser asks you before a step that needs your say."
disclosure: {policy: delegated, overrides: {}}
delegates: []
executors: []
triggers: []
hooks: {pre_tool_use: []}
tts: {tag_dialect: none, error_phrases: {}}
response:
  text: {register: "plain and warm, like a colleague handing back the result", max_status_sentences: 3}
  voice: {register: spoken, max_status_sentences: 2}
  restricted_webhook: {register: plain, max_status_sentences: 2}
persona:
  policy: optional-but-bound
  compatibility: ["casa/fido@>=0.1.0 <1.0.0"]
requires:
  plugins: [browser]
  tools: [mcp__plugin_browser_browser__start_runs, mcp__plugin_browser_browser__run_observe]
doctrine_file: doctrine.md
"""

SECTIONS = ["Start", "Which run", "A site that is not set up", "Starting", "Driving",
            "The check and its questions", "One run per turn", "Progress", "Results",
            "Between agents first", "The operator's words about logins",
            "Follow-ups", "Pairing", "How a continuation turn ends"]


def _sections() -> dict[str, str]:
    text = DOCTRINE.read_text("utf-8")
    parts = re.split(r"^## (\d+)\. (.+)$", text, flags=re.MULTILINE)
    return {parts[i]: parts[i + 2] for i in range(1, len(parts) - 1, 3)}


def test_doctrine_says_an_address_opens_in_any_run():
    doctrine = DOCTRINE.read_text(encoding="utf-8")
    assert "`run_search` with exactly one https address opens it" in doctrine
    assert "rather than guessing addresses, unless you hold the address from a reliable source" in doctrine


def test_doctrine_says_public_reading_comes_first():
    which_run = _sections()["2"]
    assert "Read public pages plainly first" in which_run and "`run_fetch`" in which_run
    assert "only for what the operator's accounts hold" in which_run


def test_doctrine_asks_for_the_method_before_the_first_change():
    assert ("Before the first change on a job with many items or more than one site, find out how "
            "the site itself does this in bulk (its help pages, import, export, select-all) and "
            "how names differ between sources, including public references. Try one item. Plan "
            "the method, then the items. Doing the same step a tenth time means stop and look for "
            "another way.") in _sections()["5"]
    import browser_server  # noqa: F401
    import tooling
    assert "A plan states its method first." in tooling.TOOLS["run_act"]["description"]


def test_doctrine_tries_the_bulk_route_and_uses_short_replies():
    secs = _sections()
    assert "try the site's own bulk route on one item and compare its time" in secs["5"]
    assert "`run_read` with offset and length" in secs["5"] and "with all_controls" in secs["5"]
    assert "On a long page, find a control by its words rather than scrolling." in secs["5"]


def test_doctrine_puts_bulk_first_and_pastes_from_the_pad():
    five = _sections()["5"]
    bulk = five.index("Bulk first. Long lists go in bulk.")
    assert bulk < five.index("Long tasks.")
    para = five[bulk:five.index("\n", bulk)]
    for words in ("read the site's help", "export, import or bulk edit", "two or three items",
                  "`run_read` with `to_pad`", "`run_act`'s `text_from`", "Never retype a list.",
                  "row by row only when the site has no bulk route", "the scratchpad's run operation",
                  "read the section it writes before you paste it"):
        assert words in para, words


def test_doctrine_says_a_lapsed_yes_is_told_and_never_retried():
    six = _sections()["6"]
    assert "A confirmed step has run, or comes back" not in six
    assert "When a Yes lapses, tell them what happened and offer the live view" in six
    assert "never try that step again on your own" in six


def test_role_exact():
    assert ROLE.read_text("utf-8") == EXACT_ROLE


def test_role_keeps_no_memory_and_runs_long():
    role = yaml.safe_load(EXACT_ROLE)
    assert role["memory"]["token_budget"] == 0
    assert role["session"]["long_runs"] is True
    assert "desk_context" not in role["session"]
    assert 1 <= len(role["own_chat"]["short_description"]) <= 120
    assert 1 <= len(role["own_chat"]["description"]) <= 512
    assert "WebFetch" in role["tools"]["disallowed"]
    assert "WebSearch" in role["tools"]["allowed"] and "WebSearch" not in role["tools"]["disallowed"]


def test_requires_tools_are_provided():
    role = yaml.safe_load(ROLE.read_text("utf-8"))
    pj = json.loads((ROOT / "plugins/browser/.claude-plugin/plugin.json").read_text("utf-8"))
    assert set(role["requires"]["tools"]) <= set(pj["casa"]["provides_tools"])


def test_doctrine_sections():
    text = DOCTRINE.read_text("utf-8")
    headings = re.findall(r"^## (\d+)\. (.+)$", text, flags=re.MULTILINE)
    assert [int(n) for n, _ in headings] == list(range(1, 15))
    assert [h for _, h in headings] == SECTIONS
    import browser_server  # noqa: F401
    import tooling
    named = set(re.findall(r"`([a-z_]+)`", text))
    tools = set(tooling.TOOLS)
    unknown = {n for n in named if n not in tools
               and n not in ("expectation_status", "plan", "mismatch", "to_pad", "rows",
                             "text_from", *EVENT_KINDS)}
    assert unknown == set(), unknown
    for job in ("start_job", "job_status", "cancel_job", "schedule_job"):
        assert job not in text


#: Casa Browser's event kinds the doctrine names
EVENT_KINDS = ("login_needed",)


def test_doctrine_follows_the_built_app():
    """The events Fido acts on, no automatic reset, `download.<ext>` names; no
    approval, demo or hint is left to ask for."""
    secs = _sections()
    assert "`login_needed`" in secs["5"] and "purpose login" in secs["5"]
    # which challenge it is, is Fido's judgement from the page
    codes = secs["5"][secs["5"].index("Sign-in codes."):]
    assert "`request_login_code`" in codes and "`run_login_code`" in codes
    assert "by email" in codes and "by text message" in codes and "purpose login" in codes
    assert "approve the sign-in in the site's own app" in codes and "`progress`" in codes
    assert "Never repeat a code" in codes and "with `run_act`" in secs["5"]
    assert "never resets or changes a password by itself" in secs["5"]
    assert "try another way on the site" in secs["7"]
    # in a topic too, the check asks, never Fido in plain text
    assert "in a topic too: never ask them in plain text whether to go ahead" in secs["6"]
    assert '"download" with its type' in secs["9"]
    text = DOCTRINE.read_text("utf-8")
    for gone in ("request_blocked", "ask_to_unblock", "card_bundle", "run_hints", "tap_card",
                 "demo_summary", "purpose demo", "Show me once", "approvals page"):
        assert gone not in text, gone


def test_continuation_silence_doctrine():
    secs = _sections()
    eight, fifteen = secs["7"], secs["14"]
    assert "section 14" in eight
    assert "Continue browsing run" in fifteen and "`continue_run`" in fifteen
    assert "silence marker" in fifteen and "empty answer is not allowed" in fifteen
    for kind in ("result", "question", "failure"):
        assert kind in fifteen
    text = DOCTRINE.read_text("utf-8")
    assert re.search(r"<\s*/?\s*[A-Za-z]", text) is None


MARKERS = ("{{", "}}", "{%", "%}", "${", "{#", "#}")


def _leaves(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _leaves(v)
    elif isinstance(value, list):
        for v in value:
            yield from _leaves(v)
    elif isinstance(value, str):
        yield value


def test_no_template_markers():
    """Casa refuses template markers in the doctrine and persona text, and in
    every parsed string of role.yaml (its own flow-style YAML, such as
    `overrides: {}}`, is not text)."""
    for path in (DOCTRINE, ROOT / "persona/pack/persona.md",
                 ROOT / "persona/pack/persona.yaml"):
        text = path.read_text("utf-8")
        for marker in MARKERS:
            assert marker not in text, (path, marker)
    for leaf in _leaves(yaml.safe_load(ROLE.read_text("utf-8"))):
        assert not any(m in leaf for m in MARKERS), leaf


def test_role_loads_in_casa(casa):
    got = casa_eval(casa, f"""
from pathlib import Path
import role_artifact, specialist_component
r = role_artifact.load_role_artifact(Path({str(ROOT / "role")!r}))
role = r.role
print(json.dumps({{"id": role["id"], "slot": role["slot"],
    "violations": specialist_component.specialist_casa_tool_violations(
        list(role["tools"]["allowed"]))}}))
""")
    assert got == {"id": "specialist:browser", "slot": "browser", "violations": []}


def test_doctrine_reaches_the_compiled_prompt(casa):
    """Casa compiles the doctrine into <role_doctrine>; titled otherwise it would compile to
    nothing and no model would read it. Casa runs a specialist on the text
    surface only, so the whole doctrine sits under "# Text projection"; the voice and
    webhook projections carry the core paragraph and a short refusal."""
    got = casa_eval(casa, f"""
from pathlib import Path
import persona_pack, prompt_compiler, role_artifact, role_slot
src = role_artifact.load_role_artifact(Path({str(ROOT / "role")!r}))
role = role_slot.materialize_role(source=src, options={{}})
persona = persona_pack.load_persona_pack(Path({str(ROOT / "persona" / "pack")!r}),
                                         Path({str(ROOT / "persona" / "manifest.json")!r}))
out = prompt_compiler.compile_projection_set(role=role, persona=persona,
                                             platform_frame="frame", safety_kernel="kernel")
print(json.dumps({{s: p.system_prompt for s, p in out.items()}}))
""")
    doctrines = {s: p.split("<role_doctrine>", 1)[1].split("</role_doctrine>", 1)[0]
                 for s, p in got.items()}
    text = doctrines["text"]
    assert "Call `browser_status` first." in text
    assert "press the button with `run_act`" in text
    assert "## 14. How a continuation turn ends" in text
    assert "by voice" not in text and "from a webhook" not in text
    for surface, said in (("voice", "by voice"), ("restricted_webhook", "from a webhook")):
        assert "You are Fido, the household's web runner." in doctrines[surface], surface
        assert said in doctrines[surface], surface
        assert "Call `browser_status` first." not in doctrines[surface], surface


def test_every_projection_is_admitted_by_casas_compiler(casa):
    """With Casa's real platform frame and safety kernel added, every projection must stay
    within its ceiling, or the install fails. The bundle
    is compiled here the way Casa's installer compiles it: Casa's own loaders, its model
    options, its shipped frame and kernel, a component-default binding, and
    `compile_prompt_bundle`, which raises when any projection is over its ceiling."""
    got = casa_eval(casa, f"""
from pathlib import Path
import persona_pack, prompt_compiler, role_artifact, role_slot, personality_binding
casa_dir = Path(prompt_compiler.__file__).parent
root = Path({str(ROOT)!r})
role = role_slot.materialize_role(source=role_artifact.load_role_artifact(root / "role"),
                                  options=role_slot._ha_model_options())
persona = persona_pack.load_persona_pack(root / "persona" / "pack",
                                         root / "persona" / "manifest.json")
binding = personality_binding.materialize_component_default_binding(
    role=role, persona=persona, component_root=root, dependency_digests=(),
    effective_config_digest=personality_binding.compute_effective_config_digest({{}}))
personality = casa_dir / "defaults" / "personality"
bundle = prompt_compiler.compile_prompt_bundle(
    role=role, persona=persona, binding=binding,
    platform_frame=(personality / "platform-frame.md").read_text(encoding="utf-8"),
    safety_kernel=(personality / "safety-kernel.md").read_text(encoding="utf-8"))
print(json.dumps({{s: [getattr(bundle, s).estimated_tokens, prompt_compiler._LIMITS[s][1]]
                  for s in ("text", "voice", "restricted_webhook")}}))
""")
    assert set(got) == {"text", "voice", "restricted_webhook"}
    for surface, (tokens, ceiling) in got.items():
        assert 0 < tokens <= ceiling, (surface, tokens, ceiling)


def test_a_run_that_cannot_go_on_is_told_in_plain_words():
    """Never "I've closed the run as a mismatch" to the operator: the run's
    end is said as what could not be done or confirmed, never as the run's status."""
    text = DOCTRINE.read_text("utf-8")
    para = text[text.index("A real obstacle is one"):]
    para = para[:para.index("When `run_wait`")]
    assert "`run_finish` (mismatch)" in para
    assert "say plainly what you could not do or confirm" in para
    assert "never that the run was finished, closed or a mismatch" in para
    assert "and say so." not in para


def test_a_way_that_fails_is_not_the_end_of_the_task():
    """A way that fails does not end the task, and the operator is never offered the work
    (pasting an export) back. Other ways on the site come first, an obstacle is what remains after real attempts,
    and what the operator asked for is never handed back to them."""
    text = DOCTRINE.read_text("utf-8")
    assert "A way that does not work is not the end of the task: try another way on the site" in text
    assert "A real obstacle is one that remains after real attempts" in text
    assert "Never hand back to the operator what they asked you to do" in text
    assert "when you cannot carry the run further" not in text.lower()


def test_a_sites_words_are_read_from_the_site():
    """A site's terms ("better than Good") are matched against the site's own scale, never
    assumed."""
    assert "A site's words are its own: read its own scale" in DOCTRINE.read_text("utf-8")


def test_an_answer_never_only_points_at_what_they_already_see():
    """Casa posts Fido's whole answer to the operator, so a line
    that only points at a card or link already posted is left out; the closing list of what
    was sent and what to expect stays."""
    para = next(v for v in _sections().values() if "write it to them, as you" in v)
    assert "never a line that only points at a card, link or live-view button you already posted" in para
    assert '"see the card above"' in para and '"waiting for you in the live view"' in para
    assert "a closing list of what you sent them and what to expect next stays" in para


def test_an_answer_for_the_operators_assistant_is_written_to_them():
    """Not "I've sent the operator a setup button … ask me again": it would be
    posted to the operator verbatim. One rule: a request from their assistant is theirs, and the
    answer is written to them, in the second person, never a line meant for the assistant."""
    para = next(v for v in _sections().values() if "write it to them, as you" in v)
    assert "a request from the operator's assistant is theirs" in para
    assert "Casa posts your answer to them as you write it" in para
    assert "never a line meant for the assistant" in para
    assert "(tool results name them so; say it to them)" in para
    assert para.count("speak to them as you") == 0      # one rule, not two


def test_the_answer_is_handed_back_like_a_colleague_would():
    """In Fido's own chat a reply must not read like a bot's. The answer is the answer, in
    his own voice, as a colleague hands back a piece of work, with no account of his steps and
    no word for Casa's workings; the text register says the same."""
    para = next(v for v in _sections().values() if "write it to them, as you" in v)
    assert ("What you post is the answer itself, in your own voice, the way a colleague hands "
            "back a piece of work: what you found or did, not the steps you took to get there, "
            "and never Casa's own workings such as notes, records, relays or ids.") in para
    role = yaml.safe_load(EXACT_ROLE)
    assert role["response"]["text"]["register"] == \
        "plain and warm, like a colleague handing back the result"
    md = (ROOT / "persona/pack/persona.md").read_text(encoding="utf-8")
    assert "hands back what it found the way a colleague would" in md


def test_a_site_that_is_not_set_up_is_signed_in_from_one_card():
    """A new site takes no manual entry, only a check and an agreement. Fido opens the site's sign-in
    page on the public web and calls `ask_sign_in`; the operator's Yes saves the site. Fido
    never registers a site, never types the operator's email or password on a public page, and keeps the
    setup link for a Casa Browser that cannot sign in at all."""
    para = _sections()["3"]
    assert "call `ask_sign_in` with the site's everyday name" in para
    assert "the page where the operator's email or password would go, on the site's own address" in para
    assert "Their Yes saves the site and starts signing in" in para
    assert "their No means you do not sign in: say so to whoever asked" in para
    assert "Never type the operator's email or password on a public page, and never register a site yourself" in para
    assert "Send `site_setup_link` only when `ask_sign_in` says Casa Browser cannot sign in at all" in para
    # the other-host redirect is refused and said plainly
    assert "an address Casa Browser does not open for it yet" in para
    logins = next(v for v in _sections().values() if "you never choose, link or save a login" in v)
    assert "You never register a site: the operator's tap on the sign-in card does." in logins
    which = _sections()["2"]
    assert "for a site the operator has not set up (section 3)" in which


def test_a_challenge_in_front_of_the_form_still_gets_the_sign_in_card():
    """A sign-in page may show an image puzzle before its email form; Fido sends the card
    rather than stopping to explain.
    The site's own page showing a challenge is its sign-in page: `ask_sign_in` there, never
    solving it; after their Yes a sign-in the challenge still blocks goes straight to the login
    live view, without asking whether they want it."""
    para = _sections()["3"]
    intro = para[:para.index("`ask_sign_in` sends the operator")]
    assert 'A challenge in front of the form (an image puzzle, a CAPTCHA, "verify you are human")' in intro
    assert "the site's own page showing it is its sign-in page" in intro
    assert "call `ask_sign_in` there and never stop to explain" in intro
    assert "You never solve a challenge yourself." in intro
    after = para[para.index("After their Yes"):]
    # several fitting items ask which login first — that question goes
    # first; a code the site sends keeps section 5's code flow
    assert ("After their Yes, once any question about which 1Password login to use is answered "
            "and unless the site asks for a sign-in code or for their approval in its app (section 5)") in after
    # with a linked item the broker can report a field-free challenge page as signed
    # in, and an off-site challenge frame is not shown in the account session (a continuation
    # turn may not even know a challenge was there): only a page plainly signed in ends it
    assert ("a page that does not plainly show them signed in (their account, not the sign-in "
            "page) means the sign-in needs them: send `request_live_view` with purpose login") in after
    assert ("with or without a `login_needed` step and even when no challenge shows any more")\
        in after
    assert "they solve it and sign in there. Never ask whether they want it." in after
    # without the card (a page the operator only reads), asking once stays allowed — and never on a
    # sign-in page, which section 3 owns
    five = _sections()["5"]
    assert ("A challenge on a public page you only read (not a sign-in page, section 3) you may "
            "offer once") in five
    assert "(purpose assist)" in five


def test_an_answer_to_the_operator_carries_no_internal_terms():
    """An answer tells the operator nothing about "a drop-off", "your registered sites
    in Casa Browser" or a run id "(r1)". One rule, in the existing write-it-to-them rule:
    everyday words, what happened and what they can do; the closing recap stays.

    A list of terms is not enough: "Vault drop-off" is not covered by "a drop-off" as an
    example (Casa's assistant names its sign-in-link route so). So the rule names its
    outcome, every word one they would use themselves, including a word a brief or a tool result
    used, and lists no terms: a list is read as the whole of what is meant."""
    paras = [v for v in _sections().values() if "write it to them, as you" in v]
    assert len(paras) == 1
    para = paras[0]
    rule = ("and never a line meant for the assistant or a word for how Casa, Casa Browser or you "
            "work inside, even one a brief or a tool result used: every word is one they would use "
            "themselves, so say in everyday words what happened and what they can do.")
    assert rule in para
    text = DOCTRINE.read_text("utf-8")
    assert text.count("in everyday words what happened") == 1
    assert "drop-off" not in text
    assert ("a closing list of what you sent them and what to expect next stays, in those "
            "everyday words.") in para
    # the doctrine itself no longer hands them the term
    assert "registered sites" not in text


def test_a_line_fido_is_told_to_say_word_for_word_names_no_internal_terms():
    """The lines a tool result tells Fido to reply with exactly, and the receipts
    the operator reads, are theirs: second person, everyday words."""
    import copy_text
    said = []
    for name, value in vars(copy_text).items():
        if not isinstance(value, str) or not name.isupper():
            continue
        if name.startswith("R_"):
            said.append((name, value))
        for quoted in re.findall(r'(?:exactly one line|in your reply): "([^"]+)"', value):
            said.append((name, quoted))
    assert {"CODE_HANDED_FAILED", "LOGIN_SAVED"} <= {n for n, _ in said}
    banned = ("the operator", "drop-off", "mismatch", "registered", "run_", "request_",
              "live_view", "(r1)", "step")
    for name, line in said:
        for term in banned:
            assert term not in line, (name, term, line)


def test_a_task_the_operator_or_fido_performed_is_answered_without_sources():
    """After a live view on a code playground an answer does not end
    "Source: W3Schools, "Tryit Editor: HTML input type=time"." Sources belong to an answer to a
    question (what Fido read); an answer about something done on a page names none."""
    para = next(v for v in _sections().values() if "write it to them, as you" in v)
    assert "An open-web answer to a question (something you read, not something you did)" in para
    rule = ("Sources belong only to such an answer: when you or the operator did something on a page (a "
            "field set, a form filled, an order placed), the reply names no source, not even that "
            "page's site and title.")
    assert para.count(rule) == 1
    assert para.index(rule) > para.index("cite the others by site and title")


def test_a_sign_in_task_not_plainly_signed_in_goes_to_the_login_live_view():
    """On a site already set up, Casa Browser can sign in by
    itself and still stop short of the operator's account (behind a challenge) with no `login_needed`
    step. When the task is signing in, only a page plainly signed in ends it; otherwise the
    login live view, without asking, as after the sign-in card's Yes (section 3). The guest
    confirmation is gone from the app, so it names no unconfirmed guest login."""
    # a sign-in that found no form claims nothing (the app's no_form): whatever the
    # task, a sign-in page instead of the operator's account is the login live view.
    five = _sections()["5"]
    assert ("The same holds without such a step: when the site shows its sign-in page instead "
            "of the operator's account, whatever the task, or when the task is signing in and the page does "
            "not plainly show them signed in (their account, not the sign-in page), send "
            "`request_live_view` with purpose login without asking whether they want it, unless "
            "the site asks for a sign-in code or for their approval in its app (below).") in five
    assert five.index("a `login_needed` step means") < five.index("The same holds without")
    assert "Sign-in codes." in five[five.index("The same holds without"):]
    assert "guest" not in DOCTRINE.read_text("utf-8")


def test_the_doctrine_says_the_long_task_rules():
    """One conversation per request: a turn's end loses nothing, a list goes under one plan, an
    unsure item is named plainly, and a task finishes by comparing what the site holds with the
    approved plan. The old rule that a turn has a set number of steps is gone."""
    five, seven = _sections()["5"], _sections()["7"]
    for said in ("A turn's end loses nothing: this request is one conversation",
                 "`run_read` for anything an observation cut",
                 "use `run_look` on both sides",
                 "Name an item you are unsure of plainly; never guess it.",
                 "After a gap in your memory or an unknown outcome, read the site before changing",
                 "must pass `plan` to `run_act` on its very first change, one item being no "
                 "exception",
                 "with it, one Yes covers the plan. Money is always its own question.",
                 "Finish by reading back what the site holds and comparing it with the approved "
                 "plan, line by line; any difference, or anything unconfirmed, is `mismatch`, and "
                 "say which."):
        assert five.count(said) == 1, said
    assert "a set number of steps" not in DOCTRINE.read_text("utf-8")
    assert "tells you to end your turn, end it." in seven


def test_the_operator_sees_the_sources_once():
    """The operator reads the sources once, beneath the answer: the answer goes with them in
    one `show_sources` call, never as a separate message before it or listed again in a
    reply; a specialist's answer carries them as text. The show_sources call finishes the run
    itself, so no continuation answers again."""
    para = next(v for v in _sections().values() if "write it to them, as you" in v)
    assert para.count("its sources, which the operator sees once, under the answer: when the requester is "
                      "the operator's assistant, send the whole answer with them in one "
                      "`show_sources` call (its answer is exactly what you would have replied, "
                      "the sources numbered in the order you rely on them, and "
                      "`expectation_status` as for `run_finish`): Casa posts them to the operator as one "
                      "message, the links beneath the answer, and the call finishes the run; "
                      "then end your turn as its result says, and never send that answer "
                      "again.") == 1
    assert para.count("When a specialist asked, list them in your reply, numbered, each by site "
                      "and page title.") == 1
    assert "also sent with `show_sources`" not in para
    assert "your reply does not list them again" not in para
    # an unconfirmed post may have landed (a TimedOut after Telegram took it), so
    # the answer is never sent again; Casa's withheld result says what to tell them
    assert para.count("When Casa says it could not confirm the message arrived, it may have "
                      "arrived: say what Casa's result tells you to say, and never send the "
                      "answer again.") == 1
    assert "reply with the answer and cite its sources" not in para


def test_the_one_who_signed_in_is_named():
    """When Casa Browser signed in by itself (the page's line says so),
    Fido's answer never says "I signed in". Fido drives the browser; Casa Browser signs in and saves
    logins; its reply names who did it."""
    para = next(v for v in _sections().values() if "write it to them, as you" in v)
    assert para.count("Say who did what: you drive the browser, and Casa Browser signs in and "
                      "saves logins by itself; when a page's line or a result says Casa Browser "
                      "signed in or saved a login, say Casa Browser did it (\"Casa Browser "
                      "signed you in to the payslip portal.\"), never that you did.") == 1
    text = DOCTRINE.read_text("utf-8")
    assert "I saved your login" not in text
    assert "Casa Browser saved your login." in _sections()["5"]
    import copy_text
    for name, value in vars(copy_text).items():
        if isinstance(value, str) and name.isupper():
            assert "I saved" not in value, name


def test_a_reply_speaks_of_their_task_not_the_run():
    """Not "Nothing is running, so there was nothing to cancel. The
    link-listing run had already finished." The operator asked for a task; the run is how it is carried."""
    para = next(v for v in _sections().values() if "write it to them, as you" in v)
    assert para.count("Speak of their task (what they asked for), never of the run that carries it: "
                      "\"the link list had already stopped, 41 of its 52 links read, before I could cancel it\".") == 1


def test_a_stop_they_made_ends_the_turn_silent():
    """Every call on a request the operator stopped answers STOPPED; section 7 says to
    end the turn with the silence marker alone (the tool result says so too)."""
    seven = _sections()["7"]
    assert seven.count("When a result says the operator stopped the browsing, end your turn with exactly "
                       "the silence marker.") == 1
    import copy_text
    assert copy_text.STOPPED.endswith("your whole answer is exactly <silent/> and nothing else, "
                                      "in a first turn too.")


def test_a_continuation_does_not_answer_again_what_show_sources_posted():
    """Section 14 says a continuation with an answer answers normally (it
    is relayed), while section 9's show_sources ends silent once the answer is posted with its
    sources. Section 14 also says that answer is not answered again."""
    fourteen = _sections()["14"]
    rule = ("An answer `show_sources` posted to the operator with its sources is not answered again: "
            "the call also finishes the run, and you end as its result says.")
    assert fourteen.count(rule) == 1
    assert fourteen.index("answer normally; it is relayed to them.") < fourteen.index(rule)


def test_a_pairing_is_answered_with_its_sentence_alone():
    """Not "The setup tool returned: Paired with Casa Browser 0.1.8." Section 13 says to
    answer with the sentence alone."""
    thirteen = _sections()["13"]
    assert thirteen.count("When asked to pair, or to run `setup_browser`, call it and answer "
                          "with its sentence alone, as it stands: no words around it, never "
                          "\"the setup tool returned\".") == 1


def test_a_first_turn_says_what_is_happening_now():
    """Not "The browser is still starting up; I'm waiting for it to open CNN." when no result
    reported a wait. A first turn says in plain words what is happening, and names a wait only
    when a result says what the run waits for."""
    seven = _sections()["7"]
    rule = ("In a first (delegated) turn, unless a result says to end with the silence marker, "
            "end with one line saying in plain words what is happening now (\"Opening CNN now.\", "
            "\"Reading your payslip portal.\"), or, when a result says what the run waits for "
            "(the operator's answer, a code), that; never claim a wait, a delay or a start-up that no result "
            "reported.")
    assert seven.count(rule) == 1
    assert "end with one line saying what the run waits for, unless" not in seven


def test_an_answer_read_on_the_page_is_stated_plainly():
    """Not "Best guess for CNN's top headline right now" for a page Fido has read. What Fido read is the answer; a hedge is for what it could
    not read clearly, and a doubt is said once, where it says what it is unsure of."""
    para = next(v for v in _sections().values() if "write it to them, as you" in v)
    assert para.count("An open-web answer to a question (something you read, not something you "
                      "did) states what you read first, plainly, as the answer (\"best guess\" "
                      "and the like only when you could not read it clearly), then the "
                      "alternatives worth mentioning, what you are unsure of, said there once, "
                      "and its sources,") == 1
    assert "gives the best answer first" not in para


def test_fido_clears_banners_fills_freely_and_keeps_going():
    """Banners go first at their minimum, by Fido's own judgement; filling
    in is not checked, sending is; a long job is carried through; a list is copied, not retyped."""
    text = (ROOT / "role" / "doctrine.md").read_text(encoding="utf-8")
    assert "clear it first, before reading or signing in, at its minimum choice" in text
    assert "the check reads the whole form when you press what sends it" in text
    assert "Keep going until the job is done or truly blocked" in text
    assert "never tell them to ask again for it" in text
    assert "(ask again, or open the live view)" not in text
    assert "Copy a list from its source text; never retype it from memory" in text
    # a sign-in that sent nothing because the page moved is tried again before any live view
    assert "clear the page (a banner, a pop-up) and observe again" in text
