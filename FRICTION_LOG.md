# Friction log

Each entry: task · steps · expected vs. actual · severity · workaround · suggestion.
Severity: **critical** (blocked) · **high** (major detour) · **medium** (cost real time) · **low** (annoyance).

---

### 1. Building a Ring integration without a Ring device

- **Task:** Develop and demo a caretaking app for the Ring track without owning hardware.
- **Steps:** Read the Ring Partner API docs; looked for an official simulator; found only the Playground token.
- **Expected:** An official simulator or sandbox account with virtual devices that emit webhooks.
- **Actual:** No official simulator. The Playground token lasts ~30 minutes, which is too short for iterating on webhook-driven logic over a day of simulated events.
- **Severity:** high
- **Workaround:** Used the community emulator `ring-sandbox` (MIT, built during this hackathon), which speaks the Partner API's JSON:API shapes and signs webhooks. Added `scripts/ring_playground_check.py` to make real Playground calls with the same client.
- **Suggestion:** Ship an official Ring sandbox: virtual devices per developer account, an "inject event" API, webhook delivery to a dev URL, and long-lived sandbox tokens.

### 2. Time-compressed demo with `ring-sandbox play`

- **Task:** Show "2 hours without movement" in a 2-minute video.
- **Steps:** `ring-sandbox play scenario.yml --speed 60`.
- **Expected:** Events delivered *now*, paced in compressed time, so a backend running a compressed clock sees them in order.
- **Actual:** `play` back-dates all event timestamps onto a virtual clock that ends "now". Pacing works, but the timestamps do not line up with a consumer running its own demo clock.
- **Severity:** medium
- **Workaround:** HeatWatch's own `Director` injects each step via `POST /_sandbox/events` at the moment its demo clock reaches it. The emulator still signs and delivers real webhooks.
- **Suggestion:** A `--live-timestamps` flag for `play`, or a shared "sandbox clock" endpoint that consumers can read.

### 3. Chime playback has no observable outcome

- **Task:** Play a check-in tone and confirm it was played.
- **Steps:** `POST /v1/devices/{chime}/media/audio/playback` with `{"event": "ring-appstore-event-1"}`.
- **Expected:** Some confirmation (history entry or webhook such as `chime_played`).
- **Actual:** `204 No Content` and nothing else. In the emulator the play only shows up in the `/_sandbox/state` journal.
- **Severity:** medium
- **Workaround:** Treat the 204 as "accepted" and show the emulator journal in the demo console.
- **Suggestion:** A playback-completed webhook, and custom audio for app slots (e.g. a spoken "Are you OK? Please wave at the camera").

### 4. Doorbells cannot tell entering from leaving

- **Task:** Decide whether the person is at home.
- **Steps:** Subscribed to doorbell webhooks; looked for a direction field on `motion_detected` and for a home/away resource in the Partner API docs.
- **Expected:** Some direction signal or presence state.
- **Actual:** Doorbell motion is just `motion_detected/human`. Direction needs a contact sensor, which is Early Access.
- **Severity:** medium
- **Workaround:** Door-gated logic: door event → movement inside = home; door event → person outside only = away (`app/presence.py`).
- **Suggestion:** Expose Ring's home/away mode (or a geofence-based presence signal) via the Partner API, with user consent.

### 5. No "no activity" primitive

- **Task:** Detect the *absence* of activity.
- **Steps:** Looked through the webhook event types and the history endpoint for an inactivity or "no motion since" signal.
- **Expected:** A way to be told "no motion on this device or location for N hours".
- **Actual:** Webhooks are only sent when something happens, so every partner has to build its own inactivity timer and state machine.
- **Severity:** low (expected, but worth stating)
- **Workaround:** HeatWatch keeps the last sign of life itself (`app/presence.py`), evaluates thresholds on a timer (`app/rules.py`) and recovers the last motion from `GET /v1/history/...` after a restart.
- **Suggestion:** An optional "inactivity" subscription per device or location (`no_motion_for: PT2H`), useful well beyond caretaking.

### 6. Emulator logs two `on_demand` history entries per snapshot

- **Task:** Fetch a snapshot and list history.
- **Steps:** `POST /v1/devices/{id}/media/image/download` then `GET /v1/history/devices/{id}/events`.
- **Expected:** One `on_demand` entry per media request.
- **Actual:** Two identical entries. `emulator.py` calls `world.record_on_demand(device_id, ts)` twice in `image_download`.
- **Severity:** low
- **Workaround:** None needed for HeatWatch (we filter by `motion.*`).
- **Suggestion:** Remove the duplicate call. Already fixed on upstream `main` after 0.5.0 (unreleased), so it only affects the published 0.5.0 package.

### 7. `HistoryEventAttributes` has no `sub_type`

- **Task:** Print `event_type` and `sub_type` for history entries.
- **Steps:** `for ev in ring.events(cam.id): print(ev.attributes.event_type, ev.attributes.sub_type)`.
- **Expected:** `ev.attributes.sub_type` is `None` when absent.
- **Actual:** `AttributeError` for entries without a sub-type (e.g. `on_demand`). The field is not declared on the model, and it only exists when the JSON happens to include it.
- **Severity:** low
- **Workaround:** `getattr(ev.attributes, "sub_type", None)`.
- **Suggestion:** Declare `sub_type: str | None = None`. Still open on upstream `main` (history withholds `sub_type` there, so the attribute access fails on *every* entry). Fix with a test contributed upstream (see `contrib/`).

### 8. Scenario steps reject unknown keys

- **Task:** Add app-specific metadata to scenario files.
- **Steps:** Added a `label:` key to a step and ran `ring-sandbox play scenarios/heat_day_emergency.yml`.
- **Expected:** Unknown keys ignored, or a clear error naming the key.
- **Actual:** `Step(**s)` raises `TypeError` on unknown step keys. Top-level keys are ignored, though.
- **Severity:** low
- **Workaround:** HeatWatch options live under a top-level `heatwatch:` block, so files stay compatible with `ring-sandbox play`.
- **Suggestion:** Allow an `x-` prefix or a `meta:` field per step. Implemented with tests and contributed upstream (see `contrib/`): both land in `Step.meta`, and unknown keys get an error naming the key and step.

---

### What worked well

- Webhook signing helpers (`webhooks.parse(..., max_age_s=...)`) made replay protection and dedupe trivial to do right.
- The JSON:API `?include=` side-loading gave everything needed (capabilities, chime slots, sensor thresholds) in one call.
- Open-Meteo's archive API lets a demo replay a *real* heatwave day with no API key.
