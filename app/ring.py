"""Everything HeatWatch asks of Ring goes through here.

Data plane: the Ring Partner API (`api.amazonvision.com`, or the ring-sandbox emulator
that speaks the same JSON:API shapes) via ring-sandbox's typed ``RingClient``:
device discovery, status, configurations, event history, snapshots, chime playback.

Control plane (sandbox only): the emulator's ``/_sandbox`` endpoints stand in for the
physical world - adding the indoor camera and climate sensor, registering the webhook
target, and injecting "someone walked past the camera" events during the demo.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from ring_sandbox import RingAPIError, RingClient

# Devices the demo household needs on top of ring-sandbox's default world
# (Front Door doorbell, Backyard camera, Kitchen Chime, Front Door Sensor).
SANDBOX_EXTRA_DEVICES = [
    {"kind": "camera", "name": "Living Room"},
    {"kind": "temp_humidity_sensor", "name": "Living Room Climate", "battery": 88},
]


class _LoggingTransport(httpx.BaseTransport):
    """Records every Partner API request so the demo can show real calls on screen."""

    def __init__(self, log: deque):
        self._inner = httpx.HTTPTransport()
        self._log = log

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        started = time.perf_counter()
        try:
            response = self._inner.handle_request(request)
        except httpx.HTTPError as exc:
            self._log.appendleft(_call(request, None, started, str(exc)))
            raise
        self._log.appendleft(_call(request, response.status_code, started))
        return response

    def close(self) -> None:
        self._inner.close()


def _call(request: httpx.Request, status: int | None, started: float, error: str = "") -> dict:
    path = request.url.raw_path.decode()
    return {
        "at": datetime.now(tz=UTC).isoformat(),
        "method": request.method,
        "host": request.url.host,
        "path": path,
        "status": status,
        "ms": round((time.perf_counter() - started) * 1000),
        "error": error,
    }


def _kind(bundle: Any) -> str:
    caps = bundle.capabilities
    image = (bundle.device.attributes.image_url or "").lower()
    if caps is not None and caps.is_chime:
        return "chime"
    if "doorbell" in image:
        return "doorbell"
    if caps is not None and caps.is_camera:
        return "camera"
    status = bundle.status.attributes if bundle.status else None
    if status is not None and status.temperature is not None:
        return "climate_sensor"
    if status is not None and status.contact_detection is not None:
        return "contact_sensor"
    return "sensor"


class RingGateway:
    def __init__(self, base_url: str, token: str, *, sandbox: bool):
        self.base_url = base_url.rstrip("/")
        self.sandbox = sandbox
        self.calls: deque[dict] = deque(maxlen=60)
        self._lock = threading.Lock()  # RingClient is synchronous; serialize use from threads
        self.client = RingClient(token, base_url=self.base_url, transport=_LoggingTransport(self.calls))
        self.devices: list[dict[str, Any]] = []
        self.last_error: str | None = None
        self.account: dict[str, Any] | None = None

    async def _run(self, fn, *args, **kwargs):
        def call():
            with self._lock:
                return fn(*args, **kwargs)

        return await asyncio.to_thread(call)

    # ------------------------------------------------------------------ data plane

    async def discover(self) -> list[dict[str, Any]]:
        """GET /v1/devices?include=status,capabilities,configurations"""
        try:
            bundles = await self._run(
                self.client.devices, include=["status", "capabilities", "configurations"]
            )
        except (RingAPIError, httpx.HTTPError) as exc:
            self.last_error = f"Ring device discovery failed: {exc}"
            return self.devices
        devices = []
        for b in bundles:
            status = b.status.attributes if b.status else None
            config = b.configurations.attributes if b.configurations else {}
            devices.append(
                {
                    "id": b.id,
                    "name": b.name,
                    "kind": _kind(b),
                    "online": b.online,
                    "battery": status.battery_percentage if status else None,
                    "temperature_max_c": ((config.get("thresholds") or {}).get("temperature") or {}).get(
                        "max"
                    ),
                    "chime_slots": [
                        s.get("event")
                        for s in ((config.get("audio") or {}).get("customizable_slots") or [])
                        if isinstance(s, dict)
                    ],
                }
            )
        self.devices = devices
        self.last_error = None
        return devices

    async def me(self) -> dict[str, Any] | None:
        try:
            user = await self._run(self.client.me)
            self.account = {"id": user.id, "name": f"{user.attributes.first_name} {user.attributes.last_name}"}
        except (RingAPIError, httpx.HTTPError, AttributeError) as exc:
            self.last_error = f"Ring account lookup failed: {exc}"
        return self.account

    def device_by_id(self, device_id: str) -> dict[str, Any] | None:
        return next((d for d in self.devices if d["id"] == device_id), None)

    def device_by_name(self, name: str) -> dict[str, Any] | None:
        return next((d for d in self.devices if d["name"] == name), None)

    async def play_chime(self, device_id: str, slot: str) -> bool:
        """POST /v1/devices/{id}/media/audio/playback - only the app's two slots are allowed."""
        try:
            await self._run(self.client.chime_play, device_id, slot)
            return True
        except (RingAPIError, httpx.HTTPError, ValueError) as exc:
            self.last_error = f"Chime playback failed: {exc}"
            return False

    async def latest_snapshot(self, device_id: str) -> tuple[bytes, str] | None:
        """POST /v1/devices/{id}/media/image/download (latest_in_range, 303 redirect flow)."""
        start = datetime.now(tz=UTC) - timedelta(hours=12)
        try:
            snap = await self._run(self.client.snapshot_latest, device_id, start=start, fmt="jpeg")
            return snap.content, snap.content_type
        except (RingAPIError, httpx.HTTPError, ValueError) as exc:
            self.last_error = f"Snapshot failed: {exc}"
            return None

    async def recent_activity(self, device_id: str, hours: float = 24) -> datetime | None:
        """Newest human/motion event from GET /v1/history/devices/{id}/events."""
        since = datetime.now(tz=UTC) - timedelta(hours=hours)
        try:

            def newest():
                for ev in self.client.events(
                    device_id, event_types=["motion.human", "motion.motion", "motion.other_motion"], since=since
                ):
                    return ev.attributes.start
                return None

            start_ms = await self._run(newest)
        except (RingAPIError, httpx.HTTPError) as exc:
            self.last_error = f"History lookup failed: {exc}"
            return None
        return datetime.fromtimestamp(start_ms / 1000, tz=UTC) if start_ms else None

    # ------------------------------------------------------------------ control plane (sandbox)

    async def _sandbox(self, method: str, path: str, **kwargs) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as http:
            resp = await http.request(method, path, **kwargs)
            resp.raise_for_status()
            return resp.json()

    async def provision_sandbox(self, webhook_url: str, signing_key: str) -> None:
        """Make the emulator look like the demo flat and point its webhooks at us."""
        if not self.sandbox:
            return
        state = await self._sandbox("GET", "/_sandbox/state")
        names = {d["name"] for d in state["devices"]}
        for spec in SANDBOX_EXTRA_DEVICES:
            if spec["name"] not in names:
                await self._sandbox("POST", "/_sandbox/devices", json=spec)
        if webhook_url not in state["webhooks"]:
            await self._sandbox(
                "POST", "/_sandbox/webhooks", json={"url": webhook_url, "signing_key": signing_key}
            )

    async def inject(self, device_id: str, event_type: str, sub_type: str | None = None) -> None:
        if not self.sandbox:
            raise RuntimeError("event injection is only available against the ring-sandbox emulator")
        await self._sandbox(
            "POST",
            "/_sandbox/events",
            json={"device_id": device_id, "type": event_type, "sub_type": sub_type},
        )

    async def sandbox_journal(self) -> list[dict[str, Any]]:
        """Emulator delivery journal: signed webhook deliveries and chime plays."""
        if not self.sandbox:
            return []
        try:
            state = await self._sandbox("GET", "/_sandbox/state")
        except httpx.HTTPError:
            return []
        return state.get("delivered", [])

    async def healthy(self) -> bool:
        try:
            if self.sandbox:
                await self._sandbox("GET", "/_sandbox/health")
            return True
        except httpx.HTTPError:
            return False
