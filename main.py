from __future__ import annotations

import hashlib
import json as json_module
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

TOKEN_STORE = Path(os.getenv("TOKEN_DIR", str(Path(__file__).parent / "data" / "garmin_tokens")))

APP_PIN = os.getenv("APP_PIN", "")
_SESSION_SECRET = hashlib.sha256(f"garmin-session-{APP_PIN}".encode()).hexdigest()

_client: garminconnect.Garmin | None = None


class PinMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if APP_PIN and path.startswith("/api/") and path != "/api/pin":
            session = request.cookies.get("session", "")
            if session != _SESSION_SECRET:
                return StarletteJSONResponse({"detail": "Krever PIN"}, status_code=401)
        return await call_next(request)


app.add_middleware(PinMiddleware)


def _save_tokens(client: garminconnect.Garmin) -> None:
    try:
        TOKEN_STORE.mkdir(parents=True, exist_ok=True)
        client.garth.dump(str(TOKEN_STORE))
    except Exception:
        logger.warning("Kunne ikke lagre tokens — må logge inn på nytt ved neste omstart")


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


def _garth_post(path: str, data: dict) -> dict:
    """Make an authenticated POST to the Garmin Connect API."""
    try:
        return _client.garth.request("POST", "connectapi", path, json=data).json()
    except Exception:
        # Fallback via connectapi method
        return _client.connectapi(path, method="POST", json=data)


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
        "session", _SESSION_SECRET,
        httponly=True, samesite="strict",
        max_age=60 * 60 * 24 * 30,
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
        _save_tokens(client)
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
    filename = file.filename or "workout"
    suffix = Path(filename).suffix.lower()

    if suffix == ".json":
        return await _upload_json_workout(content, scheduled_date)
    else:
        return await _upload_activity_file(content, suffix, scheduled_date)


async def _upload_json_workout(content: bytes, scheduled_date: str | None):
    """Create a structured workout via the Garmin workout-service API."""
    try:
        workout_def = json_module.loads(content)
    except json_module.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Ugyldig JSON: {exc}")

    try:
        result = _garth_post("/workout-service/workout", workout_def)
        logger.info("Workout created: %s", result)
        workout_id = result.get("workoutId")
        if not workout_id:
            raise HTTPException(status_code=400, detail=f"Garmin svarte uten workout ID: {result}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Kunne ikke opprette økt: {exc}")

    scheduled = False
    if scheduled_date:
        try:
            _garth_post(f"/workout-service/schedule/{workout_id}", {"date": scheduled_date})
            scheduled = True
        except Exception as exc:
            logger.warning("Schedule failed: %s", exc)

    if scheduled:
        message = f"Treningsøkt lagt til i Garmin-kalenderen din: {scheduled_date} ✓"
    else:
        message = f"Treningsøkt opprettet i Garmin Connect (ID: {workout_id})"

    return {"status": "ok", "message": message, "workoutId": workout_id, "scheduled": scheduled}


async def _upload_activity_file(content: bytes, suffix: str, scheduled_date: str | None):
    """Upload a FIT/TCX/GPX activity file via the upload-service."""
    with tempfile.NamedTemporaryFile(suffix=suffix or ".fit", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = _client.upload_activity(tmp_path)
        logger.info("Upload result: %s", result)

        detailed = result.get("detailedImportResult", {})
        successes = detailed.get("successes", [])
        failures = detailed.get("failures", [])

        if not successes:
            err_msg = "Filen ble ikke gjenkjent"
            if failures:
                msgs = failures[0].get("messages", [])
                if msgs:
                    err_msg = msgs[0].get("content", err_msg)
            raise HTTPException(status_code=400, detail=f"Opplasting feilet: {err_msg}")

        internal_id = successes[0].get("internalId")
        return {
            "status": "ok",
            "message": f"Aktivitetsfil lastet opp til Garmin Connect (ID: {internal_id}). Merk: FIT/TCX/GPX-filer lastes opp som gjennomførte aktiviteter, ikke som planlagte fremtidige treningsøkter. Bruk JSON-format for planlegging.",
            "internalId": internal_id,
            "scheduled": False,
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Upload error")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        os.unlink(tmp_path)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
