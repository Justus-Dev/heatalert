"""The webhook endpoint must only trust correctly signed, fresh, unique Ring deliveries."""

import os
import tempfile

os.environ["HEATWATCH_DATA_DIR"] = tempfile.mkdtemp()
os.environ["BEDROCK_ENABLED"] = "false"
os.environ["RING_BASE_URL"] = "http://127.0.0.1:9"  # nothing listens: no emulator in unit tests

from datetime import UTC, datetime, timedelta  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from ring_sandbox import webhooks  # noqa: E402

from app.config import CONFIG  # noqa: E402
from app.main import app, engine  # noqa: E402

client = TestClient(app)  # no context manager: lifespan (emulator connection) is skipped


def _post(payload: dict, key: str = CONFIG.webhook_signing_key):
    body = webhooks.encode(payload)
    return client.post(
        "/webhooks/ring",
        content=body,
        headers={"Content-Type": "application/json", "X-Signature": webhooks.sign(key, body)},
    )


def _event(**kw):
    return webhooks.build_event(event_type="motion_detected", device_id="ava1.ring.device.X", sub_type="human", **kw)


def test_valid_signature_accepted():
    before = engine.webhooks_verified
    assert _post(_event()).status_code == 200
    assert engine.webhooks_verified == before + 1


def test_wrong_key_rejected():
    resp = _post(_event(), key="attacker")
    assert resp.status_code == 401


def test_tampered_body_rejected():
    payload = _event()
    body = webhooks.encode(payload)
    sig = webhooks.sign(CONFIG.webhook_signing_key, body)
    tampered = body.replace(b"human", b"animal")
    resp = client.post("/webhooks/ring", content=tampered, headers={"X-Signature": sig})
    assert resp.status_code == 401


def test_duplicate_delivery_ignored():
    payload = _event(request_id="dup-1")
    assert _post(payload).json()["status"] == "ok"
    assert _post(payload).json()["status"] == "duplicate ignored"


def test_stale_replay_rejected():
    payload = _event()
    payload["meta"]["time"] = (datetime.now(tz=UTC) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    assert _post(payload).status_code == 401


def test_setup_requires_consent():
    resp = client.post(
        "/api/setup",
        json={
            "person_name": "M", "person_nickname": "M", "location_name": "B", "latitude": 1, "longitude": 1,
            "contacts": [{"id": "c1", "name": "A", "relation": "D"}], "quiet_start": "22:00", "quiet_end": "07:00",
            "consent_given": False, "consent_given_by": "x", "share_snapshot": False,
        },
    )
    assert resp.status_code == 400
