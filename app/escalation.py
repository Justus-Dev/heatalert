"""Step-by-step escalation, built to stop false alarms before they reach the family.

1. Check-in: the Ring chime plays a gentle tone in the flat (and once more halfway).
   Any movement inside within the check-in window -> all good, nobody is alerted.
   No phone, no button, no app needed by the person being looked after.
   If the chime is offline the tone is impossible, so the first contact is alerted directly.
2. First contact is alerted and asked to acknowledge ("I'm on it").
3. No acknowledgement -> second contact (e.g. a neighbour with a key).
4. Still nobody -> every contact is told that nobody has responded.

An acknowledgement is not the end: if the incident is not closed within the follow-up
window, HeatWatch asks the acknowledging contact again and then moves on to the next one.
Movement inside at any stage ends the incident; anyone already alerted gets an all-clear.
Safety-critical texts are fixed templates - they never depend on an LLM.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .persist import build, encode
from .settings import Settings

CHECKIN = "checkin"
CONTACT = "contact"
ACKNOWLEDGED = "acknowledged"
UNANSWERED = "unanswered"
RESOLVED = "resolved"


@dataclass
class Action:
    kind: str  # chime | notify | snapshot
    contact_id: str | None = None
    title: str = ""
    body: str = ""
    category: str = "alert"  # alert | all_clear | info


@dataclass
class Incident:
    id: str
    opened_at: datetime
    reason: str
    heat_level: str
    threshold_min: int
    stage: str = CHECKIN
    stage_since: datetime | None = None
    contact_index: int = -1
    chime_plays: list[datetime] = field(default_factory=list)
    checkin_skipped: bool = False
    notified: list[str] = field(default_factory=list)
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    ack_reminded: bool = False
    resolution: str | None = None
    resolved_at: datetime | None = None
    snapshot_id: str | None = None
    log: list[dict] = field(default_factory=list)

    def note(self, at: datetime, text: str) -> None:
        self.log.append({"at": at.isoformat(), "text": text})

    @property
    def open(self) -> bool:
        return self.stage != RESOLVED

    def to_dict(self, settings: Settings) -> dict:
        def name(cid: str | None) -> str | None:
            c = settings.contact(cid) if cid else None
            return c.name if c else None

        current = (
            settings.contacts[self.contact_index].id
            if 0 <= self.contact_index < len(settings.contacts)
            else None
        )
        return {
            "id": self.id,
            "opened_at": self.opened_at.isoformat(),
            "reason": self.reason,
            "heat_level": self.heat_level,
            "threshold_min": self.threshold_min,
            "stage": self.stage,
            "stage_since": self.stage_since.isoformat() if self.stage_since else None,
            "current_contact": name(current),
            "current_contact_id": current,
            "chime_plays": [t.isoformat() for t in self.chime_plays],
            "checkin_skipped": self.checkin_skipped,
            "notified": [name(c) for c in self.notified],
            "notified_ids": list(self.notified),
            "acknowledged_by": name(self.acknowledged_by),
            "acknowledged_by_id": self.acknowledged_by,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "ack_reminded": self.ack_reminded,
            "resolution": self.resolution,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "family_alerted": bool(self.notified),
            "snapshot_id": self.snapshot_id,
            "log": self.log,
        }


def _hm(t: datetime) -> str:
    return t.strftime("%H:%M")


class Escalator:
    def __init__(self) -> None:
        self.active: Incident | None = None
        self.history: list[Incident] = []

    def reset(self) -> None:
        self.active = None
        self.history = []

    # ------------------------------------------------------------------ persistence

    def to_state(self) -> dict[str, Any]:
        return {
            "history": [encode(i) for i in self.history],
            "active_id": self.active.id if self.active else None,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> Escalator:
        esc = cls()
        esc.history = [build(Incident, raw) for raw in state.get("history", [])]
        active_id = state.get("active_id")
        esc.active = next((i for i in esc.history if i.id == active_id and i.open), None)
        return esc

    # ------------------------------------------------------------------ transitions

    def open(
        self,
        now: datetime,
        reason: str,
        heat_level: str,
        threshold_min: int,
        settings: Settings,
        *,
        chime_available: bool = True,
    ) -> list[Action]:
        inc = Incident(
            id=uuid.uuid4().hex[:8],
            opened_at=now,
            reason=reason,
            heat_level=heat_level,
            threshold_min=threshold_min,
            stage_since=now,
        )
        self.active = inc
        self.history.append(inc)
        if not chime_available:
            inc.checkin_skipped = True
            inc.note(now, f"{reason}. The chime is offline, so no check-in tone is possible.")
            return self._alert_next(inc, now, settings, first=True)
        inc.chime_plays.append(now)
        inc.note(now, f"{reason}. Check-in tone played on the chime.")
        return [Action("chime")]

    def on_activity(self, now: datetime, source: str, settings: Settings) -> list[Action]:
        inc = self.active
        if inc is None or not inc.open:
            return []
        person = settings.person_name
        if inc.stage == CHECKIN:
            minutes = max(0, round((now - inc.opened_at).total_seconds() / 60))
            inc.resolution = "Movement after the check-in tone - nobody was alerted"
            inc.note(now, f"Movement in {source} {minutes} min after the check-in tone. Resolved, no one alerted.")
            self._close(inc, now)
            return []
        inc.resolution = f"Movement detected in {source} at {_hm(now)}"
        inc.note(now, f"Movement in {source}. All-clear sent to everyone who was alerted.")
        actions = [
            Action(
                "notify",
                cid,
                f"All clear: {person} is moving",
                f"HeatWatch saw movement in {source} at {_hm(now)}. No further action needed.",
                "all_clear",
            )
            for cid in inc.notified
        ]
        self._close(inc, now)
        return actions

    def tick(self, now: datetime, settings: Settings) -> list[Action]:
        inc = self.active
        if inc is None or not inc.open or inc.stage_since is None:
            return []
        elapsed = now - inc.stage_since
        checkin_window = timedelta(minutes=settings.checkin_window_min)
        ack_window = timedelta(minutes=settings.contact_ack_window_min)
        followup = timedelta(minutes=settings.ack_followup_min)

        if inc.stage == CHECKIN:
            if len(inc.chime_plays) == 1 and elapsed >= checkin_window / 2:
                inc.chime_plays.append(now)
                inc.note(now, "No movement yet. Check-in tone played again.")
                return [Action("chime")]
            if elapsed >= checkin_window:
                inc.note(now, f"No movement {settings.checkin_window_min} min after the check-in tone.")
                return self._alert_next(inc, now, settings, first=True)
        elif inc.stage == CONTACT and elapsed >= ack_window:
            current = settings.contacts[inc.contact_index]
            inc.note(now, f"{current.name} did not acknowledge within {settings.contact_ack_window_min} min.")
            return self._alert_next(inc, now, settings, first=False)
        elif inc.stage == ACKNOWLEDGED:
            return self._follow_up(inc, now, settings, elapsed, ack_window, followup)
        return []

    def acknowledge(self, contact_id: str, now: datetime, settings: Settings) -> list[Action]:
        inc = self.active
        contact = settings.contact(contact_id)
        if inc is None or not inc.open or contact is None or inc.stage == CHECKIN:
            return []
        again = inc.stage == ACKNOWLEDGED and inc.acknowledged_by == contact_id
        inc.stage = ACKNOWLEDGED
        inc.stage_since = now
        inc.acknowledged_by = contact_id
        inc.acknowledged_at = now
        inc.ack_reminded = False
        idx = next(i for i, c in enumerate(settings.contacts) if c.id == contact_id)
        inc.contact_index = max(inc.contact_index, idx)
        if contact_id not in inc.notified:
            inc.notified.append(contact_id)
        if again:
            inc.note(now, f"{contact.name} is still on it.")
            return []
        inc.note(now, f"{contact.name} acknowledged: on the way / calling. Escalation paused.")
        return [
            Action(
                "notify",
                cid,
                f"{contact.name} is taking care of it",
                f"{contact.name} acknowledged the HeatWatch alert at {_hm(now)}.",
                "info",
            )
            for cid in inc.notified
            if cid != contact_id
        ]

    def resolve_by_contact(self, contact_id: str, now: datetime, settings: Settings, note: str = "") -> list[Action]:
        inc = self.active
        contact = settings.contact(contact_id)
        if inc is None or not inc.open or contact is None:
            return []
        inc.resolution = f"Confirmed OK by {contact.name}" + (f": {note}" if note else "")
        inc.note(now, inc.resolution)
        actions = [
            Action(
                "notify",
                cid,
                f"All clear from {contact.name}",
                f"{contact.name} confirmed {settings.person_name} is OK ({_hm(now)}).",
                "all_clear",
            )
            for cid in inc.notified
            if cid != contact_id
        ]
        self._close(inc, now)
        return actions

    # ------------------------------------------------------------------ helpers

    def _follow_up(
        self,
        inc: Incident,
        now: datetime,
        settings: Settings,
        elapsed: timedelta,
        ack_window: timedelta,
        followup: timedelta,
    ) -> list[Action]:
        """'I'm on it' must end in 'is OK' - otherwise ask again, then move on."""
        contact = settings.contact(inc.acknowledged_by) if inc.acknowledged_by else None
        if contact is None:
            return []
        person = settings.person_name
        if not inc.ack_reminded and elapsed >= followup:
            inc.ack_reminded = True
            inc.stage_since = now
            nxt = inc.contact_index + 1
            then = settings.contacts[nxt].name if nxt < len(settings.contacts) else "everyone"
            inc.note(now, f"No confirmation from {contact.name} {settings.ack_followup_min} min after 'I'm on it'. Asked again.")
            return [
                Action(
                    "notify",
                    contact.id,
                    f"Is {person} OK?",
                    f"You said you were on it at {_hm(inc.acknowledged_at or now)}. Please confirm {person} is OK, "
                    f"or tap 'Still on it'. If we hear nothing within {settings.contact_ack_window_min} min, "
                    f"HeatWatch alerts {then}.",
                )
            ]
        if inc.ack_reminded and elapsed >= ack_window:
            acked_at = _hm(inc.acknowledged_at or now)
            inc.note(now, f"{contact.name} did not confirm within {settings.contact_ack_window_min} min. Moving on.")
            inc.acknowledged_by = None
            inc.ack_reminded = False
            return self._alert_next(
                inc,
                now,
                settings,
                first=False,
                previous=f" {contact.name} said 'on it' at {acked_at} but has not confirmed {person} is OK.",
            )
        return []

    def _alert_next(
        self, inc: Incident, now: datetime, settings: Settings, *, first: bool, previous: str | None = None
    ) -> list[Action]:
        nxt = inc.contact_index + 1
        person = settings.person_name
        if nxt >= len(settings.contacts):
            inc.stage = UNANSWERED
            inc.stage_since = now
            inc.note(now, "Nobody acknowledged. All contacts told to act now.")
            return [
                Action(
                    "notify",
                    c.id,
                    f"URGENT: nobody has reached {person}",
                    f"No movement from {person} since {_hm(inc.opened_at)} and no contact has confirmed "
                    f"{person} is OK. Call {person} now. If you cannot get through, call emergency services (112).",
                )
                for c in settings.contacts
            ]
        contact = settings.contacts[nxt]
        inc.stage = CONTACT
        inc.stage_since = now
        inc.contact_index = nxt
        if contact.id not in inc.notified:
            inc.notified.append(contact.id)
        heat = " during a heat warning" if inc.heat_level != "none" else ""
        key = " You have a key to the flat." if contact.has_key else ""
        if previous is None:
            previous = "" if first else f" {settings.contacts[nxt - 1].name} did not respond."
        if inc.checkin_skipped:
            asked = f" HeatWatch could not ask {person} first because the chime is offline."
        else:
            asked = f" {person} did not react to the check-in tone at {_hm(inc.opened_at)}."
        inc.note(now, f"Alert sent to {contact.name} ({contact.relation}).")
        actions = [
            Action(
                "notify",
                contact.id,
                f"Please check on {person}",
                f"{inc.reason}{heat}.{asked}{previous}{key} Please call or go round, then tap 'I'm on it'.",
            )
        ]
        if first:
            actions.append(Action("snapshot", contact.id))
        return actions

    def _close(self, inc: Incident, now: datetime) -> None:
        inc.stage = RESOLVED
        inc.stage_since = now
        inc.resolved_at = now
        self.active = None
