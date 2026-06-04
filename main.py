from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse as StarletteJSONResponse

import garminconnect

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Garmin Workout Scheduler")

# Token storage: app-local ./data dir (survives requests, lost on redeploy)
# Override with TOKEN_DIR env var if needed
TOKEN_STORE = Path(os.getenv("TOKEN_DIR", str(Path(__file__).parent / "data" / "garmin_tokens")))

# Optional PIN protection — set APP_PIN environment variable to enable
APP_PIN = os.getenv("APP_PIN", "")
# Stable session secret derived from PIN so it survives server restarts
_SESSION_SECRET = hashlib.sha256(f"garmin-session-{APP_PIN}".encode()).hexdigest()

_client: garminconnect.Garmin | None = None


class PinMiddleware(BaseHTTPMiddleware):
    """Protect all /api/* routes (except /api/pin) when APP_PIN is set."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if APP_PIN and path.startswith("/api/") and path not in ("/api/pin", "/api/status"):
            session = request.cookies.get("session", "")
            if session != _SESSION_SECRET:
                return StarletteJSONResponse({"detail": "Krever PIN"}, status_code=401)
        return await call_next(request)


app.add_middleware(PinMiddleware)


def _try_restore_session() -> garminconnect.Garmin | None:
    if not TOKEN_STORE.exists():
        return None
    try:
        client = garminconnect.Garmin()
        client.garth.load(str(TOKEN_STORE))
        client.get_full_name()
        return client
    except Exception:
        return None


@app.on_event("startup")
async def startup():
    global _client
    _client = _try_restore_session()
    if _client:
        logger.info("Restored saved Garmin session")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/pin")
async def verify_pin(pin: str = Form(...)):
    if not APP_PIN:
        return {"status": "ok", "pinRequired": False}
    if pin != APP_PIN:
        raise HTTPException(status_code=401, detail="Feil PIN-kode")
    response = JSONResponse({"status": "ok", "pinRequired": True})
    response.set_cookie(
        "session",
        _SESSION_SECRET,
        httponly=True,
        samesite="strict",
        max_age=60 * 60 * 24 * 30,  # 30 dager
    )
    return response


@app.get("/api/status")
async def get_status():
    global _client
    if _client is None:
        _client = _try_restore_session()
    if _client is None:
        return {"loggedIn": False}
    try:
        name = _client.get_full_name()
        return {"loggedIn": True, "displayName": name}
    except Exception:
        _client = None
        return {"loggedIn": False}


@app.post("/api/login")
async def login(email: str = Form(...), password: str = Form(...)):
    global _client
    try:
        client = garminconnect.Garmin(email=email, password=password)
        client.login()
        TOKEN_STORE.mkdir(parents=True, exist_ok=True)
        client.garth.dump(str(TOKEN_STORE))
        _client = client
        return {"status": "ok", "displayName": client.get_full_name()}
    except garminconnect.GarminConnectAuthenticationError:
        raise HTTPException(status_code=401, detail="Feil e-post eller passord")
    except Exception as exc:
        logger.exception("Login failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/logout")
async def logout():
    global _client
    _client = None
    shutil.rmtree(TOKEN_STORE, ignore_errors=True)
    return {"status": "ok"}


@app.post("/api/upload")
async def upload_workout(
    file: UploadFile = File(...),
    scheduled_date: str = Form(default=None),
):
    global _client
    if _client is None:
        raise HTTPException(status_code=401, detail="Ikke innlogget")

    content = await file.read()
    suffix = Path(file.filename or "workout.fit").suffix.lower() or ".fit"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = _client.upload_activity(tmp_path)
        logger.info("Upload result: %s", result)

        detailed = result.get("detailedImportResult", {})
        successes = detailed.get("successes", [])
        failures = detailed.get("failures", [])

        if not successes:
            err_msg = "Ukjent feil"
            if failures:
                msgs = failures[0].get("messages", [])
                if msgs:
                    err_msg = msgs[0].get("content", err_msg)
            raise HTTPException(status_code=400, detail=f"Opplasting feilet: {err_msg}")

        internal_id = successes[0].get("internalId")
        scheduled = False
        schedule_note = ""

        if scheduled_date and internal_id:
            try:
                _client.garth.request(
                    "POST",
                    "connectapi",
                    f"/workout-service/schedule/{internal_id}",
                    json={"date": scheduled_date},
                )
                scheduled = True
            except Exception as exc:
                logger.warning("Scheduling failed: %s", exc)
                schedule_note = (
                    " Merk: Planlegging feilet — filen ble trolig tolket som en "
                    "gjennomført aktivitet, ikke en fremtidig treningsøkt."
                )

        if scheduled:
            message = f"Lagt til i Garmin-kalenderen din: {scheduled_date} ✓"
        else:
            message = f"Fil lastet opp (ID: {internal_id}).{schedule_note}"

        return {
            "status": "ok",
            "message": message,
            "internalId": internal_id,
            "scheduled": scheduled,
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Upload error")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        os.unlink(tmp_path)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
