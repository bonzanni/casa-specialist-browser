"""Every fixed sentence the plugin says to a model or, through Casa, to the
operator. Kept in one place so the wording is reviewed once and no sentence
is ever built from text the app or Casa sent.

(Named `copy_text`, not `copy`: this directory is the first entry of the
server's `sys.path`, and a module named `copy` would replace the standard
library's for every module that imports it.)
"""

APP_OLDER = "Casa Browser needs updating before Fido can browse."
APP_NEWER = "Fido needs updating before it can use this Casa Browser."
NOT_PAIRED = ("Fido is not paired with Casa Browser yet. Press Pair with Casa on the "
              "Casa Browser page, then ask me to pair.")
PAIR_AGAIN = ("Casa Browser no longer accepts Fido's pairing. Press Pair with Casa on its "
              "page, then ask me to pair again.")
UNREACHABLE = "Casa Browser is not answering. It may be stopped or not installed."
NOT_READY = "Casa Browser is still being set up (its page says what is missing)."
#: a run route's `not_ready`, so that an observation of a run still starting is not told as the
#: setup's sentence ("still being set up"). It names no
#: cause: the app answers it for a run still starting, one waiting for the operator, and a
#: page it could not read yet
RUN_NOT_READY = ("That run cannot take this step right now; Casa Browser itself is set up. "
                 "Call run_wait, then try again.")
PAIRED = "Paired with Casa Browser {app_version}."
NO_WINDOW = ("Casa Browser is not offering a pairing right now. Press Pair with Casa on its "
             "page, then ask me to pair within two minutes.")
FILE_TOO_LARGE = ("That is larger than Casa Browser keeps (100 MB per file, 500 MB per zip); "
                  "nothing was kept.")
UNKNOWN_HOST = ("Fido cannot tell where Casa Browser is from this Casa's host name. Set the "
                "Casa Browser address in Fido's settings.")
NO_DATA_DIR = "Fido has no private data folder in this Casa, so it cannot keep a pairing."
UNEXPECTED = "Casa Browser gave an answer Fido does not understand. Updating both may help."
TOO_LARGE = ("Casa Browser's answer was larger than Fido reads at once (4 MiB); nothing was "
             "done.")

# One sentence per AppError code the plugin can raise.
BY_CODE = {
    "app_older": APP_OLDER,
    "app_newer": APP_NEWER,
    "not_paired": NOT_PAIRED,
    "unauthorized": PAIR_AGAIN,
    "unreachable": UNREACHABLE,
    "not_set_up": NOT_READY,
    "not_ready": RUN_NOT_READY,
    "file_too_large": FILE_TOO_LARGE,
    "unknown_host": UNKNOWN_HOST,
    "no_data_dir": NO_DATA_DIR,
    "bad_response": UNEXPECTED,
    "too_large": TOO_LARGE,
    "protocol_mismatch": ("Casa Browser and Fido speak different versions; one of them "
                          "needs updating."),
    "stale_handle": "That handle is not on the page as last observed: observe again and use one it lists.",
    "blocked": "Casa Browser blocked that.",
    "unsupported": "Casa Browser does not support that request.",
    "interrupted": "Casa Browser stopped that run (it restarted or was interrupted).",
    "bad_request": "That request is not one Fido can send.",
    "conflict": "That changed meanwhile; look again.",
    "not_found": "Casa Browser has no such thing (it may have ended or been removed).",
    "internal": "Casa Browser hit an internal error; nothing more is known.",
    "turn_bound": "End your turn now; a fresh turn carries on within a minute.",
    "judgement_stale": "That step's check came too late (the step was replaced or expired); "
                       "nothing was done. Observe again.",
    "digest_mismatch": "That step's check did not match the step; nothing was done. Observe "
                       "again.",
}

# -- tools ------------------------------------------------------------
UNKNOWN_TOOL = "That tool does not exist."
UNKNOWN_ARGUMENT = "That tool does not take one of the arguments given; nothing was done."
BAD_ARGUMENTS = "The argument {name} is not valid for that tool; nothing was done."
TOOL_FAILED = "That tool failed unexpectedly. Nothing more is known."
CASA_FAILED = "Casa did not accept that request ({code}); nothing was done."
END_TURN_BOUND = "End your turn now; a fresh turn carries on within a minute."
END_TURN_WAITING = "End your turn now; the operator's answer continues it."
END_TURN_STEPS = ("This turn has used its steps: drive the run no further. End your turn: a fresh "
                  "turn carries on from the page within a minute.")
