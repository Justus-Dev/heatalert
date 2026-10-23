"""The clock every HeatWatch rule reads.

Live mode is wall-clock time. Demo mode replays a day at a chosen speed (60 => one real
second is one simulated minute) so a two-hour inactivity threshold can be shown in a
two-minute video. Ring events still travel through real signed webhooks; only the
clock that judges them is compressed.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


class SimClock:
    def __init__(self, tz: str = "Europe/Berlin"):
        self.tz = ZoneInfo(tz)
        self.mode = "live"
        self.scale = 1.0
        self.paused = False
        self._anchor_real = time.time()
        self._anchor_sim = datetime.now(tz=self.tz)

    # ---------------------------------------------------------------- control

    def set_timezone(self, tz: str) -> None:
        self.tz = ZoneInfo(tz)

    def go_live(self) -> None:
        self.mode = "live"
        self.scale = 1.0
        self.paused = False

    def start_demo(self, start: datetime, scale: float) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=self.tz)
        self.mode = "demo"
        self.scale = float(scale)
        self.paused = False
        self._anchor_real = time.time()
        self._anchor_sim = start

    def set_scale(self, scale: float) -> None:
        if self.mode != "demo":
            return
        self._reanchor()
        self.scale = float(scale)

    def pause(self) -> None:
        if self.mode == "demo" and not self.paused:
            self._reanchor()
            self.paused = True

    def resume(self) -> None:
        if self.mode == "demo" and self.paused:
            self._anchor_real = time.time()
            self.paused = False

    def _reanchor(self) -> None:
        self._anchor_sim = self.now()
        self._anchor_real = time.time()

    # ---------------------------------------------------------------- reading

    def now(self) -> datetime:
        if self.mode == "live":
            return datetime.now(tz=self.tz)
        if self.paused:
            return self._anchor_sim
        elapsed = time.time() - self._anchor_real
        return self._anchor_sim + timedelta(seconds=elapsed * self.scale)

    def from_real_ms(self, ts_ms: int | None) -> datetime:
        """Map a real (wall-clock) Ring timestamp onto this clock.

        In demo mode an event that happened 2 real seconds ago happened 2 simulated
        minutes ago. Results never lie in the simulated future.
        """
        if ts_ms is None:
            return self.now()
        if self.mode == "live":
            return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).astimezone(self.tz)
        now = self.now()
        if self.paused:
            return now
        delta_real = ts_ms / 1000 - self._anchor_real
        mapped = self._anchor_sim + timedelta(seconds=delta_real * self.scale)
        return min(max(mapped, self._anchor_sim), now)

    def real_seconds(self, sim: timedelta) -> float:
        return sim.total_seconds() / self.scale

    def describe(self) -> dict:
        now = self.now()
        return {
            "mode": self.mode,
            "scale": self.scale,
            "paused": self.paused,
            "now": now.isoformat(),
            "label": (
                "Live time"
                if self.mode == "live"
                else f"Demo time ×{self.scale:g}" + (" · paused" if self.paused else "")
            ),
        }
