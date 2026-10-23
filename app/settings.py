"""Household settings chosen by the family during setup, persisted as JSON."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import time
from pathlib import Path
from typing import Any

# Device roles HeatWatch understands. A Ring device is mapped to one role.
ROLES = {
    "entrance_camera": "Entrance camera / doorbell (outside the front door)",
    "door_sensor": "Front door contact sensor",
    "indoor_camera": "Indoor camera (motion events only)",
    "garden_camera": "Garden camera (on the property)",
    "chime": "Chime (plays the check-in tone)",
    "climate_sensor": "Indoor temperature sensor",
    "ignore": "Not used by HeatWatch",
}

DEFAULT_ROLES = {
    "Front Door": "entrance_camera",
    "Front Door Sensor": "door_sensor",
    "Living Room": "indoor_camera",
    "Backyard": "garden_camera",
    "Kitchen Chime": "chime",
    "Living Room Climate": "climate_sensor",
}


@dataclass
class Contact:
    id: str
    name: str
    relation: str
    phone: str = ""
    has_key: bool = False


@dataclass
class Consent:
    given: bool = False
    given_by: str = ""
    given_at: str | None = None
    share_snapshot: bool = False


@dataclass
class Settings:
    setup_complete: bool = False
    person_name: str = "Margaret"
    person_nickname: str = "Mom"
    location_name: str = "Berlin, Germany"
    latitude: float = 52.52
    longitude: float = 13.405
    timezone: str = "Europe/Berlin"
    contacts: list[Contact] = field(
        default_factory=lambda: [
            Contact("c1", "Anna", "Daughter", "+49 151 0000001"),
            Contact("c2", "Mr. Weber", "Neighbor", "+49 151 0000002", has_key=True),
        ]
    )
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"
    threshold_normal_min: int = 240
    threshold_heat_min: int = 120
    threshold_extreme_min: int = 90
    heat_apparent_c: float = 32.0
    extreme_apparent_c: float = 38.0
    checkin_window_min: int = 5
    contact_ack_window_min: int = 15
    ack_followup_min: int = 30  # after "I'm on it": ask again, then move on
    nudges_enabled: bool = True
    nudge_time: str = "17:00"
    chime_slot: str = "ring-appstore-event-1"
    consent: Consent = field(default_factory=Consent)
    device_roles: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ROLES))

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def parse_hhmm(value: str) -> time:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))

    def contact(self, contact_id: str) -> Contact | None:
        return next((c for c in self.contacts if c.id == contact_id), None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Settings:
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if k in known}
        if "contacts" in data:
            data["contacts"] = [Contact(**c) for c in data["contacts"]]
        if "consent" in data:
            data["consent"] = Consent(**data["consent"])
        settings = cls(**data)
        settings.validate()
        return settings

    def validate(self) -> None:
        for name in ("quiet_start", "quiet_end", "nudge_time"):
            self.parse_hhmm(getattr(self, name))
        if not (0 < self.threshold_extreme_min <= self.threshold_heat_min <= self.threshold_normal_min):
            raise ValueError("thresholds must satisfy 0 < extreme <= heat <= normal")
        if self.checkin_window_min <= 0 or self.contact_ack_window_min <= 0 or self.ack_followup_min <= 0:
            raise ValueError("check-in, acknowledgement and follow-up windows must be positive")
        if not self.contacts:
            raise ValueError("at least one contact is required")
        for role in self.device_roles.values():
            if role not in ROLES:
                raise ValueError(f"unknown device role {role!r}")


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self.settings = self._load()

    def _load(self) -> Settings:
        if self.path.exists():
            try:
                return Settings.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
            except (ValueError, TypeError, json.JSONDecodeError):
                pass  # corrupt or outdated file: fall back to defaults
        return Settings()

    def save(self, settings: Settings) -> None:
        settings.validate()
        self.settings = settings
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(settings.to_dict(), indent=2), encoding="utf-8")

    def update(self, patch: dict[str, Any]) -> Settings:
        merged = self.settings.to_dict()
        merged.update(patch)
        updated = Settings.from_dict(merged)
        self.save(updated)
        return updated