FINISH_NOW = ("This run has used 2,000 tool calls. Finish it now with run_finish: say what is "
              "done and what is left.")
RESULT_CUT = "\n[Cut here: this result is too long to show whole. run_read has the page's text.]"
TEXT_CUT = "[The page's text goes on past this point: run_read has all of it.]"
#: a page reply is short on any site; these say how much was left out and how to get it
PAGE_TEXT_TOP = ("[The top of the page's text: {shown} of {total} characters, {lines} lines. "
                 "run_read goes on from offset {offset} (length takes less); with find it looks "
                 "for words in all of it.]")
CONTROLS_LEFT = ("[{listed} of {total} controls listed; not listed: {left}. run_observe with find "
                 "lists those whose label or value holds your words, anywhere on the page; with "
                 "all_controls true, all that fit.]")
CONTROLS_ELSEWHERE = "elsewhere on the page {n}"
CONTROLS_CAPPED = ("[{listed} of the page's {total} controls and dropdown options are listed, what "
                   "is in view first. run_observe with find reaches any of them by its words.]")
OPTIONS_LEFT = " ({n} options not listed)"
CONTROLS_NONE = ("No visible control on the page holds those words: all of it was searched, as "
                 "loaded so far. Words in its text: run_read with find.")
CONTROLS_NONE_PART = ("No control found with those words, but the search could not reach all of the "
                      "page (a limit, a frame that is not shown or a closed part of it). run_read "
                      "with find or run_look may still show it.")
FIND_PART = ("[The search could not reach all of the page (a limit, a frame that is not shown or a "
             "closed part of it).]")
FIND_MORE = "[{n} more hold those words and did not fit: narrower words reach them.]"
CONTROLS_CUT = "Long labels are cut so every control fits; find with a label's words lists fewer, whole."
READ_FOUND = "{n} place(s) hold those words, each with its offset; run_read with offset goes there:"
READ_FOUND_MORE = "The first {shown} are shown."
READ_NOT_FOUND = "The text does not hold those words (plain words, any case; no patterns)."
LOOK_SHOWN = "The page as it looks now."
LOOK_NONE = "No picture of this page is available; read it with run_observe or run_read."
READ_ONE_THING = "Read one thing at a time: a handle, a file_id or rows, only one of them."
PAD_READ_ALONE = ("to_pad takes all of the text from offset: it goes with neither find nor "
                  "length.")
PAD_WRITTEN = ("Wrote {chars} characters ({lines} lines) from offset {start} to scratchpad "
               "section {section}, replacing it; masked {masked}. The text is there, not here.")
PAD_MORE = ("More follows past the scratchpad's 1 MiB: run_read again with offset {next_offset} "
            "and to_pad another section.")
TEXT_FROM_ALONE = "text_from goes only with kind type, in place of text."
TEXT_FROM_TOO_LONG = ("Scratchpad section {section} holds more than typed text may (32,000 "
                      "characters, 2,000 on one line); nothing was typed. Put less in one section.")
READ_STATUS = "[offset {offset}, next_offset {next_offset}, complete {complete}, masked {masked}]"
READ_MORE = "More follows: call run_read again with offset {next_offset}."
READ_WHOLE = "This window reaches the end, and no limit cut the text."
READ_PARTIAL = "That is all Casa Browser can read of it; the page may hold more than this."
FETCH_HEAD = "file {file_id} ({mime}, {size} bytes) from {url}"
FETCH_MORE = "More follows: call run_read with file_id {file_id} and offset {next_offset}."
FETCH_FIELDS_CUT = ("These fields run past one window: ask for fewer, or run_read the file for "
                    "the whole answer.")
FETCH_NOT_TEXT = "That is not text to read here; the file is kept for this run."
FETCH_NOT_JSON = "That answer is not JSON, so fields do not apply; this is what it says."
FETCH_HTTP = ("The site answered {status} to a plain read, with nothing to read. Open it in the "
              "browser: run_search with exactly that address, or search for the page if the "
              "address may be wrong.")
