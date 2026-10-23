# HeatWatch

**A Ring-powered safety net for older people who live alone, one that raises the alarm
when *nothing* happens, and gets more careful when it is hot.**

Amazon Developer Hackathon 2026 · Primary track: **Ring** (caretaking, accessibility,
IoT home automation) · Mini challenges: **AWS Builder**, **Open Source**

---

## The problem

Heatwaves kill quietly. Europe's summer of 2022 is estimated to have caused more than
60,000 heat-related deaths (Ballester et al., *Nature Medicine*, 2023). Germany's
Robert Koch Institute estimates around 4,500 for Germany alone that year. Most were people
over 75, many of them living alone. Often nobody noticed for hours.

Fall detection on cameras does not solve this. A person lying on the floor barely moves,
so a camera produces **no event at all** at exactly the wrong moment. And "the camera saw
nobody" can mean asleep, in another room, out shopping, or on the floor.

## The idea

HeatWatch does not try to see an emergency. It notices **the absence of signs of life**
and puts that absence in context:

1. **Is the person at home?** The front door is the gate. *Door opened → movement
   inside* means at home; *door opened → person seen outside only* means they left. Hours
   without movement while someone is out is fine, so there is no alarm.
2. **How long is too long today?** Normally 4 hours without movement (during the day).
   With a heat warning (feels-like ≥ 32 °C, the DWD "strong heat stress" level), or when
   the Ring indoor sensor reports the flat is too warm: **2 hours**. Extreme heat
   (≥ 38 °C): 90 minutes. Night rest is excluded.
3. **Check before alarming anyone.** First the **Ring chime plays a gentle tone** in the
   flat. Movement within 5 minutes means all good and **nobody is alerted**. The person
   needs no phone, no button and no app.
4. **Then escalate step by step:** contact 1 → contact 2 (e.g. a neighbour with a key)
   → everyone, with "call 112" guidance. Movement at any point sends an all-clear.
   An "I'm on it" pauses the escalation, but it has to end in "is OK". After 30 minutes
   HeatWatch asks again, and if there is still no answer it moves on to the next contact.
5. **A gentle care hint, not an alarm:** on a heat day when nobody came or went all day,
   the family gets a short note at 17:00 suggesting a call. It is written by Claude on
   Amazon Bedrock.

### Privacy is part of the design

- HeatWatch reads **only event metadata** (timestamps and types such as motion, door,
  doorbell and too-warm) and deletes it after 24 hours.
- No video is recorded, no live view, no microphone.
- Setup records the **consent** of the person being looked after. HeatWatch does not run
  without it.
- Only in an emergency, and only if the person agreed, is the camera's last snapshot shared,
  and only with the contact who is being alerted.
- Pets are not signs of life (`motion_detected` with `sub_type: animal` is ignored).
- The snapshot image is shown only in the view of the contact it was shared with. Others
  see that it was shared, not the image. Snapshots are kept in memory only, never on disk.

### Built for the bad day

- **Every contact has their own view** (`/?as=c1`): their messages, their buttons
  ("I'm on it", "Still on it", "Margaret is OK"). The family overview (`/`) holds setup
  and settings.
- **An open emergency survives a restart.** Presence, the running escalation, timeline,
  messages and webhook dedupe IDs are written atomically to `data/state.json` and
  restored on startup.
- **Device outages degrade safely and are explained.** Chime offline → no tone, so the
  first contact is alerted directly and told why. Door sensor offline → HeatWatch can't
  see her leave, so it assumes she is home (safe side). Indoor camera offline → checks
  pause and the family is told. Every outage and recovery is a message to contact 1.
- **Optional family access code** (`HEATWATCH_ACCESS_CODE`): signed HttpOnly session
  cookie, constant-time comparison, slowed-down wrong guesses. The Ring webhook endpoint
  stays protected by its HMAC signature instead.

---

## Architecture

```
 Ring devices (or ring-sandbox emulator)          HeatWatch backend (FastAPI)                 Family web app
 ┌──────────────────────────────┐   signed     ┌───────────────────────────────────────┐   ┌────────────────┐
 │ Doorbell "Front Door"        │   webhooks   │ /webhooks/ring                        │   │ Today: status, │
 │ Contact sensor "Front Door…" │ ───────────▶ │  HMAC-SHA256 verify · freshness ·     │   │ quiet-meter,   │
 │ Indoor camera "Living Room"  │  (v1.1, HMAC)│  request_id dedupe                    │   │ heat, timeline │
 │ Chime "Kitchen Chime"        │              │ presence.py  door-gated home/away     │◀─▶│ Messages       │
 │ Climate "Living Room Climate"│ ◀─────────── │ rules.py     heat-adjusted thresholds │   │ Settings,      │
 └──────────────────────────────┘ Partner API  │ escalation.py chime → contacts        │   │ consent setup  │
          ▲                       (RingClient) │ weather.py   Open-Meteo forecast /    │   └────────────────┘
          │ /_sandbox control plane            │              historical archive       │
          └──────── demo director ─────────────│ ai.py        Claude on Amazon Bedrock │
                    (scenario replay)          └───────────────────────────────────────┘
```

| File | Responsibility |
|---|---|
| `app/main.py` | HTTP: webhook receiver, family API, demo controls |
| `app/engine.py` | Wires events → presence → rules → escalation → actions |
| `app/presence.py` | Door-gated home/away detection, last sign of life |
| `app/rules.py` | Heat level (DWD scale), thresholds, night rest, inactivity reference |
| `app/escalation.py` | Chime check-in, contact escalation, acknowledgements, all-clear |
| `app/ring.py` | Ring Partner API client wrapper + emulator control plane |
| `app/weather.py` | Open-Meteo forecast (live) and historical archive (replay) |
| `app/ai.py` | Claude on Amazon Bedrock for the care hint and day summary |
| `app/director.py` | Replays scenario YAML through the emulator on a compressed clock |
| `app/clock.py` | Live clock / demo clock (×60 = one real second is one minute) |
| `app/persist.py` | Atomic JSON state file so an open emergency survives restarts |
| `web/` | Mobile-first family app + demo console (plain HTML/CSS/JS) |
| `scenarios/` | Four demo days (ring-sandbox compatible YAML) |

