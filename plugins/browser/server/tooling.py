"""The tool registry, the argument check every call passes first, and the
result envelope every delivering tool returns.

A tool is registered with `@tool(name, description, schema, slot=None)`:

* `schema` is a JSON Schema subset (`object` with `properties`, `required`,
  `additionalProperties: false` always; `string` with `maxLength`,
  `minLength`, `pattern`, `enum`; `integer` with `minimum`/`maximum`;
  `boolean`; `array` with `items`, `minItems`, `maxItems`). Arguments are
  checked against it before the tool runs; a failure is the only `isError`
  result besides the setup tool's "not paired", and the tool body is never
  reached — so a refused argument makes no app or Casa call.
* a tool with `slot` is a Casa capability tool: its function returns only
  the note; `call` wraps every outcome — argument errors included — into the
  envelope `{"<slot>": "<reference>", "note": "…"}` after a deposit, or
  `{"<slot>": null, "note": "…"}` when nothing was deposited (Casa's explicit
  no-post shape); any other result would make Casa withhold the post.

Failures become one fixed sentence plus the failure's code word, never text
the app or Casa wrote.
"""
from __future__ import annotations

import json
import re

import copy_text
from app_client import AppError, sentence_for
from casa_broker import BrokerFailed, DepositFailed

TOOLS: dict[str, dict] = {}
#: the calls this process has served: one process per turn, so this turn's (the step bound)
calls_made = 0
#: this call's run, counted over all that run's turns (the loop catch); 0 for no run
run_calls = 0
#: sentences code wants on the current call's result (delivery outcomes)
_NOTES: list[str] = []
#: the picture the current call returns beside its text, base64
_IMAGE: str | None = None
#: a longer result is cut: above 50,000 characters the CLI moves it to a file Fido cannot open
MAX_RESULT_CHARS = 48_000



def note(sentence: str) -> None:
    """Add a sentence to the result of the tool call now running."""
    if sentence not in _NOTES:
        _NOTES.append(sentence)


def attach_image(b64: str) -> None:
    """Return this picture beside the text of the tool call now running."""
    global _IMAGE
    _IMAGE = b64


def take_image() -> str | None:
    """The picture of the call that just ended, once."""
    global _IMAGE
    image, _IMAGE = _IMAGE, None
    return image


UNKNOWN_MEMBER = "(an argument this tool does not take)"


class ToolError(Exception):
    """A fixed sentence returned with `isError: true`."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


class Refused(Exception):
    """A fixed sentence returned as an ordinary (non-error) result."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


class Delivery:
    """The deposit a capability tool made during this call, if any."""

    def __init__(self, slot: str):
        self.slot = slot
        self.reference: str | None = None

    def envelope(self, note: str) -> str:
        return json.dumps({self.slot: self.reference, "note": note}, ensure_ascii=False)


def failure_sentence(exc: BaseException) -> str:
    if isinstance(exc, AppError):
        return f"{sentence_for(exc)} ({exc.code})"
    if isinstance(exc, (BrokerFailed, DepositFailed)):
        return copy_text.CASA_FAILED.format(code=exc.code.split(":", 1)[0])
    if isinstance(exc, (Refused, ToolError)):
        return exc.text
    return copy_text.TOOL_FAILED


# -- argument check -------------------------------------------------------------------

def _check(value, schema: dict, path: str) -> str | None:
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return path or "arguments"
        props = schema.get("properties", {})
        for key in value:
            if key not in props:
                # never the caller's own key: it may carry anything
                return f"{path}{UNKNOWN_MEMBER}"
        for key in schema.get("required", []):
            if key not in value:
                return f"{path}{key}"
        for key, sub in props.items():
            if key in value:
                why = _check(value[key], sub, f"{path}{key}.")
                if why:
                    return why
        return None
    name = path.rstrip(".") or "arguments"
    if kind == "string":
        if not isinstance(value, str):
            return name
        if len(value) > schema.get("maxLength", 10_000) or len(value) < schema.get(
                "minLength", 0):
            return name
        if "pattern" in schema and not re.fullmatch(schema["pattern"].strip("^$"), value):
            return name
        if "enum" in schema and value not in schema["enum"]:
            return name
        return None
    if kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return name
        if value < schema.get("minimum", -2 ** 53) or value > schema.get("maximum", 2 ** 53):
            return name
        return None
    if kind == "boolean":
        return None if isinstance(value, bool) else name
    if kind == "array":
        if not isinstance(value, list):
            return name
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 1000):
            return name
        for i, item in enumerate(value):
            why = _check(item, schema["items"], f"{path.rstrip('.')}[{i}].")
            if why:
                return why
        return None
    return name


def check_arguments(arguments, schema: dict) -> str | None:
    """The first argument that does not fit, by name, or None."""
    return _check(arguments, schema, "")


# -- schema helpers ----------------------------------------------------------------------

ID = {"type": "string", "pattern": r"^[A-Za-z0-9_-]{1,64}$"}


def text(limit: int, minimum: int = 1) -> dict:
    return {"type": "string", "maxLength": limit, "minLength": minimum}