FETCH_NOT_RUN = {
    "not_document": ("That address serves a type of file that goes through Casa's check. Open it "
                     "in the browser: run_search with exactly that address, then download it from "
                     "the page."),
    "bad_address": ("run_fetch reads exactly one https address. Give the whole address as it is, "
                    "or search for the page."),
    "blocked": ("Casa Browser could not reach that address, or does not read it (a private "
                "address never). Try another address, or search for the page."),
}
PLAN_TOO_LONG = ("That plan cannot go on a question: it must be plain text (no tabs or other "
                 "control characters) and fit six messages. Nothing was done; shorten it.")
APPROVED_PLAN = "The operator approved this plan with one Yes:\n{plan}"
POST_CARD_FIRST = "Post the question first: post_question."
SEND_LINK_FIRST = "Send the live-view link first (request_live_view)."
UNKNOWN_RUN = "Fido has no record of that run."
NOT_YOUR_RUN = "That run belongs to another request."
NO_SUCH_SITE = "Casa Browser has no site {site_id}; browser_status lists the sites."
SITE_RULE_ACCOUNT = ("An account run needs at least one of the operator's sites with account "
                     "access.")
SITE_RULE_OPEN_WEB = "An open-web run cannot use account access to any site."
DUPLICATE_SITE = "A site appears twice in one run."
START_URL_RULE = ("Only an open-web run takes start_url, and only as one https address the task "
                  "gives.")
NOT_PRINTABLE = "The task kind must be one line of plain text."
STARTED = ("Started {n} run(s): {ids}. Drive the first ({first}); the others continue one "
           "by one.")
CONFIRMATION_PENDING = ("The operator has a card asking them to start; their answer "
                        "continues it.")
#: the card says what waits on the operator's tap, so a line saying it again only repeats it
CONFIRMATION_SILENT = ("The operator already sees it: end your turn now, and your whole answer is "
                       "exactly <silent/> and nothing else, in a first turn too.")
CONFIRMATION_LINE = ("End your turn now: a first turn ends with one short line to whoever "
                     "asked saying the run waits for the operator's OK.")
ADMISSION_REFUSED = "Casa could not ask the operator, so no run started. Tell whoever asked."
ADMISSION_DECLINED = "The operator said not now; no run started. Tell whoever asked."
ADMISSION_EXPIRED = ("The operator's OK did not come within the hour; no run started. "
                     "Tell whoever asked.")
ADMISSION_STILL_PENDING = "Still waiting for the operator's answer. End your turn."
ADMISSION_UNKNOWN = "Fido has no request matching that reference."
ADMISSION_ENDED = "That request already ended ({status})."
ADMISSION_BUSY = "That request is already being started; check again shortly."
SUBMIT_PARTIAL = ("Started {done} of {total} run(s); the rest are recorded and continue "
                  "later.")
ALREADY_FINISHED = "That run has already ended ({state})."
#: an ending nothing told yet / one already told
ENDED_TELL = ("Nothing was driving it when it ended, so whoever asked has not heard: tell them "
              "in one or two plain lines what became of it and what they can do (ask again).")
ENDED_TOLD = ("Whoever asked was already told how it ended: in a continuation turn your whole "
              "answer is exactly <silent/>.")
RUN_ENDED = "The run has ended ({state})."
EARLIER_STEPS = "({n} earlier steps not shown.)"
LAST_ANSWER = "The operator's last answer{ago}: {answer}. What happened: {receipt}"
LIVE_IS_OPERATORS = "The live view is the operator's; it ends when they press Done."
FOREIGN_FILE = "File {file_id} does not belong to that run."
FINISHED = "Finished the run ({status})."
#: the closing answer reaches the operator themselves, relayed by their assistant
SPEAK_TO_THE_OPERATOR = (" Your answer reaches the operator in their own chat: speak to them as "
                         "you (\"the time you set\"), never as \"the operator\".")
#: v0.1.27: the run's answer went out with show_sources, so the finish never asks for it: it
#: ends silent once Casa confirmed it delivered (HANDED_OVER_SILENT), else with
#: ANSWER_UNCONFIRMED
ANSWERED_FINISHED = "The run is already finished."
#: v0.1.27: Casa did not confirm the answer's message reached the operator
ANSWER_UNCONFIRMED = ("Casa could not confirm that message reached the operator: end your turn with one "
                      "plain line saying your answer may not have reached them and they can ask "
                      "again, and never repeat the answer.")