### Ring Partner API usage (all in code, via `ring_sandbox.RingClient`)

| Endpoint | Used for |
|---|---|
| `GET /v1/devices?include=status,capabilities,configurations` | Discover devices, detect chimes/cameras/sensors, read the climate sensor's temperature threshold and the chime's app audio slots |
| `GET /v1/users/me` | Account shown as connection proof |
| `GET /v1/history/devices/{id}/events?event_types=motion.human,...` | Live mode: recover the last sign of life after a restart |
| `POST /v1/devices/{id}/media/audio/playback` | **The check-in tone** (app audio slot `ring-appstore-event-1`) |
| `POST /v1/devices/{id}/media/image/download` (`latest_in_range`, 303 flow) | Last snapshot, only in an emergency and only with consent |
| Webhooks v1.1 (`motion_detected`, `button_press`, `contact_sensor_*`, `temperature_*`, `device_online/offline`, `device_added/removed`) | Everything HeatWatch reacts to; HMAC-verified on raw bytes, 5-minute freshness window, `request_id` dedupe |

Contact and climate sensors are **Early Access** features of the Ring Partner API.

---

## Run it (3 commands)

Requires Python 3.11+.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./scripts/start.sh                 # starts the ring-sandbox emulator (:8787) and HeatWatch (:8000)
open http://127.0.0.1:8000         # family app (left) + demo console (right)
```

Or start the two processes by hand:

```bash
.venv/bin/ring-sandbox serve --port 8787
.venv/bin/uvicorn app.main:app --port 8000
```

On startup HeatWatch adds the indoor camera and climate sensor to the emulator's default
household and registers its webhook (`/_sandbox/webhooks`) with the shared signing key.

### Demo flow

1. Complete the setup screen (consent is required).
2. In the demo console pick a day, a weather (heatwave or normal, both **observed** Berlin
   weather from the Open-Meteo archive) and a speed, then press **Start replay**.
3. Use **The physical world** buttons to act as the person, e.g. move after the chime,
   or take a device offline.
4. Switch **Whose phone is this?** to Anna or Mr. Weber and answer the alert there.

| Scenario | What it shows |
|---|---|
| `heat_day_emergency` | Shopping trip (left/returned via the door logic), cat ignored, indoor heat from the Ring sensor, no movement after 11:05 → chime 13:05 → Anna 13:10 → Mr. Weber 13:25 → everyone 13:40. **Replay with "Normal" weather: the check-in only comes at 15:05.** |
| `heat_day_false_alarm` | Long nap; chime at 13:20; movement 90 s later; **nobody alerted**. Care hint at 17:00 (Bedrock). |
| `out_for_the_day` | 4.5 h without indoor movement but the door shows she left: **no alarm**. |
| `device_trouble` | Door sensor and chime go offline. HeatWatch assumes she is home, can't play the tone, so it alerts Anna directly and explains why. She comes back → all-clear. |

The scenario files are plain ring-sandbox scenarios, so `ring-sandbox play
scenarios/heat_day_emergency.yml` works too. HeatWatch-only options live under the
`heatwatch:` key, which ring-sandbox ignores.

### Against the real Ring API

```bash
.venv/bin/python scripts/ring_playground_check.py --token <PLAYGROUND_TOKEN>
.venv/bin/python scripts/ring_playground_check.py --token <TOKEN> --record fixtures/
.venv/bin/ring-sandbox serve --port 8787 --fixtures fixtures/   # replay your real devices
```

For production, set `RING_BASE_URL=https://api.amazonvision.com`, `RING_SANDBOX=false`,
`RING_TOKEN`, `WEBHOOK_SIGNING_KEY` and a public `PUBLIC_WEBHOOK_URL` in `.env`.

### Amazon Bedrock (optional)

With AWS credentials available (env vars, `~/.aws`, SSO) and access to Claude on Bedrock,
HeatWatch uses the Anthropic SDK's Bedrock client (`AnthropicBedrockMantle`, model
`anthropic.claude-opus-5-5`, effort `low`) for the care hint and the day summary. Without
credentials it uses fixed templates and says so in the UI. **Alerts never depend on an
LLM.**

### Tests

```bash
.venv/bin/python -m pytest -q
```

Unit tests cover door-gated presence, heat thresholds, night rest, the whole escalation
path, acknowledgement follow-ups, the chime-offline path, state round-trips, and webhook
security (wrong key, tampered body, stale replay, duplicate).

`tests/test_integration.py` starts the **emulator and HeatWatch as real processes** and
checks the full chain over HTTP: access code, false alarm caught by the chime, heat
moving the check-in two hours earlier, an emergency surviving a restart, and the offline
chime path. It takes about two minutes; skip it with `pytest -m "not integration"`.

## Honest limitations

- Built and demonstrated against the **ring-sandbox emulator** (no physical Ring devices).
  The Partner API calls are identical. A Playground token check script is included.
- Snapshots from the emulator are placeholder images.
- Family messages appear in the app (and optionally as desktop notifications). Push or SMS
  delivery is the obvious next step.
- The access code is one shared family code. Contact views (`?as=c1`) are a view choice,
  not a separate login per contact.
- One household and one indoor camera. More rooms means more indoor cameras mapped to the
  `indoor_camera` role.

## License

MIT, see [LICENSE](LICENSE).