def enum(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


def ids(minimum: int, maximum: int) -> dict:
    return {"type": "array", "items": ID, "minItems": minimum, "maxItems": maximum}


def obj(required: list[str] | None = None, **props) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props),
            "additionalProperties": False}


# -- registration and dispatch --------------------------------------------------------------

def tool(name: str, description: str, schema: dict, *, slot: str | None = None,
         ending: str | None = None, receipt: bool = False):
    """Register a tool. `ending`, when given, is appended as the last line of
    EVERY result of the tool — argument errors and failures included. A
    `receipt` tool's result is JSON Casa itself parses: no note is added to it, and it is never
    cut."""
    def register(fn):
        TOOLS[name] = {"description": description, "schema": schema, "fn": fn, "slot": slot,
                       "ending": ending, "receipt": receipt}
        return fn
    return register


def call(name: str, arguments) -> tuple[str, bool]:
    """Run one tool: `(text, is_error)`. The ONLY producer of a tool's result:
    every outcome — an argument of any wrong type, a refusal, a failure, a
    result — goes through `_finish`, so a delivering tool always answers its
    envelope and a tool with an ending always ends with it."""
    global calls_made, run_calls, _IMAGE
    calls_made += 1                     # every call takes a model turn, refused or not
    run_calls, _IMAGE = 0, None
    spec = TOOLS.get(name)
    if spec is None:
        return copy_text.UNKNOWN_TOOL, True
    if arguments is None:
        arguments = {}
    _NOTES.clear()
    delivery = Delivery(spec["slot"]) if spec["slot"] else None
    bad = check_arguments(arguments, spec["schema"])
    if bad is not None:
        sentence = (copy_text.UNKNOWN_ARGUMENT if UNKNOWN_MEMBER in bad
                    else copy_text.BAD_ARGUMENTS.format(name=bad))
        return _finish(spec, delivery, sentence, True)
    if isinstance(arguments.get("run_id"), str):
        run_calls = _count(arguments["run_id"])
    try:
        if delivery is None:
            text_ = spec["fn"](arguments)
        else:
            text_ = spec["fn"](arguments, delivery)
        if _stopped(arguments):
            return _finish(spec, delivery, copy_text.STOPPED, False, ending=False)
        return _finish(spec, delivery, text_, False)
    except ToolError as exc:
        if _stopped(arguments):
            return _finish(spec, delivery, copy_text.STOPPED, False, ending=False)
        return _finish(spec, delivery, exc.text, True)
    except BaseException as exc:  # noqa: BLE001 — a fixed sentence, never the text
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if (isinstance(exc, Refused) and exc.text == copy_text.STOPPED) or _stopped(arguments):
            # refused before any app call (the seal of the operator's Stop), or the call the Stop cut: the
            # Stop's own classification keeps the run's ending, not this call
            return _finish(spec, delivery, copy_text.STOPPED, False, ending=False)
        if (isinstance(exc, AppError) and exc.code == "interrupted"
                and isinstance(arguments.get("run_id"), str)):
            # the app answered that this run was interrupted; this turn has heard it
            import state
            try:
                state.heard_ending(arguments["run_id"])
            except Exception:  # noqa: BLE001 — the turn's answer stands; at worst told twice
                pass
        return _finish(spec, delivery, failure_sentence(exc), False)


def _count(run_id: str) -> int:
    """The loop catch's count; an unreadable count never costs the call its result."""
    import state
    try:
        return state.count_call(run_id)
    except Exception:  # noqa: BLE001
        return 0


def _stopped(arguments: dict) -> bool:
    """A call's result is rewritten to STOPPED only when the APP says the
    run's request was stopped (its `cancelled_requests` record, read as `stopped` on the run).
    The local seal only says whether to ask: with none, no Stop of this request ever began. An
    app that cannot be read leaves the result as it is."""
    run_id = arguments.get("run_id")
    if not isinstance(run_id, str):
        return False
    import state
    try:
        if not state.run_stopped(run_id):
            return False
        import app_client
        import wire
        client = app_client.client_from_env()
        return wire.parse_run(client.request("GET", f"/v1/runs/{run_id}")).get("stopped") is True
    except Exception:  # noqa: BLE001 — unreadable: the call's answer stands
        return False


def _finish(spec: dict, delivery: "Delivery | None", text_: str, is_error: bool, *,
            ending: bool = True) -> tuple[str, bool]:
    global _IMAGE
    if text_ == copy_text.STOPPED:
        _NOTES.clear()                  # nothing more for this call: the operator's Stop says it
        _IMAGE = None
    if len(text_) > MAX_RESULT_CHARS and not spec.get("receipt"):
        text_ = text_[:MAX_RESULT_CHARS - len(copy_text.RESULT_CUT)] + copy_text.RESULT_CUT
    if _NOTES and not spec.get("receipt"):
        text_ = "\n".join([text_, *_NOTES])
    _NOTES.clear()
    if delivery is not None:
        text_ = delivery.envelope(text_)
    if ending and spec.get("ending"):
        text_ = f"{text_}\n{spec['ending']}"
    return text_, is_error