ANSWERED_WITH_SOURCES = " Your answer went out with show_sources: never write it again."
CANCELLED = "Cancelled the run."
NEXT_QUEUED = " The next run of this request continues in a fresh turn."
PAGE_CHANGED = "That handle is not on the page as last observed: observe again and use one it lists."
STALLED = ("Nothing happened on this run during the last turn, so it has been stopped. Tell "
           "whoever asked in one or two plain lines what stopped it and what they can do (ask "
           "again); no handles, ids, addresses or error codes.")
SCROLL_MOVED = "It is now {top} of {end} pixels down."
SCROLL_STILL = "It did not move: {top} of {end} pixels down."
DONE_ACTION = ("Done ({outcome}). The page is now generation {generation}; observe again "
               "before the next action.")
DONE_SCROLL = ("Done. The page is now generation {generation}. To scroll further, repeat this same "
               "call; observe when you want to act on what is there.")
NO_RECORDS = "No earlier runs for this requester in that period."
NOTICES_SENT = "Casa Browser had {n} notice(s); they were handed to Casa for the operator."
NOTICE_REPEATS = " (×{n})"
QUESTION_OPEN = ("The operator's question about this is open. End your turn; their answer "
                 "continues the run.")
QUESTION_UNCONFIRMED = ("The operator's question is still being delivered. End your turn; "
                        "Fido checks again within a minute.")
FILE_WITHHELD = ("The {name} file (run {run_id}, file {file_id}) did not reach the operator; "
                 "send it again with deliver_file.")
FILE_UNCONFIRMED = ("The {name} file's delivery could not be confirmed; send it again with "
                    "deliver_file (run {run_id}, file {file_id}).")
SEND_FILE_FIRST = ("{name} (file {file_id}) has not been sent: send it first (deliver_file, "
                   "or handoff_files for a specialist), or finish without it.")
SEND_AGAIN_FIRST = ("Send {name} again first (deliver_file, run {run_id}, file {file_id}); "
                    "its delivery is not confirmed.")
LIVE_UNSENT = ("The live-view button did not reach the operator, and it cannot be sent again "
               "while the run waits for them: tell them in one plain line that it waits for them "
               "on Casa Browser's page.")
LIVE_WAITING = ("The operator has the live view. End your turn; the run carries on after they "
                "press Done.")
DOWNLOADED = "Downloaded {safe_name} ({mime}, {size} bytes) as file {file_id}."
ARCHIVED = "Zipped into {safe_name} ({size} bytes) as file {file_id}."
CONTINUATION_ENDING = ("This is a continuation turn: if it only made progress the operator "
                       "was already told about, your whole answer is exactly <silent/> and "
                       "nothing else; otherwise answer normally.")
SCROLL_MEMBERS = "A scroll needs direction and amount, and only a scroll takes them."
TYPE_NEEDS_TEXT = "Typing needs text."
SELECT_NEEDS_CHOICE = ("Selecting needs choice_handle: the handle of one of the option lines the "
                       "observation lists right after the dropdown, never the option's text.")
ENTER_ONLY_TYPE = "Only typing takes enter."
TEXT_ONE_LINE_LONG = ("One-line text stops at 2,000 characters; only text with line breaks may be "
                      "longer, up to 32,000.")
TEXT_PLAIN = ("Typed text must be plain characters (no tabs or other control characters); "
              "line breaks only into a field shown as multi-line; enter: true submits one-line "
              "text.")

# -- the check of a step (action model) -----------------------------------------
STEP_ASKED = ("The operator must confirm this step: call post_question with run_id (it carries "
              "the page's picture), then end your turn; the answer continues the run.")
#: the same, when a plan could still make this one question cover the whole job
STEP_ASKED_PLAN = ("The operator must confirm this step. If the job will make more changes of "
                   "this kind, do not post this question: call run_act again on this same step "
                   "with plan naming the whole remaining job, any list in full, so one question "
                   "covers it all. Only if this is the job's one change: call post_question "
                   "with run_id (it carries the page's picture), then end your turn; the answer "
                   "continues the run.")
