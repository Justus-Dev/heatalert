"""HeatWatch HTTP server: Ring webhook receiver, family web app, demo controls."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from ring_sandbox import webhooks
from ring_sandbox.webhooks import SIGNATURE_HEADER, SignatureError

from .config import CONFIG
from .engine import HeatWatch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("heatwatch.http")

SESSION_COOKIE = "heatwatch_session"

engine = HeatWatch(CONFIG)


async def _loop() -> None:
    while True:
        try:
            await engine.tick()
        except Exception:
            log.exception("tick failed")
        await asyncio.sleep(CONFIG.tick_s)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    await engine.startup()
    task = asyncio.create_task(_loop())
    yield
    task.cancel()
    with contextlib.suppress(OSError):
        engine.save_state()


app = FastAPI(title="HeatWatch", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=CONFIG.web_dir), name="static")


# ---------------------------------------------------------------------- access code


def _session_token(code: str) -> str:
    return hmac.new(code.encode(), b"heatwatch-session-v1", hashlib.sha256).hexdigest()


def require_access(request: Request) -> None:
    """Optional family access code (HEATWATCH_ACCESS_CODE). Empty = open local demo."""
    if not CONFIG.access_code:
        return
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if not hmac.compare_digest(cookie, _session_token(CONFIG.access_code)):
        raise HTTPException(401, "access code required")


public = APIRouter(prefix="/api")
api = APIRouter(prefix="/api", dependencies=[Depends(require_access)])


@public.get("/auth")
async def auth_status(request: Request) -> dict[str, bool]:
    try:
        require_access(request)
        ok = True
    except HTTPException:
        ok = False
    return {"required": bool(CONFIG.access_code), "ok": ok}


class LoginIn(BaseModel):
    code: str


@public.post("/login")
async def login(body: LoginIn, response: Response) -> dict[str, bool]:
    if not CONFIG.access_code:
        return {"ok": True}
    if not hmac.compare_digest(body.code.strip().encode(), CONFIG.access_code.encode()):
        await asyncio.sleep(0.5)  # slow down guessing
        raise HTTPException(401, "wrong access code")
    response.set_cookie(
        SESSION_COOKIE,
        _session_token(CONFIG.access_code),
        httponly=True,
        samesite="lax",
        max_age=30 * 24 * 3600,
    )
    return {"ok": True}


@public.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


# ---------------------------------------------------------------------- Ring webhooks


@app.post("/webhooks/ring")
async def ring_webhook(request: Request) -> Response:
    raw = await request.body()  # verify the raw bytes, never re-serialized JSON
    try:
        event = webhooks.parse(
            raw,
            signing_key=CONFIG.webhook_signing_key,
            signature=request.headers.get(SIGNATURE_HEADER),
            max_age_s=CONFIG.webhook_max_age_s,
        )
    except SignatureError as exc:
        engine.webhooks_rejected += 1
        log.warning("rejected webhook: %s", exc)
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    except ValueError as exc:
        engine.webhooks_rejected += 1
        return JSONResponse({"error": f"malformed payload: {exc}"}, status_code=400)
    if not engine.accept_request_id(event.request_id):
        return JSONResponse({"status": "duplicate ignored"})
    await engine.handle_event(event)
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------- family app


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(CONFIG.web_dir / "index.html", headers={"Cache-Control": "no-store"})


@api.get("/state")
async def state() -> dict[str, Any]:
    return engine.state()


class SetupIn(BaseModel):
    person_name: str
    person_nickname: str
    location_name: str
    latitude: float
    longitude: float
    contacts: list[dict[str, Any]]
    quiet_start: str
    quiet_end: str
    consent_given: bool
    consent_given_by: str
    share_snapshot: bool


@api.post("/setup")
async def setup(body: SetupIn) -> dict[str, Any]:
    if not body.consent_given:
        raise HTTPException(400, "HeatWatch only runs with the consent of the person it looks after")
    if not body.consent_given_by.strip():
        raise HTTPException(400, "Please record who gave consent")
    patch = body.model_dump(exclude={"consent_given", "consent_given_by", "share_snapshot"})
    patch["setup_complete"] = True
    patch["consent"] = {
        "given": True,
        "given_by": body.consent_given_by.strip(),
        "given_at": datetime.now(tz=UTC).isoformat(),
        "share_snapshot": body.share_snapshot,
    }
    try:
        settings = engine.store.update(patch)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    engine.log_system("Setup completed", f"Consent recorded by {body.consent_given_by.strip()}")
    return settings.to_dict()


@api.put("/settings")
async def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    patch.pop("consent", None)  # consent changes only through setup
    patch.pop("setup_complete", None)
    try:
        settings = engine.store.update(patch)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    engine.clock.set_timezone(settings.timezone)
    return settings.to_dict()


@api.post("/setup/reset")
async def reset_setup() -> dict[str, Any]:
    settings = engine.store.update(
        {"setup_complete": False, "consent": {"given": False, "given_by": "", "given_at": None, "share_snapshot": False}}
    )
    return settings.to_dict()


class ContactAction(BaseModel):
    contact_id: str
    note: str = ""


@api.post("/incident/ack")
async def acknowledge(body: ContactAction) -> dict[str, Any]:
    try:
        await engine.acknowledge(body.contact_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return engine.state()


@api.post("/incident/resolve")
async def resolve(body: ContactAction) -> dict[str, Any]:
    try:
        await engine.resolve(body.contact_id, body.note)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return engine.state()


@api.get("/snapshots/{snapshot_id}")
async def snapshot(snapshot_id: str) -> Response:
    item = engine.snapshots.get(snapshot_id)
    if item is None:
        raise HTTPException(404, "snapshot not found (snapshots are kept in memory only)")
    content, mime, _ = item
    return Response(content=content, media_type=mime, headers={"Cache-Control": "no-store"})


@api.post("/summary")
async def summary() -> dict[str, Any]:
    return await engine.summary()


@api.post("/ring/refresh")
async def ring_refresh() -> dict[str, Any]:
    await engine.connect_ring()
    return engine.state()["ring"]


# ---------------------------------------------------------------------- demo controls


@api.get("/demo/scenarios")
async def scenarios() -> list[dict[str, Any]]:
    return [s.to_dict() for s in engine.director.scenarios()]


class DemoStart(BaseModel):
    scenario: str
    weather: str | None = None
    speed: float | None = None


@api.post("/demo/start")
async def demo_start(body: DemoStart) -> dict[str, Any]:
    if not engine.ring_ready:
        raise HTTPException(409, "Ring emulator not connected - start `ring-sandbox serve --port 8787` first")
    if body.speed is not None and not 1 <= body.speed <= 1200:
        raise HTTPException(400, "speed must be between 1 and 1200")
    try:
        await engine.director.start(body.scenario, body.weather, body.speed)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return engine.state()


class SpeedIn(BaseModel):
    speed: float


@api.post("/demo/speed")
async def demo_speed(body: SpeedIn) -> dict[str, Any]:
    if not 1 <= body.speed <= 1200:
        raise HTTPException(400, "speed must be between 1 and 1200")
    engine.clock.set_scale(body.speed)
    return engine.clock.describe()


@api.post("/demo/pause")
async def demo_pause() -> dict[str, Any]:
    engine.clock.pause()
    return engine.clock.describe()


@api.post("/demo/resume")
async def demo_resume() -> dict[str, Any]:
    engine.clock.resume()
    return engine.clock.describe()


@api.post("/demo/live")
async def demo_live() -> dict[str, Any]:
    await engine.go_live()
    return engine.state()


class ManualIn(BaseModel):
    action: str


@api.post("/demo/inject")
async def demo_inject(body: ManualIn) -> dict[str, Any]:
    try:
        label = await engine.director.manual(body.action)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "label": label}


@api.get("/demo/journal")
async def demo_journal() -> list[dict[str, Any]]:
    return (await engine.ring.sandbox_journal())[-25:]


app.include_router(public)
app.include_router(api)
