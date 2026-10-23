"""End-to-end: ring-sandbox emulator -> signed webhooks -> HeatWatch engine -> chime/escalation.

Starts both servers as real processes on free ports, replays scenarios at high speed,
and checks the outcome through the public API, including the access code and a restart
in the middle of an emergency. Skip with:  pytest -m "not integration"
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent
BIN = Path(sys.executable).parent
CODE = "sunflower"
SPEED = 600  # one real second = ten simulated minutes


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=1)
            return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


class Stack:
    def __init__(self, tmp: Path):
        self.emu_port, self.app_port = _free_port(), _free_port()
        self.emu_url = f"http://127.0.0.1:{self.emu_port}"
        self.url = f"http://127.0.0.1:{self.app_port}"
        self.env = {
            **os.environ,
            "RING_BASE_URL": self.emu_url,
            "PUBLIC_WEBHOOK_URL": f"{self.url}/webhooks/ring",
            "HEATWATCH_DATA_DIR": str(tmp),
            "HEATWATCH_ACCESS_CODE": CODE,
            "HEATWATCH_TICK_S": "0.05",
            "BEDROCK_ENABLED": "false",
        }
        self.emu = subprocess.Popen(
            [str(BIN / "ring-sandbox"), "serve", "--port", str(self.emu_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait(f"{self.emu_url}/_sandbox/health")
        self.app: subprocess.Popen | None = None
        self.start_app()

    def start_app(self) -> None:
        self.app = subprocess.Popen(
            [str(BIN / "uvicorn"), "app.main:app", "--port", str(self.app_port)],
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait(f"{self.url}/api/auth")
        self.http = httpx.Client(base_url=self.url, timeout=10)
        self.http.post("/api/login", json={"code": CODE}).raise_for_status()
        self._wait_ring()

    def _wait_ring(self) -> None:
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.state()["ring"]["ready"]:
                return
            time.sleep(0.2)
        raise RuntimeError("HeatWatch did not connect to the emulator")

    def stop_app(self) -> None:
        assert self.app is not None
        self.app.terminate()
        self.app.wait(10)

    def state(self) -> dict:
        r = self.http.get("/api/state")
        r.raise_for_status()
        return r.json()

    def run_until(self, scenario: str, weather: str, hhmm: str, timeout: float = 90) -> dict:
        self.http.post("/api/demo/start", json={"scenario": scenario, "weather": weather, "speed": SPEED}).raise_for_status()
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = self.state()
            if s["clock"]["now"][11:16] >= hhmm or s["demo"]["finished"]:
                return s
            time.sleep(0.1)
        raise TimeoutError(f"{scenario} did not reach {hhmm}")

    def close(self) -> None:
        for p in (self.app, self.emu):
            if p and p.poll() is None:
                p.terminate()
                p.wait(10)


@pytest.fixture(scope="module")
def stack(tmp_path_factory) -> Iterator[Stack]:
    st = Stack(tmp_path_factory.mktemp("heatwatch"))
    try:
        st.http.post(
            "/api/setup",
            json={
                "person_name": "Margaret", "person_nickname": "Mom", "location_name": "Berlin, Germany",
                "latitude": 52.52, "longitude": 13.405,
                "contacts": [
                    {"id": "c1", "name": "Anna", "relation": "Daughter", "phone": "", "has_key": False},
                    {"id": "c2", "name": "Mr. Weber", "relation": "Neighbor", "phone": "", "has_key": True},
                ],
                "quiet_start": "22:00", "quiet_end": "07:00",
                "consent_given": True, "consent_given_by": "Margaret", "share_snapshot": True,
            },
        ).raise_for_status()
        yield st
    finally:
        st.close()


def _titles(state: dict) -> list[str]:
    return [e["title"] for e in state["timeline"]]


def test_access_code_protects_the_api(stack: Stack):
    anonymous = httpx.get(f"{stack.url}/api/state")
    assert anonymous.status_code == 401
    wrong = httpx.post(f"{stack.url}/api/login", json={"code": "nope"})
    assert wrong.status_code == 401
    assert httpx.get(f"{stack.url}/api/auth").json() == {"required": True, "ok": False}


def test_false_alarm_is_caught_by_the_chime(stack: Stack):
    s = stack.run_until("heat_day_false_alarm", "heat", "13:40")
    titles = _titles(s)
    assert "Check-in started" in titles and "Check-in answered" in titles
    assert not [n for n in s["notifications"] if n["category"] == "alert"]
    assert s["ring"]["webhooks_verified"] > 0 and s["ring"]["webhooks_rejected"] == 0
    assert any(c["path"].endswith("/media/audio/playback") and c["status"] == 204 for c in s["ring"]["calls"])


def test_heat_moves_the_check_in_two_hours_earlier(stack: Stack):
    heat = stack.run_until("heat_day_emergency", "heat", "13:20")
    normal = stack.run_until("heat_day_emergency", "normal", "13:20")
    assert heat["incident"] is not None and heat["incident"]["notified_ids"] == ["c1"]
    assert normal["incident"] is None  # same events, normal weather: still within 4 h


def test_open_emergency_survives_a_restart(stack: Stack):
    before = stack.run_until("heat_day_emergency", "heat", "13:15")
    assert before["incident"]["stage"] == "contact"
    stack.http.post("/api/demo/pause")
    time.sleep(1.5)  # let the state file be written
    stack.stop_app()
    stack.start_app()
    after = stack.state()
    assert after["restored_from"] is not None
    assert after["incident"]["id"] == before["incident"]["id"]
    assert after["incident"]["stage"] == "contact"
    assert len(after["notifications"]) >= len(before["notifications"])
    assert "Restored after restart" in _titles(after)
    # and the family can still act on it
    stack.http.post("/api/incident/resolve", json={"contact_id": "c1"}).raise_for_status()
    assert stack.state()["incident"]["stage"] == "resolved"


def test_offline_chime_alerts_the_family_directly(stack: Stack):
    s = stack.run_until("device_trouble", "heat", "12:00")
    titles = _titles(s)
    assert "Front Door Sensor is offline" in titles and "Kitchen Chime is offline" in titles
    assert "Check-in without tone" in titles
    inc = s["incident"]
    assert inc["checkin_skipped"] and inc["stage"] == "resolved"  # she came back at 11:40
    assert any(n["category"] == "all_clear" and n["contact_id"] == "c1" for n in s["notifications"])