STEP_LOOK_FIRST = "Not done. The check says to look first: {note}"
STEP_MOVED = "The click did not land on that element; observe again."
STEP_NOT_RUN = "That step did not run ({reason}); observe again."
#: why a typing step did not run, nothing judged
STEP_NOT_RUN_BY_REASON = {
    "not_typable": ("That field does not take typing (it is read-only): it is usually a picker. "
                    "Click it, or the icon beside it, observe, and choose from what opens."),
    "value_format": ("That field did not take the text: give the value in the browser's own "
                     "format for its kind (time 19:30, date 2026-11-03, datetime-local "
                     "2026-11-03T19:30, month 2026-11, week 2026-W45, color #ff0000, a number "
                     "on a range's scale), whatever the page displays."),
    "no_enter": ("That step takes no enter (a field with a kind, or text with line breaks): "
                 "type it without enter, then press the page's own button as its own step."),
    "one_line": ("That field takes one line: line breaks go only into a field shown as "
                 "multi-line, never into a search. Type it as one line, or use the live view "
                 "when it really needs line breaks."),
    #: why a target could not be pressed; nothing was sent
    "not_visible": ("That element is hidden right now: open what holds it first (a menu, a tab, "
                    "a section), observe, then try again."),
    "disabled": ("That control is disabled right now: the page usually wants something first "
                 "(a required field, a choice); do that, observe, then try again."),
    "no_size": ("That element takes no space on the page: something else is usually drawn for it "
                "(a label, a styled box). Look at the page and click what you see beside it."),
}
TYPED_NOT_SHOWN = ("The field does not show what you typed: the page changed or refused it. "
                   "It may be a widget: click it, or the icon beside it, and choose from what "
                   "opens.")
STEP_CHECK_UNAVAILABLE = ("The check is not available; tell the operator the run cannot go "
                          "on right now.")
STEP_OUTCOME_UNKNOWN = ("That step may or may not have happened, and it is not tried again. "
                        "Tell the operator you could not confirm it went through and ask them "
                        "to check.")
STEP_LAPSED = "That question lapsed and nothing was pressed ({reason}); observe again."
PAGE_KEEPS_CHANGING = "The page keeps changing; observe again."

# -- cards and taps -------------------------------------------------------
NOT_NOW = "Not now"
FORGET_TEXT = "Forget Fido's saved login for {site}? The item in the vault stays."
FORGET_LOGIN = "Forget login"
KEEP = "Keep"
KEEP_GOING_TEXT = "Still on {task} ({minutes} min). Fido keeps going; tap Stop to end it."
SO_FAR = "So far: {done}."
KEEP_GOING = "Keep going"
STOP = "Stop"
RECEIPT_ANSWERED = "That question was already answered."
RECEIPT_FORGOT = "Forgot the {site} login. The vault item stays; next time I'll ask which one to use."
RECEIPT_KEPT = "Kept the {site} login."
RECEIPT_KEEP_GOING = "Fido keeps going."
RECEIPT_STOPPED = "Stopped."
#: the answer of every call on a request the operator stopped themselves
STOPPED = ("The operator stopped this browsing themselves and has been told. Make no more calls for it and "
           "tell no one: your whole answer is exactly <silent/> and nothing else, in a first "
           "turn too.")
RECEIPT_NOT_TAKEN = "Casa Browser did not take that answer; Fido will ask again."
UNKNOWN_TAP = "That button is no longer valid."
FORGET_ASKED = "Asked the operator whether to forget the {site} login."
KEEP_GOING_ASKED = "Asked the operator whether to keep going; carry on driving meanwhile."
SCREENSHOT_SENT = ("Handed the screenshot to Casa for the operator. Your reply is the one short "
                   "plain line with the outcome; no recap, no sources.")
SHOT_NOT_RUNNING = ("The page can be shown only while the run is running; send the step's "
                    "screenshot by its id instead.")
NOT_A_PNG = "That screenshot is not available."

# -- links ---------------------------------------------------------------------
LIVE_LABEL = "Open live view"
#: the hand-over is this one message; Done is named here only. Casa heads it
#: with Fido's own label line, so the caption does not name Fido again
LIVE_CAPTION = "{reason}. Press Done on that page when you have finished."
LIVE_SENT = ("Sent the operator the live view with your reason; that message is the whole "
             "hand-over. {ending} The run carries on after they press Done there.")
