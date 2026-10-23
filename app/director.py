"""Demo director: replays a scripted day through the ring-sandbox emulator.

Scenario files are plain ring-sandbox scenarios (``name``, ``description``, ``steps`` with
``offset_s``/``type``/``sub_type``/``device``) so ``ring-sandbox play`` accepts them too.
HeatWatch-only settings live under a top-level ``heatwatch:`` key, which ring-sandbox
ignores.

Every step is injected into the emulator *at the moment the demo clock reaches it*; the
emulator then signs the event and delivers it to /webhooks/ring like real Ring would.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .escalation import CHECKIN

if TYPE_CHECKING:
    from .engine import HeatWatch

log = logging.getLogger("heatwatch.director")

# Real past days from the Open-Meteo archive (Berlin). The heat numbers are observed, not made up.
WEATHER_VARIANTS = {
    "heat": {"date": "2022-07-20", "fallback": (37.7, 36.4), "label": "Heatwave replay · 20 Jul 2022"},
    "normal": {"date": "2019-05-21", "fallback": (22.4, 22.5), "label": "Normal day replay · 21 May 2019"},
}

# Manual "the physical world does something" buttons in the demo panel.
MANUAL_ACTIONS = {
    "indoor_motion": ("indoor_camera", "motion_detected", "human", "Person moves in the living room"),
    "pet_motion": ("indoor_camera", "motion_detected", "animal", "Cat walks past the indoor camera"),
    "door_open": ("door_sensor", "contact_sensor_faulted", None, "Front door opens"),
    "door_close": ("door_sensor", "contact_sensor_cleared", None, "Front door closes"),
    "outside_person": ("entrance_camera", "motion_detected", "human", "Person outside the front door"),
    "doorbell": ("entrance_camera", "button_press", None, "Someone rings the doorbell"),
    "indoor_hot": ("climate_sensor", "temperature_exceeded", None, "Indoor temperature too high"),
    "indoor_ok": ("climate_sensor", "temperature_cleared", None, "Indoor temperature back to normal"),
    "camera_offline": ("indoor_camera", "device_offline", None, "Indoor camera goes offline"),
    "camera_online": ("indoor_camera", "device_online", None, "Indoor camera back online"),
    "chime_offline": ("chime", "device_offline", None, "Chime goes offline"),
    "chime_online": ("chime", "device_online", None, "Chime back online"),
    "door_sensor_offline": ("door_sensor", "device_offline", None, "Door sensor goes offline"),
    "door_sensor_online": ("door_sensor", "device_online", None, "Door sensor back online"),
}


@dataclass
class Step:
    offset_s: float
    type: str = "motion_detected"
    sub_type: str | None = None
    device: str | None = None
    duration_ms: int = 20_000
    heat_only: bool = False


@dataclass
class Scenario:
    id: str
    name: str
    description: str
    start: str
    end: str
    weather: str
    speed: float
    initial_presence: str
    checkin_response_after_min: float | None
    steps: list[Step] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "start": self.start,
            "end": self.end,
            "weather": self.weather,
            "speed": self.speed,
            "checkin_response_after_min": self.checkin_response_after_min,
            "steps": len(self.steps),
        }


def load_scenario(path: Path) -> Scenario:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    meta = raw.get("heatwatch") or {}
    steps = [Step(**s) for s in raw.get("steps", [])]
    steps += [Step(**s, heat_only=True) for s in meta.get("heat_only_steps", [])]
    steps.sort(key=lambda s: s.offset_s)
    response = (meta.get("checkin_response") or {}).get("after_min")
    return Scenario(
        id=path.stem,
        name=raw["name"],
        description=raw.get("description", ""),
        start=str(meta.get("start", "08:00")),
        end=str(meta.get("end", "18:00")),
        weather=meta.get("weather", "heat"),
        speed=float(meta.get("speed", 60)),
        initial_presence=meta.get("initial_presence", "home"),
        checkin_response_after_min=float(response) if response is not None else None,
        steps=steps,
    )


class Director:
    def __init__(self, engine: HeatWatch, scenarios_dir: Path):
        self.engine = engine
        self.scenarios_dir = scenarios_dir
        self.scenario: Scenario | None = None
        self.variant: str | None = None
        self.start_at: datetime | None = None
        self.end_at: datetime | None = None
        self.pending: list[Step] = []
        self.fired = 0
        self.running = False
        self.finished = False
        self.generation = 0
        self._responded_to: set[str] = set()
        self._response_due: tuple[str, datetime] | None = None
        self.last_error: str | None = None

    def scenarios(self) -> list[Scenario]:
        return [load_scenario(p) for p in sorted(self.scenarios_dir.glob("*.yml"))]

    async def start(self, scenario_id: str, variant: str | None, speed: float | None) -> None:
        path = self.scenarios_dir / f"{scenario_id}.yml"
        if not path.exists() or path.parent != self.scenarios_dir:
            raise ValueError(f"unknown scenario {scenario_id!r}")
        sc = load_scenario(path)
        variant = variant or sc.weather
        if variant not in WEATHER_VARIANTS:
            raise ValueError(f"unknown weather variant {variant!r}")
        weather = WEATHER_VARIANTS[variant]
        day = datetime.fromisoformat(weather["date"])
        hh, mm = (int(x) for x in sc.start.split(":"))
        eh, em = (int(x) for x in sc.end.split(":"))
        tz = self.engine.clock.tz
        start_at = day.replace(hour=hh, minute=mm, tzinfo=tz)
        # Stop the previous replay and freeze its clock *before* awaiting anything:
        # otherwise the tick loop keeps firing steps against the old clock meanwhile.
        self.stop()
        self.generation += 1
        self.engine.clock.pause()
        await self.engine.begin_demo(
            start_at,
            speed or sc.speed,
            sc.initial_presence,
            weather_date=weather["date"],
            weather_fallback=weather["fallback"],
            label=f"{sc.name} · {weather['label']}",
        )
        self.start_at = start_at
        self.end_at = day.replace(hour=eh, minute=em, tzinfo=tz)
        self.scenario, self.variant = sc, variant
        self.pending = [s for s in sc.steps if variant == "heat" or not s.heat_only]
        self.fired = 0
        self._responded_to = set()
        self._response_due = None
        self.finished = False
        self.last_error = None
        self.running = True

    def stop(self) -> None:
        self.running = False
        self.pending = []
        self._response_due = None

    async def tick(self, now: datetime) -> None:
        if not self.running or self.start_at is None:
            return
        generation = self.generation
        while self.pending and self.start_at + timedelta(seconds=self.pending[0].offset_s) <= now:
            step = self.pending.pop(0)
            await self._inject_named(step.device, step.type, step.sub_type)
            if generation != self.generation:
                return  # a new replay started while we were injecting
            self.fired += 1
        await self._respond_to_checkin(now)
        if self.end_at and now >= self.end_at and not self.finished:
            self.finished = True
            self.engine.clock.pause()
            self.engine.log_system("Demo day finished", "Clock paused. Choose another scenario to replay.")

    async def _respond_to_checkin(self, now: datetime) -> None:
        """False-alarm scenarios: the person moves a little after hearing the chime."""
        sc = self.scenario
        inc = self.engine.escalator.active
        if sc is None or sc.checkin_response_after_min is None:
            return
        if inc is not None and inc.stage == CHECKIN and inc.id not in self._responded_to:
            self._responded_to.add(inc.id)
            self._response_due = (inc.id, inc.opened_at + timedelta(minutes=sc.checkin_response_after_min))
        if self._response_due and now >= self._response_due[1]:
            self._response_due = None
            await self.manual("indoor_motion")

    async def manual(self, action: str) -> str:
        if action not in MANUAL_ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        role, event_type, sub_type, label = MANUAL_ACTIONS[action]
        device = self.engine.device_for_role(role)
        if device is None:
            raise ValueError(f"no Ring device has the role {role!r}")
        await self._inject(device["id"], event_type, sub_type)
        return label

    async def _inject_named(self, name: str | None, event_type: str, sub_type: str | None) -> None:
        device = self.engine.ring.device_by_name(name) if name else None
        if device is None:
            self.last_error = f"Scenario device {name!r} not found in Ring device list"
            log.warning(self.last_error)
            return
        await self._inject(device["id"], event_type, sub_type)

    async def _inject(self, device_id: str, event_type: str, sub_type: str | None) -> None:
        try:
            await self.engine.ring.inject(device_id, event_type, sub_type)
        except Exception as exc:
            self.last_error = f"Emulator injection failed: {exc}"
            log.warning(self.last_error)

    def to_dict(self) -> dict:
        sc = self.scenario
        return {
            "running": self.running,
            "finished": self.finished,
            "scenario": sc.to_dict() if sc else None,
            "variant": self.variant,
            "variant_label": WEATHER_VARIANTS[self.variant]["label"] if self.variant else None,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "steps_fired": self.fired,
            "steps_pending": len(self.pending),
            "next_step_at": (
                (self.start_at + timedelta(seconds=self.pending[0].offset_s)).isoformat()
                if self.pending and self.start_at
                else None
            ),
            "last_error": self.last_error,
        }
