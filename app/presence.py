"""Is the person at home, and when did they last show a sign of life?

A Ring doorbell cannot tell entering from leaving, and an indoor camera that sees
nobody might mean "asleep", "in another room", "out" or "on the floor". HeatWatch
resolves this with the front door as the gate:

* Door opened, then motion *inside*            -> at home (came in, or never left).
* Door opened, then a person *outside* only    -> left the house. No inactivity alarm.
* Door opened and closed, nobody seen after    -> assumed at home (e.g. aired the hall).

Inactivity only counts while the person is at home.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

DOOR_RESOLVE_WINDOW = timedelta(minutes=10)

HOME = "home"
AWAY = "away"
DOOR = "door"  # door just used, direction not resolved yet


@dataclass
class Change:
    title: str
    detail: str = ""


@dataclass
class PresenceTracker:
    status: str = HOME
    since: datetime | None = None
    reason: str = "Monitoring started"
    last_activity: datetime | None = None
    last_activity_source: str | None = None
    door_open: bool = False
    last_door_event: datetime | None = None
    pending_since: datetime | None = None
    pending_from: str = HOME
    pending_outdoor: bool = False
    last_departure: datetime | None = None
    last_return: datetime | None = None
    day: date | None = None
    door_uses_today: int = 0
    visitors_today: int = 0
    left_today: bool = False

    def start(self, at: datetime, status: str = HOME) -> None:
        self.status = status
        self.since = at
        self.reason = "Monitoring started"
        self.day = at.date()

    # ------------------------------------------------------------------ events

    def observe(self, kind: str, at: datetime, source: str = "") -> list[Change]:
        """Apply one normalized observation; returns presence changes worth logging."""
        self._roll_day(at)
        changes: list[Change] = []
        if kind == "door_open":
            self.door_open = True
            self._door_used(at)
        elif kind == "door_close":
            self.door_open = False
            self._door_used(at)
        elif kind == "outdoor_person":
            if self.status == DOOR:
                self.pending_outdoor = True
        elif kind == "doorbell_ring":
            self.visitors_today += 1
            if self.status == DOOR:
                self.pending_outdoor = True
        elif kind == "indoor_activity":
            self.last_activity = at
            self.last_activity_source = source
            if self.status in (DOOR, AWAY):
                came_back = self.status == AWAY or self.pending_from == AWAY
                self._set(HOME, at, "Movement inside after the door was used")
                if came_back:
                    self.last_return = at
                    changes.append(Change("Came home", f"Movement inside ({source})"))
                self.pending_since = None
        elif kind == "garden_activity":
            if self.status != AWAY:
                self.last_activity = at
                self.last_activity_source = source
        return changes

    def tick(self, now: datetime) -> list[Change]:
        """Resolve a door transition once the observation window has passed."""
        self._roll_day(now)
        if self.status != DOOR or self.pending_since is None:
            return []
        if self.door_open or now - self.pending_since < DOOR_RESOLVE_WINDOW:
            return []
        resolved_at = self.pending_since + DOOR_RESOLVE_WINDOW
        self.pending_since = None
        if self.pending_outdoor:
            self._set(AWAY, resolved_at, "Person seen outside, no movement inside after the door")
            self.last_departure = resolved_at
            self.left_today = True
            return [Change("Left the house", "Seen outside the front door, nothing inside after")]
        self._set(self.pending_from, resolved_at, "Door used, nobody seen leaving")
        if self.pending_from == HOME:
            return [Change("Still at home", "Door was used but nobody was seen leaving")]
        return [Change("Still away", "Door was used but no movement inside followed")]

    # ------------------------------------------------------------------ helpers

    def _door_used(self, at: datetime) -> None:
        self.last_door_event = at
        if self.status != DOOR:
            self.pending_from = self.status
            self.pending_outdoor = False
            self._set(DOOR, at, "Front door in use")
        self.pending_since = at
        self.door_uses_today += 1

    def _set(self, status: str, at: datetime, reason: str) -> None:
        self.status = status
        self.since = at
        self.reason = reason

    def _roll_day(self, at: datetime) -> None:
        if self.day != at.date():
            self.day = at.date()
            self.door_uses_today = 0
            self.visitors_today = 0
            self.left_today = self.status == AWAY

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "since": self.since.isoformat() if self.since else None,
            "reason": self.reason,
            "last_activity": self.last_activity.isoformat() if self.last_activity else None,
            "last_activity_source": self.last_activity_source,
            "door_open": self.door_open,
            "last_departure": self.last_departure.isoformat() if self.last_departure else None,
            "last_return": self.last_return.isoformat() if self.last_return else None,
            "door_uses_today": self.door_uses_today,
            "visitors_today": self.visitors_today,
            "left_today": self.left_today,
        }