#: how a turn ends once the operator has a hand-over (a question card, a live view):
#: for their assistant, which Casa already told that they have it, with nothing; for a specialist that
#: asked, a first turn with one line saying what the run waits for
HANDED_OVER_SILENT = ("The operator already has it and their assistant is told so: end your turn now, and "
                      "your whole answer is exactly <silent/> and nothing else, in a first turn "
                      "too.")
HANDED_OVER_LINE = ("End your turn now: a first turn ends with one short line to whoever asked "
                    "saying what the run waits for (\"Waiting for the operator in the live "
                    "view.\"), a continuation turn silently.")
SETUP_LABEL = "Open Casa Browser"
SETUP_CAPTION = "Casa Browser's page for {site}."
SETUP_SENT = "Sent the operator a button to Casa Browser's page for {site}."

# -- files, beats, sources -------------------------------------------------------
FILE_FOR_SPECIALIST = "This run's requester is a specialist: hand files over with handoff_files."
FILE_FOR_OPERATOR = "This run's requester is the operator's assistant: send files with deliver_file."
FILE_NEEDS_ZIP = ("{name} cannot go to Telegram as it is: zip it with run_archive (in groups "
                  "under 19 MB) and send the zip.")
FILE_TOO_BIG = "{name} is too large to send in Telegram ({size} MB). Offer another route."
FILE_HANDOFF_TOO_BIG = "{name} is too large for the handoff folder ({size} MB)."
FILE_SENT = ("Handed {name} to Casa for the operator; Casa reports whether it arrived, and "
             "the next call says so if it did not.")
DOWNLOAD_BLOCKED = ("Casa Browser could not fetch that file (refused, failed, or the site "
                    "did not send the file itself). Try another link or button on the page.")
HANDED_OFF = "Handed off: {items}. Give whoever asked these paths."
HANDOFF_FAILED = "The handoff folder refused {name} ({code})."
BEAT_TOO_SOON = "Too soon for another update; the last one was under 90 seconds ago."
BEAT_SENT = "Handed the update to Casa for the operator."
SOURCES_HEADER = "Sources:"
SOURCES_FOR_SPECIALIST = "Cite the sources by site and title in your answer."
SOURCES_NONE = ("None of those are this run's sources, so nothing was sent: reply with the "
                "answer as usual and cite its sources by site and title.")
SOURCES_PREPARED = ("Prepared your answer with {n} source(s) beneath it as one message for the "
                    "operator; Casa reports whether it arrived. When it did, it is your whole "
                    "answer and lists the sources: {ending}")
SOURCES_LEFT_OUT = " Left out of it: {ids}."
#: show_sources finishes the run itself, so no continuation answers again; when it cannot
#: (a file not yet confirmed, or the app refused), it asks for run_finish, never the answer again
SOURCES_FINISHED = ("Prepared your answer with {n} source(s) beneath it as one message for the "
                    "operator, and finished the run ({status}).{after} Casa reports whether the "
                    "message arrived. When it did, it is your whole answer and lists the "
                    "sources: {ending}")
SOURCES_FINISH = ("finish the run now with run_finish (expectation_status as it honestly "
                  "stands) and end your turn as its result says.")
#: v0.1.27: a run's answer goes to the operator once, whatever became of the first post
SOURCES_ALREADY = ("This run's answer was already sent to the operator with its sources: never send it "
                   "again. If the run is still going, finish it with run_finish; end your turn "
                   "as its result says.")
#: v0.1.27: a continuation of a run whose answer the operator already has (woken for its files only)
ANSWERED_BRIEF = ("This run's answer already went out with show_sources: do not answer again "
                  "or redo the task. "
                  "Your only job is the files named here, then run_finish; end your turn as "
                  "its result says.")

# -- the question in Telegram (action model) -------------------------------------
NO_QUESTION = "Casa Browser has no question waiting for that run."
QUESTION_NOT_OPEN = "That question is no longer open ({state}); observe the run again."
CARD_IN_FLIGHT = ("The question with its picture is still being delivered; call post_question "
                  "again in a moment.")
QUESTION_POSTED = ("Asked the operator: they have the question with its picture and buttons; do "
                   "not repeat or describe it. {ending} The answer continues the run.")
