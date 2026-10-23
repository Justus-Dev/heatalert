"""HeatWatch engine: turns Ring events into a calm, heat-aware safety net."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict, deque
from datetime import date, datetime, timedelta

from ring_sandbox.models import WebhookEvent

from . import persist
from .ai import Writer
from .clock import SimClock
from .config import Config
from .director import Director
from .escalation import Action, Escalator
from .presence import AWAY, DOOR, HOME, PresenceTracker
from .weather import Weather
from .ring import RingGateway
from .rules import HEAT_LABELS, NONE, assess_heat, in_quiet_hours, inactivity_reference, threshold_for
from .settings import ROLES, Settings, SettingsStore
from .weather import WeatherService

log = logging.getLogger("heatwatch")

# Motion sub-types that count as a sign of life from an indoor camera. Pets do not.
LIFE_SIGN_SUBTYPES = {"human", "motion", "other_motion", None}

EVENT_LABELS = {
    "motion_detected": "Motion",
    "button_press": "Doorbell pressed",
    "contact_sensor_faulted": "Door opened",
    "contact_sensor_cleared": "Door closed",
    "temperature_exceeded": "Temperature above threshold",
    "temperature_cleared": "Temperature back to normal",
    "device_offline": "Device offline",
    "device_online": "Device online",
    "device_added": "Device added",
    "device_removed": "Device removed",
    "tamper_detected": "Tamper detected",
    "tamper_cleared": "Tamper cleared",
}


# What it means for the safety net when a device of this role is offline.
OUTAGE_IMPACT = {
    "indoor_camera": "Inactivity checks are paused: HeatWatch cannot see movement at home.",
    "chime": "No check-in tone: if needed, HeatWatch alerts the family directly.",
    "door_sensor": "HeatWatch cannot tell when {person} goes out, so it assumes {person} is home (the safe side).",
    "entrance_camera": "Leaving is recognised less reliably; HeatWatch then assumes {person} is home.",
    "climate_sensor": "Indoor heat is not measured; only the weather forecast is used.",
    "garden_camera": "Movement in the garden is not counted.",
}

STATE_VERSION = 1


def _hm(t: datetime) -> str:
    return t.strftime("%H:%M")


def _duration(td: timedelta) -> str:
    minutes = max(0, int(td.total_seconds() // 60))
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


class HeatWatch:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = SettingsStore(cfg.data_dir / "settings.json")
        self.clock = SimClock(self.settings.timezone)
        self.ring = RingGateway(cfg.ring_base_url, cfg.ring_token, sandbox=cfg.ring_sandbox)
        self.weather = WeatherService()
        self.writer = Writer(enabled=cfg.bedrock_enabled, region=cfg.aws_region, model_id=cfg.bedrock_model_id)
        self.director = Director(self, cfg.scenarios_dir)
        self.snapshots: dict[str, tuple[bytes, str, str]] = {}
        self.seen_requests: OrderedDict[str, None] = OrderedDict()
        self.webhooks_verified = 0
        self.webhooks_rejected = 0
        self.ring_ready = False
        self.demo_label: str | None = None
        self._last_provision_try = 0.0
        self._background: set[asyncio.Task] = set()
        self.state_path = cfg.data_dir / "state.json"
        self._dirty = False
        self._last_save = 0.0
        self.restored_from: str | None = None
        self._reset_day(self.clock.now(), HOME)
        self._restore()

    @property
    def settings(self) -> Settings:
        return self.store.settings

    # ------------------------------------------------------------------ lifecycle

    def _reset_day(self, now: datetime, presence: str) -> None:
        self.presence = PresenceTracker()
        self.presence.start(now, presence)
        self.escalator = Escalator()
        self.timeline: deque[dict] = deque(maxlen=500)
        self.notifications: list[dict] = []
        self.monitoring_since = now
        self.rearmed_at: datetime | None = None
        self.indoor_hot = False
        self.indoor_detail = ""
        self.offline: set[str] = set()
        self.nudged_on: date | None = None
        self.heat_level: str | None = None
        self._dirty = True

    # ------------------------------------------------------------------ persistence

    def save_state(self) -> None:
        """Write everything needed to continue an open emergency after a restart."""
        w = self.weather.current
        persist.write_atomic(
            self.state_path,
            {
                "version": STATE_VERSION,
                "saved_at": datetime.now(tz=self.clock.tz),
                "clock": {"mode": self.clock.mode, "scale": self.clock.scale, "now": self.clock.now()},
                "demo_label": self.demo_label,
                "presence": persist.encode(self.presence),
                "escalator": self.escalator.to_state(),
                "timeline": list(self.timeline),
                "notifications": self.notifications,
                "monitoring_since": self.monitoring_since,
                "rearmed_at": self.rearmed_at,
                "indoor_hot": self.indoor_hot,
                "indoor_detail": self.indoor_detail,
                "offline": sorted(self.offline),
                "nudged_on": self.nudged_on,
                "heat_level": self.heat_level,
                "weather": {**w.to_dict(), "fetched_at": w.fetched_at} if w else None,
                "seen_requests": list(self.seen_requests)[-500:],
                "webhooks_verified": self.webhooks_verified,
                "webhooks_rejected": self.webhooks_rejected,
            },
        )
        self._dirty = False

    def _restore(self) -> None:
        state = persist.read(self.state_path)
        if not state or state.get("version") != STATE_VERSION:
            return
        try:
            clock = state["clock"]
            if clock["mode"] == "demo":
                # A demo cannot continue its script after a restart: keep the day, paused.
                self.clock.start_demo(clock["now"], clock["scale"])
                self.clock.pause()
            self.presence = persist.build(PresenceTracker, state["presence"])
            self.escalator = Escalator.from_state(state["escalator"])
            self.timeline = deque(state["timeline"], maxlen=500)
            self.notifications = state["notifications"]
            self.monitoring_since = state["monitoring_since"]
            self.rearmed_at = state["rearmed_at"]
            self.indoor_hot = state["indoor_hot"]
            self.indoor_detail = state["indoor_detail"]
            self.offline = set(state["offline"])
            self.nudged_on = state["nudged_on"]
            self.heat_level = state["heat_level"]
            if state.get("weather"):
                self.weather.current = persist.build(Weather, state["weather"])
            self.seen_requests = OrderedDict((rid, None) for rid in state["seen_requests"])
            self.webhooks_verified = state["webhooks_verified"]
            self.webhooks_rejected = state["webhooks_rejected"]
            self.demo_label = state["demo_label"]
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("could not restore saved state, starting fresh: %s", exc)
            self._reset_day(self.clock.now(), HOME)
            return
        self.restored_from = state["saved_at"].isoformat()
        open_note = (
            f" Open incident restored at stage '{self.escalator.active.stage}'." if self.escalator.active else ""
        )
        demo_note = " Demo day paused." if self.clock.mode == "demo" else ""
        self.log_system("Restored after restart", f"State saved at {_hm(state['saved_at'])} reloaded.{open_note}{demo_note}")

    async def startup(self) -> None:
        self.log_system("HeatWatch started", f"Ring API: {self.cfg.ring_base_url}")
        await self.connect_ring()
        if self.clock.mode == "live":
            s = self.settings
            await self.weather.refresh_live(s.latitude, s.longitude, s.timezone, s.location_name)

    async def connect_ring(self) -> bool:
        loop = asyncio.get_running_loop()
        self._last_provision_try = loop.time()
        try:
            await self.ring.provision_sandbox(self.cfg.public_webhook_url, self.cfg.webhook_signing_key)
        except Exception as exc:
            self.ring.last_error = f"Cannot reach the Ring emulator at {self.cfg.ring_base_url}: {exc}"
            self.ring_ready = False
            return False
        await self.ring.discover()
        await self.ring.me()
        self.ring_ready = bool(self.ring.devices)
        if self.ring_ready:
            self.log_system(
                "Connected to Ring",
                f"{len(self.ring.devices)} devices discovered via GET /v1/devices",
            )
            if self.clock.mode == "live":
                await self._seed_from_history()
        return self.ring_ready

    async def _seed_from_history(self) -> None:
        """Live mode: pick up the last sign of life from Ring event history after a restart."""
        cam = self.device_for_role("indoor_camera")
        if cam is None:
            return
        newest = await self.ring.recent_activity(cam["id"])
        if newest is not None:
            at = newest.astimezone(self.clock.tz)
            if self.presence.last_activity is None or at > self.presence.last_activity:
                self.presence.last_activity = at
                self.presence.last_activity_source = cam["name"]

    async def begin_demo(
        self,
        start: datetime,
        speed: float,
        presence: str,
        *,
        weather_date: str,
        weather_fallback: tuple[float, float],
        label: str,
    ) -> None:
        s = self.settings
        await self._reset_emulator_devices()
        # Fetch the replayed weather first so the demo clock starts exactly at `start`.
        w = await self.weather.load_replay(
            weather_date, s.latitude, s.longitude, s.timezone, s.location_name, weather_fallback
        )
        self.clock.start_demo(start, speed)
        self._reset_day(start, presence)
        self.snapshots.clear()
        self.demo_label = label
        self.log_system("Demo day started", f"{label}. {w.source}.")
        self._check_heat_change(self.clock.now())

    async def _reset_emulator_devices(self) -> None:
        """A previous demo may have left emulated devices offline: bring them back first."""
        if not self.cfg.ring_sandbox:
            return
        await self.ring.discover()
        for d in self.ring.devices:
            if d.get("online") is False:
                try:
                    await self.ring.inject(d["id"], "device_online")
                except Exception as exc:
                    log.warning("could not bring %s back online: %s", d["name"], exc)
                d["online"] = True
        await asyncio.sleep(0.3)  # let the emulator's webhooks land in the old day

    async def go_live(self) -> None:
        self.director.stop()
        self.clock.go_live()
        self.demo_label = None
        self._reset_day(self.clock.now(), HOME)
        s = self.settings
        self.weather.current = None
        await self.weather.refresh_live(s.latitude, s.longitude, s.timezone, s.location_name)
        self.log_system("Live mode", "Real time, live Open-Meteo forecast")

    # ------------------------------------------------------------------ main loop

    async def tick(self) -> None:
        loop = asyncio.get_running_loop()
        if not self.ring_ready and loop.time() - self._last_provision_try > 10:
            await self.connect_ring()
        now = self.clock.now()
        await self.director.tick(now)
        now = self.clock.now()
        for change in self.presence.tick(now):
            self._add("presence", change.title, change.detail)
        if self.clock.mode == "live":
            s = self.settings
            await self.weather.refresh_live(s.latitude, s.longitude, s.timezone, s.location_name)
        self._check_heat_change(now)
        await self._execute(self.escalator.tick(now, self.settings))
        await self._check_inactivity(now)
        self._check_nudge(now)
        if self._dirty and loop.time() - self._last_save >= 1.0:
            self._last_save = loop.time()
            try:
                self.save_state()
            except OSError as exc:
                log.error("could not save state: %s", exc)

    # ------------------------------------------------------------------ Ring webhooks

    def accept_request_id(self, request_id: str) -> bool:
        """Deduplicate deliveries: Ring may deliver the same event more than once."""
        if request_id in self.seen_requests:
            return False
        self.seen_requests[request_id] = None
        while len(self.seen_requests) > 2000:
            self.seen_requests.popitem(last=False)
        return True

    async def handle_event(self, ev: WebhookEvent) -> None:
        self.webhooks_verified += 1
        etype = ev.event_type
        if etype in ("device_added", "device_removed"):
            await self.ring.discover()
        device = self.ring.device_by_id(ev.device_id)
        if device is None and ev.data.attributes.source_type == "devices":
            await self.ring.discover()
            device = self.ring.device_by_id(ev.device_id)
        name = device["name"] if device else ev.device_id
        role = self.settings.device_roles.get(name, "ignore") if device else "ignore"
        at = self.clock.from_real_ms(ev.data.attributes.timestamp)
        sub = ev.sub_type

        label = EVENT_LABELS.get(etype, etype)
        if sub:
            label += f" · {sub.replace('_', ' ')}"
        self._add(
            "ring",
            f"{label} — {name}",
            f"Signed Ring webhook ({etype}), HMAC verified",
            at=at,
            raw={"type": etype, "sub_type": sub, "device": name, "role": role, "request_id": ev.request_id},
        )

        if etype == "device_offline" and device:
            device["online"] = False
            if device["id"] not in self.offline:
                self.offline.add(device["id"])
                impact = OUTAGE_IMPACT.get(role)
                if impact:
                    impact = impact.format(person=self.settings.person_name)
                    self._add("device", f"{name} is offline", impact, at=at)
                    self._notify(
                        self.settings.contacts[0].id,
                        f"{name} is offline",
                        f"Since {_hm(at)}. {impact} Please check its power and Wi-Fi.",
                        "info",
                        at=at,
                    )
            return
        if etype == "device_online" and device:
            device["online"] = True
            if device["id"] in self.offline:
                self.offline.discard(device["id"])
                if role in OUTAGE_IMPACT:
                    self._add("device", f"{name} is back online", "Full protection restored", at=at)
                    self._notify(
                        self.settings.contacts[0].id,
                        f"{name} is back online",
                        f"Since {_hm(at)}. HeatWatch is fully working again.",
                        "info",
                        at=at,
                    )
            return

        if role == "climate_sensor":
            if etype == "temperature_exceeded":
                limit = device.get("temperature_max_c") if device else None
                self.indoor_hot = True
                self.indoor_detail = (
                    f"Indoor temperature above {limit:g} °C ({name}, Ring sensor)"
                    if limit is not None
                    else f"Indoor temperature too high ({name}, Ring sensor)"
                )
            elif etype == "temperature_cleared":
                self.indoor_hot = False
                self.indoor_detail = ""
            self._check_heat_change(at)
            return

        observation = self._observation(role, etype, sub)
        if observation is None:
            if role == "indoor_camera" and etype == "motion_detected":
                self._add("ignored", "Pet movement ignored", f"{name}: '{sub}' is not counted as a sign of life", at=at)
            return
        for change in self.presence.observe(observation, at, name):
            self._add("presence", change.title, change.detail, at=at)
        if observation == "indoor_activity" or (
            observation == "garden_activity" and self.presence.status != AWAY
        ):
            open_incident = self.escalator.active
            await self._execute(self.escalator.on_activity(at, name, self.settings))
            if open_incident is not None and self.escalator.active is None:
                title = "Check-in answered" if not open_incident.notified else "All clear"
                self._add("resolved", title, open_incident.resolution or "", at=at)

    @staticmethod
    def _observation(role: str, etype: str, sub: str | None) -> str | None:
        if role == "door_sensor":
            return {"contact_sensor_faulted": "door_open", "contact_sensor_cleared": "door_close"}.get(etype)
        if role == "entrance_camera":
            if etype == "button_press":
                return "doorbell_ring"
            if etype == "motion_detected" and sub == "human":
                return "outdoor_person"
            return None
        if role == "indoor_camera" and etype == "motion_detected":
            return "indoor_activity" if sub in LIFE_SIGN_SUBTYPES else None
        if role == "garden_camera" and etype == "motion_detected" and sub == "human":
            return "garden_activity"
        return None

    # ------------------------------------------------------------------ rules

    def heat(self):
        w = self.weather.current
        return assess_heat(
            self.settings, w.apparent_max_c if w else None, self.indoor_hot, self.indoor_detail
        )

    def _check_heat_change(self, now: datetime) -> None:
        heat = self.heat()
        if heat.level == self.heat_level:
            return
        previous, self.heat_level = self.heat_level, heat.level
        threshold = _duration(threshold_for(self.settings, heat.level))
        if heat.level == NONE:
            if previous is not None:
                self._add("heat", "Heat warning ended", f"Inactivity threshold back to {threshold}", at=now)
            return
        self._add(
            "heat",
            f"{HEAT_LABELS[heat.level]} active",
            f"{'; '.join(heat.reasons)}. HeatWatch now checks in after {threshold} without movement.",
            at=now,
        )

    def inactivity(self, now: datetime) -> dict:
        s = self.settings
        heat = self.heat()
        threshold = threshold_for(s, heat.level)
        home_since = self.presence.since if self.presence.status == HOME else None
        floor = max(t for t in (self.monitoring_since, self.rearmed_at) if t is not None)
        reference = inactivity_reference(
            s, now, last_activity=self.presence.last_activity, home_since=home_since, monitoring_since=floor
        )
        paused = None
        if not (s.setup_complete and s.consent.given):
            paused = "Setup and consent not completed"
        elif self.presence.status == AWAY:
            paused = f"{s.person_name} is out — no inactivity checks"
        elif self.presence.status == DOOR:
            paused = "Front door in use — working out who came or went"
        elif in_quiet_hours(s, now):
            paused = f"Night rest until {s.quiet_end}"
        elif self.offline_role("indoor_camera"):
            paused = "Indoor camera offline"
        elif self.escalator.active is not None:
            paused = "Check-in in progress"
        elapsed = now - reference
        return {
            "reference": reference.isoformat(),
            "elapsed_min": round(elapsed.total_seconds() / 60, 1),
            "threshold_min": round(threshold.total_seconds() / 60),
            "normal_threshold_min": s.threshold_normal_min,
            "due_at": (reference + threshold).isoformat(),
            "progress": min(1.0, elapsed / threshold) if threshold.total_seconds() else 0,
            "paused": paused,
            "heat_level": heat.level,
        }

    async def _check_inactivity(self, now: datetime) -> None:
        state = self.inactivity(now)
        if state["paused"] or state["elapsed_min"] < state["threshold_min"]:
            return
        heat = state["heat_level"]
        quiet_for = _duration(timedelta(minutes=state["elapsed_min"]))
        reason = f"No movement for {quiet_for} while {self.settings.person_name} is at home"
        chime_ok = self.device_for_role("chime") is not None and not self.offline_role("chime")
        actions = self.escalator.open(
            now, reason, heat, state["threshold_min"], self.settings, chime_available=chime_ok
        )
        if chime_ok:
            self._add("checkin", "Check-in started", f"{reason}. Playing a gentle tone on the chime.", at=now)
        else:
            first = self.settings.contacts[0].name
            self._add("checkin", "Check-in without tone", f"{reason}. The chime is offline, so {first} is alerted directly.", at=now)
        await self._execute(actions)

    def _check_nudge(self, now: datetime) -> None:
        """Heat day, nobody came or went: a caring hint to call - not an alarm."""
        s = self.settings
        if not (s.nudges_enabled and s.setup_complete) or self.nudged_on == now.date():
            return
        if self.heat().level == NONE or now.time() < Settings.parse_hhmm(s.nudge_time):
            return
        if self.presence.status != HOME or self.escalator.active is not None:
            return
        if self.presence.door_uses_today or self.presence.visitors_today:
            return
        self.nudged_on = now.date()
        task = asyncio.create_task(self._send_nudge(now))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _send_nudge(self, now: datetime) -> None:
        s = self.settings
        contact = s.contacts[0]
        w = self.weather.current
        feels = f"{w.apparent_max_c:.0f} °C" if w and w.apparent_max_c is not None else "very hot"
        last = self.presence.last_activity
        fallback = (
            f"{s.person_name} hasn't left the flat today and nobody has visited. It feels like {feels} "
            f"outside. Maybe give {s.person_name} a quick call and remind them to drink some water?"
        )
        prompt = (
            f"Write a message of at most 2 short sentences to {contact.name} ({contact.relation} of "
            f"{s.person_name}, who calls {s.person_name} '{s.person_nickname}').\n"
            f"Facts: it is {_hm(now)} on a heatwave day, feels-like maximum {feels}. "
            f"{s.person_name} is at home and fine (last movement {_hm(last) if last else 'unknown'}), "
            "but has not left home all day and nobody has visited.\n"
            f"Suggest a short call, and include one practical heat tip (drinking water, curtains closed "
            f"during the day, cool damp cloth). Refer to {s.person_name} as '{s.person_nickname}'."
        )
        text, author = await self.writer.write(prompt, fallback)
        self._notify(contact.id, f"A quick call for {s.person_nickname}?", text, "nudge", author=author, at=now)
        self._add("nudge", "Care hint sent", f"To {contact.name}: no one came or went today", at=now)

    async def summary(self) -> dict:
        s = self.settings
        now = self.clock.now()
        heat = self.heat()
        events = [e for e in reversed(self.timeline) if e["kind"] in ("presence", "checkin", "resolved", "alert", "heat", "nudge")]
        facts = "\n".join(f"- {_hm(datetime.fromisoformat(e['at']))} {e['title']}: {e['detail']}" for e in events[-30:])
        last = self.presence.last_activity
        status = {HOME: "at home", AWAY: "out", DOOR: "at the front door"}[self.presence.status]
        fallback = (
            f"{s.person_name} is {status}. Last movement {_hm(last) if last else 'not seen yet'}. "
            f"{heat.label}. {len(self.escalator.history)} check-in(s) today."
        )
        prompt = (
            f"Summarize {s.person_name}'s day so far for the family in 3 short sentences, as of {_hm(now)}. "
            f"Heat today: {heat.label}; {'; '.join(heat.reasons) or 'no heat stress'}. "
            f"Current status: {status}. Event log (oldest first):\n{facts or '- nothing notable yet'}\n"
            "Mention check-ins and whether anyone had to be alerted. End with one caring suggestion."
        )
        text, author = await self.writer.write(prompt, fallback)
        return {"text": text, "author": author, "at": now.isoformat()}

    # ------------------------------------------------------------------ actions

    async def _execute(self, actions: list[Action]) -> None:
        for action in actions:
            if action.kind == "chime":
                await self._play_chime()
            elif action.kind == "notify":
                self._notify(action.contact_id, action.title, action.body, action.category)
            elif action.kind == "snapshot":
                await self._share_snapshot(action.contact_id)

    async def _play_chime(self) -> None:
        chime = self.device_for_role("chime")
        now = self.clock.now()
        if chime is None:
            self._add("checkin", "No chime configured", "Check-in tone skipped; escalation continues", at=now)
            return
        inc = self.escalator.active
        if inc is None:
            return  # movement arrived before the tone was sent: no need to play it
        slot = self.settings.chime_slot
        ok = await self.ring.play_chime(chime["id"], slot)
        if ok:
            if self.escalator.active is not inc:
                title = "Check-in tone played (movement arrived at the same time)"
            else:
                title = "Check-in tone played again" if len(inc.chime_plays) > 1 else "Check-in tone played"
            self._add("checkin", title, f"{chime['name']} · app audio slot {slot} · Ring API 204", at=now)
        else:
            self._add("checkin", "Chime failed", f"{self.ring.last_error}; escalation continues", at=now)

    async def _share_snapshot(self, contact_id: str | None) -> None:
        s = self.settings
        inc = self.escalator.active
        now = self.clock.now()
        if not s.consent.share_snapshot:
            self._add("privacy", "No snapshot shared", f"{s.person_name} did not consent to sharing images", at=now)
            return
        cam = self.device_for_role("indoor_camera")
        if cam is None or inc is None:
            return
        got = await self.ring.latest_snapshot(cam["id"])
        if got is None:
            self._add("privacy", "Snapshot unavailable", self.ring.last_error or "", at=now)
            return
        sid = uuid.uuid4().hex[:10]
        self.snapshots[sid] = (got[0], got[1], now.isoformat())
        inc.snapshot_id = sid
        for n in reversed(self.notifications):
            if n["incident_id"] == inc.id and n["contact_id"] == contact_id and n["category"] == "alert":
                n["snapshot_id"] = sid
                break
        contact = s.contact(contact_id) if contact_id else None
        self._add(
            "privacy",
            "Last snapshot shared",
            f"Only with {contact.name if contact else 'the alerted contact'}, as consented. Nothing is stored in normal operation.",
            at=now,
        )

    def _notify(
        self,
        contact_id: str | None,
        title: str,
        body: str,
        category: str,
        *,
        author: str = "template",
        at: datetime | None = None,
    ) -> None:
        contact = self.settings.contact(contact_id) if contact_id else None
        at = at or self.clock.now()
        inc = self.escalator.active or (self.escalator.history[-1] if self.escalator.history else None)
        self.notifications.append(
            {
                "id": uuid.uuid4().hex[:10],
                "at": at.isoformat(),
                "contact_id": contact_id,
                "contact_name": contact.name if contact else "Family",
                "category": category,
                "title": title,
                "body": body,
                "author": author,
                "incident_id": inc.id if inc and category in ("alert", "all_clear", "info") else None,
                "snapshot_id": None,
            }
        )
        kind = {"alert": "alert", "all_clear": "resolved", "nudge": "nudge"}.get(category, "notify")
        self._add(kind, f"Message to {contact.name if contact else 'family'}", title, at=at)

    # ------------------------------------------------------------------ family actions

    def _require_incident(self, contact_id: str) -> None:
        if self.escalator.active is None:
            raise ValueError("there is no open incident")
        if self.settings.contact(contact_id) is None:
            raise ValueError(f"unknown contact {contact_id!r}")

    async def acknowledge(self, contact_id: str) -> None:
        self._require_incident(contact_id)
        await self._execute(self.escalator.acknowledge(contact_id, self.clock.now(), self.settings))

    async def resolve(self, contact_id: str, note: str) -> None:
        self._require_incident(contact_id)
        now = self.clock.now()
        actions = self.escalator.resolve_by_contact(contact_id, now, self.settings, note)
        self.rearmed_at = now
        contact = self.settings.contact(contact_id)
        self._add("resolved", "Incident closed", f"Confirmed OK by {contact.name if contact else contact_id}", at=now)
        await self._execute(actions)

    # ------------------------------------------------------------------ helpers

    def device_for_role(self, role: str) -> dict | None:
        roles = self.settings.device_roles
        return next((d for d in self.ring.devices if roles.get(d["name"]) == role), None)

    def offline_role(self, role: str) -> bool:
        dev = self.device_for_role(role)
        return bool(dev and (dev["id"] in self.offline or dev.get("online") is False))

    def log_system(self, title: str, detail: str = "") -> None:
        self._add("system", title, detail)

    def _add(self, kind: str, title: str, detail: str = "", *, at: datetime | None = None, raw: dict | None = None) -> None:
        at = at or self.clock.now()
        self.timeline.appendleft(
            {"id": uuid.uuid4().hex[:10], "at": at.isoformat(), "kind": kind, "title": title, "detail": detail, "raw": raw}
        )
        self._dirty = True
        # Privacy: event metadata is kept for 24 h at most.
        cutoff = at - timedelta(hours=24)
        while self.timeline and datetime.fromisoformat(self.timeline[-1]["at"]) < cutoff:
            self.timeline.pop()

    # ------------------------------------------------------------------ API view

    def state(self) -> dict:
        s = self.settings
        now = self.clock.now()
        heat = self.heat()
        last_incident = self.escalator.active or (self.escalator.history[-1] if self.escalator.history else None)
        roles = s.device_roles
        devices = [
            {
                **d,
                "role": roles.get(d["name"], "ignore"),
                "role_label": ROLES.get(roles.get(d["name"], "ignore"), ""),
                "offline": d["id"] in self.offline or d.get("online") is False,
            }
            for d in self.ring.devices
        ]
        degraded = [
            {"name": d["name"], "role": d["role"], "impact": OUTAGE_IMPACT[d["role"]].format(person=s.person_name)}
            for d in devices
            if d["offline"] and d["role"] in OUTAGE_IMPACT
        ]
        return {
            "clock": self.clock.describe(),
            "restored_from": self.restored_from,
            "degraded": degraded,
            "demo_label": self.demo_label,
            "settings": s.to_dict(),
            "presence": self.presence.snapshot(),
            "inactivity": self.inactivity(now),
            "heat": {
                "level": heat.level,
                "label": heat.label,
                "reasons": heat.reasons,
                "weather": self.weather.current.to_dict() if self.weather.current else None,
                "weather_error": self.weather.last_error,
                "indoor_hot": self.indoor_hot,
                "thresholds_min": {
                    "none": s.threshold_normal_min,
                    "heat": s.threshold_heat_min,
                    "extreme": s.threshold_extreme_min,
                },
            },
            "incident": last_incident.to_dict(s) if last_incident else None,
            "incidents_today": len(self.escalator.history),
            "notifications": list(reversed(self.notifications))[:40],
            "timeline": list(self.timeline)[:120],
            "devices": devices,
            "ring": {
                "base_url": self.cfg.ring_base_url,
                "sandbox": self.cfg.ring_sandbox,
                "ready": self.ring_ready,
                "account": self.ring.account,
                "last_error": self.ring.last_error,
                "calls": list(self.ring.calls)[:20],
                "webhooks_verified": self.webhooks_verified,
                "webhooks_rejected": self.webhooks_rejected,
            },
            "demo": self.director.to_dict(),
            "ai": {"status": self.writer.status},
        }
