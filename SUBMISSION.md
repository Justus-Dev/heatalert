# Submission draft: HeatWatch

> Everything below can be pasted as-is.

## Tracks

- **Primary track:** Ring (priority categories: caretaking, accessibility, IoT home automation)
- **Mini challenge:** Open Source (MIT-licensed project + upstream fix and feature for `ring-sandbox`)

## One-liner

HeatWatch alarms on what *doesn't* happen: no signs of life at home. It uses the front
door to know whether anyone is home, tightens its thresholds during heatwaves, and asks
via the Ring chime before it alarms the family.

## What we built during the submission window

Everything in this repository is new: backend (FastAPI), presence/heat/escalation logic,
Ring Partner API integration, optional Bedrock integration, per-contact family web app with an
optional access code, crash-safe state persistence, device-outage handling, demo console,
four scenarios, unit and end-to-end tests, and docs.

## Product feedback

### Ring Partner API (via the `ring-sandbox` emulator + Playground)
- **Used for:** device discovery with `?include=status,capabilities,configurations`;
  webhooks (motion, doorbell, contact and climate sensors, online/offline); chime playback as
  a check-in tone; snapshot download (303 flow) for emergencies with consent; event history.
- **Worked well:** JSON:API side-loading gives capabilities, chime slots and sensor
  thresholds in one call. HMAC-signed webhooks with `meta.time` inside the signed body make
  replay protection straightforward.
- **Needs work:** no official simulator or sandbox; 30-minute Playground tokens; no direction or
  presence signal; chime playback has no confirmation; no "inactivity" primitive (see
  friction log).
- **Onboarding:** docs are clear on shapes. The hard part is not having devices.
- **Would build again:** yes. The chime + sensor surface is ideal for caretaking.

### ring-sandbox (community emulator)
- **Used for:** the whole demo: virtual devices, signed webhook delivery, chime and
  snapshot endpoints, event injection.
- **Worked well:** realistic shapes, `webhooks.parse(max_age_s=…)`, fixture record/replay.
- **Needs work:** `play` back-dates timestamps (hard to pair with a compressed demo clock);
  history entries had no `sub_type` attribute and scenario steps rejected app metadata.
  We contributed a fix and a feature with tests for both.

### Amazon Bedrock (Claude, via the Anthropic SDK's `AnthropicBedrockMantle` client)
- **Used for:** the gentle heat-day care hint and the on-demand day summary
  (`anthropic.claude-opus-5-5`, effort `low`). Safety alerts are deliberately fixed
  templates and never wait on a model.
- **How:** `app/ai.py`. It detects AWS credentials, calls the Messages API on Bedrock with a short
  system prompt, handles `refusal`, times out after 20 s and falls back to templates.
- **Status:** integration is in the code but was **not tested against live Bedrock**: we
  had no AWS account set up in time. The demo runs on the template fallback, which the UI
  labels as such. For that reason we are not entering the AWS Builder mini challenge.

### Open-Meteo
- **Used for:** live forecast (feels-like maximum) and **historical archive replay** of a
  real heatwave day (Berlin, 20 Jul 2022: 37.7 °C, feels like 36.4 °C).
- **Worked well:** no key, fast, archive API is perfect for honest demos.

## Open Source: required details

- **Repo URL:** https://github.com/Justus-Dev/heatalert (MIT)
- **Contribution URL:** https://github.com/josepha-mayo/ring-sandbox/pull/20 (fork branch: https://github.com/Justus-Dev/ring-sandbox/tree/fix/history-sub-type-and-step-meta)
- **GitHub username:** Justus-Dev
- **What / how / why:** A contribution to `ring-sandbox`, the community Ring Partner API
  emulator HeatWatch is built on, with tests:
  1. **Fix:** `HistoryEventAttributes.sub_type` is declared (`None`, since history withholds
     it). Before, `ev.attributes.sub_type` raised `AttributeError` on every history entry,
     so any loop over `client.events()` that printed or branched on it crashed.
  2. **Feature:** scenario steps accept a `meta:` mapping and `x-`-prefixed keys
     (collected into `Step.meta`, never sent to the emulator). Apps can annotate their own
     scenario files (labels, app options) and still replay them with `ring-sandbox play`.
  3. **Better errors:** an unknown step key now names the key and step index with a hint,
     instead of a raw dataclass `TypeError`.
  Both new tests fail without the change. The full upstream suite, `ruff check` and
  `ruff format` pass. Details: `contrib/ring-sandbox/PULL_REQUEST.md`.

## Feature requests

| Request | Why | Urgency |
|---|---|---|
| Official Ring sandbox (virtual devices, event injection, webhooks to dev URL, long-lived tokens) | Most hackathon builders have no devices; 30-min tokens break iteration | critical |
| Presence / home-away signal via Partner API (with consent) | Inactivity and caretaking apps need to know if anyone is home | important |
| Chime playback confirmation webhook + custom audio for app slots | Check-ins need to know the tone played; a spoken prompt is more accessible | important |
| Inactivity subscription (`no_motion_for`) | Every caretaking partner rebuilds the same timer | nice-to-have |

## Friction log

See `FRICTION_LOG.md` (8 entries with severity, workaround and suggestion).

## Demo video (≈ 2:30) shot list

1. **0:00 Problem (15 s):** heatwaves, older people alone, "a fall makes no event".
2. **0:15 Setup and consent (20 s):** setup screen, privacy list, consent checkbox.
3. **0:35 Proof of real API (15 s):** demo console: `GET /v1/devices`, signed webhooks
   counter, and the terminal running `scripts/ring_playground_check.py` with a Playground token.
4. **0:50 Heat-day emergency (45 s):** ×120 replay; shopping trip (left / came home), cat
   ignored, indoor heat sensor; check-in tone at 13:05 → Anna. Switch "Whose phone" to
   Anna and tap "I'm on it". Mention: if she never confirms, HeatWatch asks again after
   30 min and then alerts Mr. Weber.
5. **1:35 Same day, normal weather (20 s):** replay with "Normal": still quiet at 13:05;
   check-in only at 15:05.
6. **1:55 False alarm caught (25 s):** nap scenario; chime → movement → "nobody was alerted".
   Then the 17:00 care hint (template text; Bedrock-written when AWS credentials are set).
7. **2:20 Close (10 s):** door logic (out for the day, no alarm), devices failing safely
   (`device_trouble`), privacy, open source.