QUESTION_POSTED_NO_CONFIRM = ("Asked the operator; the picture did not reach them, so the question "
                              "offers only the live view; do not repeat it. {ending} The answer "
                              "continues the run.")
QUESTION_WAITING = "A question waits with no card: call post_question with run_id."
QUESTION_WITHHELD = "The question did not reach the operator: post it again with post_question."
ANSWER_IN_PROGRESS = ("The operator answered the question; Casa Browser is finishing it. End "
                      "your turn; the result continues the run.")
ANSWER_APPLIED = "The operator's answer was applied: {receipt}"
LIVE_POSTED = "The operator has the live view and the question again."
LIVE_NOT_OPENED = "The live view could not be opened ({code})."
# what the operator reads after a tap (short; says what happened)
R_DONE = "Done: {label}."            # on the tapped card, not a message
R_ASK_AGAIN = "The page changed, so I didn't press it. I'll ask you again."
R_CHANGED = "The page changed, so I didn't press it."
#: a Yes the app could not check again, when it brings no copy of its own: the page need not
#: have changed
R_NOT_CHECKED = "I couldn't check this page again, so I didn't press it. You can do it in the live view."
#: the question lapsed, the step was judged afresh on the page as it stood, and it ran
R_RAN_AFRESH = "The page changed, so the check looked again, and it went ahead."
R_GONE = "I couldn't find that on the page any more, so I didn't press anything. It may already be done."
R_CHECK_DOWN = "The check isn't available, so I didn't press it."
R_UNKNOWN = "I pressed {label} but couldn't confirm it went through. Can you check?"
R_DECLINED = "OK, I didn't press {label}."
R_DECLINED_PLAIN = "OK, I left it."
R_CHOSEN = "OK: {label}."
R_NOT_CURRENT = "That question is no longer current."
R_LIVE_FIRST = "Press Done in the live view first."
R_LATER = "I couldn't reach the browser; I'll finish this shortly."
R_NOT_TAKEN = "Casa Browser didn't take that answer, so nothing was done. I'll ask again."
R_UNSURE = "Something went wrong; I'll check what happened."
LIVE_LINE = "Live view: [Open Casa Browser]({url}). Press Done there, then answer here."
OPTIONS = "Options: {options}."
#: atop a question that follows the operator's lapsed Yes (the app's `after_lapse`): the step did not run
AFTER_LAPSE = "Your last Yes didn't go through: the page changed before the step ran. This is a new question."
YES_PREFIX = "Yes, "                    # before the check's confirm label

# -- logins (action model) ------------------------------------------------------------
#: the login question's card (fixed copy; host and titles escaped by `cards.md`)
LOGIN_ONE = "To sign in to **{host}** I'd use your 1Password item **\"{title}\"**. OK?"
LOGIN_MANY = "To sign in to **{host}**, which 1Password item?"
YES = "Yes"
NO = "No"
NONE_OF_THESE = "None of these"
#: receipts of the login question's buttons
R_LINKED = "OK, I'll sign in with \"{label}\"."
R_NO_ITEM = "OK, I won't use a 1Password item."
R_NOT_LINKED = "I couldn't use that item, so nothing was linked."
#: the sign-in card for a site that is not set up (the app's host, label and title)
SITE_PLAIN = "**Sign in to {host}** ({name})?"
SITE_ONE = "**Sign in to {host}** ({name}) with your 1Password item **\"{title}\"**?"
SITE_SAVES = "Yes saves it as one of your sites in Casa Browser."
#: receipts of the sign-in card's buttons
R_SIGNING_IN = "OK, signing in to {label}."
R_SITE_DECLINED = "OK, I won't sign in. Nothing was saved."
R_SITE_NOT_OPENED = "Casa Browser saved {label}, but its sign-in page didn't open."
R_SITE_NOT_SAVED = "Casa Browser couldn't save {label}, so I didn't sign in."
R_SITE_ALREADY = "A site for {label} was saved meanwhile, so I didn't sign in. Ask me again."
#: ask_sign_in results (to Fido)
SIGN_IN_ASKED = ("Asked the operator whether to sign in to this site: they have the card with the "
                 "page's picture; do not repeat or describe it. {ending} Their Yes saves the site "
                 "and starts signing in; the result continues the run.")
