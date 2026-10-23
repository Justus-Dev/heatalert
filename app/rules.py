"""When does missing activity become worrying? Heat makes HeatWatch more sensitive."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .settings import Settings

NONE = "none"
HEAT = "heat"
EXTREME = "extreme"

HEAT_LABELS = {
    NONE: "No heat warning",
    HEAT: "Heat warning",
    EXTREME: "Extreme heat warning",
}


@dataclass
class HeatAssessment:
    level: str
    reasons: list[str]

    @property
    def label(self) -> str:
        return HEAT_LABELS[self.level]


def assess_heat(
    settings: Settings, apparent_max_c: float | None, indoor_hot: bool, indoor_detail: str = ""
) -> HeatAssessment:
    """Combine the outdoor forecast (feels-like maximum) with the indoor Ring sensor.

    The thresholds follow the German Weather Service (DWD) heat-stress scale:
    feels-like >= 32 °C is "strong heat stress", >= 38 °C is "extreme heat stress".
    """
    level = NONE
    reasons: list[str] = []
    if apparent_max_c is not None:
        if apparent_max_c >= settings.extreme_apparent_c:
            level = EXTREME
            reasons.append(f"Feels like {apparent_max_c:.0f} °C today (extreme heat stress)")
        elif apparent_max_c >= settings.heat_apparent_c:
            level = HEAT
            reasons.append(f"Feels like {apparent_max_c:.0f} °C today (strong heat stress)")
    if indoor_hot:
        if level == NONE:
            level = HEAT
        reasons.append(indoor_detail or "Indoor temperature above the sensor threshold")
    return HeatAssessment(level, reasons)


def threshold_for(settings: Settings, heat_level: str) -> timedelta:
    minutes = {
        NONE: settings.threshold_normal_min,
        HEAT: settings.threshold_heat_min,
        EXTREME: settings.threshold_extreme_min,
    }[heat_level]
    return timedelta(minutes=minutes)


def in_quiet_hours(settings: Settings, at: datetime) -> bool:
    start = Settings.parse_hhmm(settings.quiet_start)
    end = Settings.parse_hhmm(settings.quiet_end)
    t = at.timetz().replace(tzinfo=None)
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # window crosses midnight, e.g. 22:00-07:00


def last_quiet_end(settings: Settings, at: datetime) -> datetime:
    """The most recent moment quiet hours ended at or before ``at``."""
    end = Settings.parse_hhmm(settings.quiet_end)
    candidate = at.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate > at:
        candidate -= timedelta(days=1)
    return candidate


def inactivity_reference(
    settings: Settings,
    now: datetime,
    *,
    last_activity: datetime | None,
    home_since: datetime | None,
    monitoring_since: datetime,
) -> datetime:
    """The moment the inactivity clock started counting.

    The latest of: last sign of life, coming home, end of night rest, start of monitoring.
    Sleeping in past the end of quiet hours is fine until the threshold passes.
    """
    candidates = [monitoring_since, last_quiet_end(settings, now)]
    if last_activity:
        candidates.append(last_activity)
    if home_since:
        candidates.append(home_since)
    return max(c for c in candidates if c <= now) if any(c <= now for c in candidates) else now
