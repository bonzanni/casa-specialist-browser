# casa-specialist-browser

**Fido**, the browsing specialist for [Casa](https://github.com/bonzanni/ha-casa-app), a
household assistant that runs as a Home Assistant app. Fido fetches, reads and downloads, and fills in
and submits what a task asks for, on the operator's own registered websites and on the open web,
one task at a time; Casa Browser's check asks the operator before a step that needs it. Fido
reports back with its sources and the choices it made.

Fido does not browse by itself. It drives **Casa Browser**, a separate and optional Home
Assistant app that owns the real browser, its network policy, the operator's logins and the
check that asks the operator before a step that needs it. Install Casa Browser from the same app repository as Casa; Fido without Casa Browser
installs, and says plainly that the app is not answering.

## What is in this repository

| Path | What it is |
| --- | --- |
| `manifest.json` | The specialist component Casa installs: component `casa/browser`, role `specialist:browser` |
| `role/` | Fido's role and doctrine |
| `persona/` | The default persona pack `casa/fido` |
| `plugins/browser/` | The bundled MCP plugin `browser`: a standard-library client of Casa Browser's versioned API |
| `scripts/digests.py` | Computes and checks the digests Casa verifies at install |

## Requirements

- **Casa 0.376.0 or later.** Not backward compatible: a Casa older than 0.360.0 refuses this
  component at install, and one older than 0.376.0 refuses its longer cards. Its role declares
  `session.long_runs: true` (which replaces `session.desk_context`, refused since Casa 0.360.0)
  and an `own_chat` block, so Fido talks to the operator from his own Telegram bot rather than
  through the assistant; the block's two texts become that bot's short description and
  description. The component also declares
  plugin tools that call Casa (`callsCasa`) and a driver sweep (`casa.driverSweep`), the
  operator's Stop (`casa.stopTool`, which Casa calls directly with no model, together with
  Casa Browser 0.1.8's request cancel), notes for a long task (`run_read`'s `to_pad`,
  `run_act`'s `text_from`, on the conversation's pad) and the scratchpad's `run`, which the
  doctrine names for computing a paste (x86_64 only; elsewhere Casa answers that it is
  unavailable). Question cards and plan pages are sized by the limits Casa 0.376.0 applies to a
  Telegram rich message (up to 12,000 characters a message); an older Casa refuses a card
  or plan page longer than one 4,096-character Telegram message.
- **Casa Browser** (optional, separate app) speaking protocol version 1. A mismatch is reported
  as "Casa Browser needs updating" or "Fido needs updating" before any browsing. Reading a
  page's rows, plain reads of public pages, the line on a question that follows a lapsed Yes,
  and a tick sent to the check with `ask_first` need Casa Browser 0.1.12 or later.

## Install

Ask Casa's assistant in chat, for example: *"Install the browsing specialist from
bonzanni/casa-specialist-browser"*. Casa's configurator takes the latest published release and
tells you what it brings, and one consent message covers the specialist, its persona and its
plugin. Approve it and the assistant can delegate browsing to Fido. To update later, ask the
assistant to upgrade the browsing specialist; it asks for consent the same way. Install Casa
Browser from Casa's app repository in Home Assistant, then pair the two (below).

## Pairing

Fido holds Casa Browser's pairing secret in its own plugin data directory, never in a chat, a
model argument or an app option. To pair, press **Pair with Casa** on the Casa Browser page and,
within two minutes, ask Fido to pair (Casa also runs the pairing setup once after install).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r tests/requirements.txt
.venv/bin/python -m pytest tests -q -rs
```

Tests that compare this component with Casa's own code run when `CASA_ROOT` names a Casa
checkout (its root); without it they are reported as skipped, with the reason. After editing
anything under `role/`, `persona/pack/`, `config-schema.json` or `plugins/browser/`, stage the
edit and run `python3 scripts/digests.py --write`; CI runs `--check`.

## License

MIT, see `LICENSE`.