SIGN_IN_JUDGING = ("Casa's check of the 1Password items did not finish; call ask_sign_in again "
                   "in a moment.")
SIGN_IN_NOT_CONFIGURED = ("Casa Browser has no 1Password set up, so it cannot sign in anywhere: "
                          "send site_setup_link and say so.")
SIGN_IN_ACCOUNT_SITE = "This page already belongs to a site this run signs in to."
SIGN_IN_REGISTERED = ("This page belongs to the operator's site {site}: start an account run "
                      "for it instead.")
SIGN_IN_NOT_HTTPS = "Casa Browser signs in only on https pages; this page is not one."
SIGN_IN_PAGE_CHANGED = ("The page changed while Casa Browser read it: observe, and call "
                        "ask_sign_in again on the sign-in page.")
SIGN_IN_QUESTION_OPEN = "A question is already waiting for the operator: post_question shows it."
#: notes the plugin's relay adds to a tool result (no model involved in the relay itself)
LOGIN_ASKED = ("Casa Browser asks the operator which 1Password login to use for this site: call "
               "post_question with run_id {run_id}, then end your turn.")
LOGIN_NONE = ("No 1Password login fits this site: ask the operator to sign in once, with "
              "request_live_view (purpose login).")
LOGIN_SAVED = ("Casa Browser saved the {site} login the operator used in the live view. Tell "
               "them in your reply: \"Casa Browser saved your {site} login.\"")
LOGIN_KEPT = ("The operator signed in with a different password than the {site} 1Password item "
              "holds, and the item was left as it is. Tell them once.")
PICTURE_WITHHELD_COPY = "I can't show you this page reliably; you can do it in the live view."

# -- sign-in codes (action model) ---------------------------------------------------
#: request_login_code: the one sentence the turn ends with (to whoever asked; fixed copy)
CODE_REQUEST = ("The {site} site emailed a sign-in code. Please find it in Gmail (only an email "
                "from {site}, received in the last few minutes) and send it back to me with: "
                "run {run_id}, request {token}, code <the code>.")
CODE_REQUESTED = ("End your turn now with exactly this line and nothing else; the run waits up "
                  "to 10 minutes for the code:\n{line}")
NO_CODE_WAITING = "This run is not waiting for a sign-in code."
CODE_REQUEST_UNKNOWN = ("That sign-in code request is not open any more (already answered, "
                        "expired, or not yours). Tell whoever asked in one plain line.")
#: run_login_code results in the run's own turn
CODE_SIGNED_IN = "Signed in. Carry on with the task: observe the page."
CODE_AGAIN = ("The site asks for another sign-in code. Look at the page: if it was emailed, call "
              "request_login_code again; otherwise offer the login live view.")
CODE_NOT_SIGNED_IN = ("Casa Browser could not finish the sign-in ({reason}). Offer the login "
                      "live view (request_live_view, purpose login).")
#: run_login_code results in the requester's turn: the one line Fido replies with (no run details)
CODE_HANDED = ("The code is in and sent; the run's next turn reads the page to see whether it "
               "worked. Reply with exactly one line: \"I passed the code to {site}; I'm carrying "
               "on with the task.\"")
CODE_HANDED_AGAIN = ("The site asks for one more code. Reply with exactly one line: \"{site} asks "
                     "for another sign-in code; I'll ask for it.\"")
CODE_HANDED_FAILED = ("The sign-in did not finish. Reply with exactly one line: \"{site} did not "
                      "accept the sign-in; I'll send you a live view to sign in there.\"")
CODE_HANDED_UNSURE = ("Reply with exactly one line: \"I passed the code on; I'm carrying on with "
                      "the task.\"")
CODE_HANDED_NOT_WAITING = ("Casa Browser was no longer waiting for that code. Reply with exactly "
                           "one line: \"That sign-in code came too late; I'll carry on another "
                           "way.\"")
#: continue_run while the run waits for the code it asked for
CODE_WAITING = ("This run is waiting for the sign-in code you asked for. End your turn now: "
                "your whole answer is exactly <silent/>.")
CODE_NEVER_CAME = ("The sign-in code you asked for did not come in 10 minutes. Offer the login "
                   "live view (request_live_view, purpose login).")
