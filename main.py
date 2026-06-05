from __future__ import annotations

import json as json_module
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse as StarletteJSONResponse

import garminconnect

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Garmin Workout Scheduler")

TOKEN_STORE = Path(os.getenv("TOKEN_DIR", str(Path(__file__).parent / "data" / "garmin_tokens")))
APP_PIN = os.getenv("APP_PIN", "")

# session_id (uuid hex) → Garmin client (None = PIN ok, not yet logged in to Garmin)
_sessions: dict[str, Optional[garminconnect.Garmin]] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sid(request: Request) -> str:
    return request.cookies.get("sid", "")


def _get_client(request: Request) -> Optional[garminconnect.Garmin]:
    return _sessions.get(_sid(request))


def _set_cookie(response, sid: str) -> None:
    response.set_cookie(
        "sid", sid,
        httponly=True, samesite="strict",
        max_age=60 * 60 * 24 * 30,
    )


def _token_dir(sid: str) -> Path:
    return TOKEN_STORE / sid


def _save_tokens(sid: str, client: garminconnect.Garmin) -> None:
    try:
        d = _token_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        client.garth.dump(str(d))
    except Exception:
        logger.warning("Kunne ikke lagre tokens for session %s", sid[:8])


def _restore_client(sid: str) -> Optional[garminconnect.Garmin]:
    d = _token_dir(sid)
    if not d.exists():
        return None
    try:
        client = garminconnect.Garmin()
        client.garth.load(str(d))
        client.get_full_name()
        return client
    except Exception:
        return None


def _api_post(client: garminconnect.Garmin, path: str, data: dict) -> dict:
    inner = getattr(client, "client", None) or getattr(client, "garth", None)
    if inner:
        for desc, fn in [
            ("inner.request(connectapi,POST)", lambda: inner.request("connectapi", path, method="POST", json=data).json()),
            ("inner.request(POST,connectapi)", lambda: inner.request("POST", "connectapi", path, json=data).json()),
            ("inner.post(connectapi)",         lambda: inner.post("connectapi", path, json=data).json()),
        ]:
            try:
                return fn()
            except Exception as e:
                logger.warning("%s: %s", desc, e)
    try:
        import garth as _garth
        return _garth.request("POST", "connectapi", path, json=data).json()
    except Exception as e:
        logger.warning("garth module: %s", e)
    raise RuntimeError("Ingen POST-klient tilgjengelig")


# ── Middleware ────────────────────────────────────────────────────────────────

class PinMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if APP_PIN and path.startswith("/api/") and path != "/api/pin":
            if _sid(request) not in _sessions:
                return StarletteJSONResponse({"detail": "Krever PIN"}, status_code=401)
        return await call_next(request)


app.add_middleware(PinMiddleware)


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    if not TOKEN_STORE.exists():
        return
    for d in TOKEN_STORE.iterdir():
        if d.is_dir():
            sid = d.name
            client = _restore_client(sid)
            if client:
                _sessions[sid] = client
                logger.info("Restored session %s…", sid[:8])


# ── Public ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# ── PIN ───────────────────────────────────────────────────────────────────────

@app.post("/api/pin")
async def verify_pin(pin: str = Form(...)):
    if not APP_PIN:
        sid = uuid.uuid4().hex
        _sessions[sid] = None
        r = JSONResponse({"status": "ok", "pinRequired": False})
        _set_cookie(r, sid)
        return r
    if pin != APP_PIN:
        raise HTTPException(status_code=401, detail="Feil PIN-kode")
    sid = uuid.uuid4().hex
    _sessions[sid] = None
    r = JSONResponse({"status": "ok", "pinRequired": True})
    _set_cookie(r, sid)
    return r


# ── Status ────────────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status(request: Request):
    sid = _sid(request)

    # No-PIN mode: transparently create a session for new visitors
    if not APP_PIN and sid not in _sessions:
        sid = uuid.uuid4().hex
        _sessions[sid] = None

    if sid not in _sessions:
        return JSONResponse({"loggedIn": False, "pinRequired": bool(APP_PIN)})

    client = _sessions[sid]

    # Lazy token restore (e.g. after server restart)
    if client is None:
        client = _restore_client(sid)
        if client:
            _sessions[sid] = client

    if client is None:
        r = JSONResponse({"loggedIn": False})
        if not APP_PIN:
            _set_cookie(r, sid)
        return r

    name_file = _token_dir(sid) / "display_name.txt"
    if name_file.exists():
        name = name_file.read_text().strip()
    else:
        try:
            name = client.get_full_name()
        except Exception:
            _sessions[sid] = None
            return JSONResponse({"loggedIn": False})

    r = JSONResponse({"loggedIn": True, "displayName": name})
    if not APP_PIN:
        _set_cookie(r, sid)
    return r


# ── Login / Logout ────────────────────────────────────────────────────────────

@app.post("/api/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    sid = _sid(request)
    if not sid:
        sid = uuid.uuid4().hex
    if sid not in _sessions:
        _sessions[sid] = None
    try:
        client = garminconnect.Garmin(email=email, password=password)
        client.login()
        display_name = email.split("@")[0].replace(".", " ").replace("_", " ").title()
        _save_tokens(sid, client)
        (_token_dir(sid) / "display_name.txt").write_text(display_name)
        _sessions[sid] = client
        r = JSONResponse({"status": "ok", "displayName": display_name})
        _set_cookie(r, sid)
        return r
    except garminconnect.GarminConnectAuthenticationError:
        raise HTTPException(status_code=401, detail="Feil e-post eller passord")
    except Exception as exc:
        logger.exception("Login failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/logout")
async def logout(request: Request):
    sid = _sid(request)
    if sid in _sessions:
        _sessions[sid] = None
        shutil.rmtree(_token_dir(sid), ignore_errors=True)
    return {"status": "ok"}


# ── Upload ────────────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_workout(
    request: Request,
    file: UploadFile = File(...),
    scheduled_date: str = Form(default=None),
    activity_name: str = Form(default=None),
):
    client = _get_client(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Ikke innlogget")

    content = await file.read()
    suffix = Path(file.filename or "workout").suffix.lower()

    if suffix == ".json":
        return await _upload_json_workout(client, content, scheduled_date, activity_name)
    else:
        return await _upload_activity_file(client, content, suffix, scheduled_date, activity_name)


async def _upload_json_workout(
    client: garminconnect.Garmin,
    content: bytes,
    scheduled_date: str | None,
    activity_name: str | None = None,
):
    try:
        workout_def = json_module.loads(content)
    except json_module.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Ugyldig JSON: {exc}")

    if activity_name:
        workout_def["workoutName"] = activity_name

    workout_id = None
    try:
        result = client.upload_workout(workout_def)
        logger.info("upload_workout result: %s", result)
        if isinstance(result, dict):
            workout_id = result.get("workoutId")
    except Exception as exc:
        logger.warning("upload_workout() failed: %s", exc)

    if not workout_id:
        try:
            result = _api_post(client, "/workout-service/workout", workout_def)
            logger.info("direct POST result: %s", result)
            workout_id = result.get("workoutId")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Kunne ikke opprette økt: {exc}")

    if not workout_id:
        raise HTTPException(status_code=400, detail="Garmin returnerte ingen workout ID")

    scheduled = False
    if scheduled_date:
        try:
            client.schedule_workout(workout_id, scheduled_date)
            scheduled = True
        except Exception as exc:
            logger.warning("schedule_workout() failed: %s", exc)

        if not scheduled:
            try:
                _api_post(client, f"/workout-service/schedule/{workout_id}", {"date": scheduled_date})
                scheduled = True
            except Exception as exc:
                logger.warning("direct schedule failed: %s", exc)

    if scheduled:
        message = f"Treningsøkt lagt til i Garmin-kalenderen din: {scheduled_date} ✓"
    else:
        message = f"Treningsøkt opprettet i Garmin Connect (ID: {workout_id})"

    return {"status": "ok", "message": message, "workoutId": workout_id, "scheduled": scheduled}


async def _upload_activity_file(
    client: garminconnect.Garmin,
    content: bytes,
    suffix: str,
    scheduled_date: str | None,
    activity_name: str | None = None,
):
    # Inject activity name into GPX <trk><name> if provided
    if activity_name and suffix == ".gpx":
        try:
            import xml.etree.ElementTree as ET
            ET.register_namespace('', 'http://www.topografix.com/GPX/1/1')
            root = ET.fromstring(content)
            ns = {'gpx': 'http://www.topografix.com/GPX/1/1'}
            trk = root.find('gpx:trk', ns) or root.find('trk')
            if trk is not None:
                name_el = trk.find('gpx:name', ns) or trk.find('name')
                if name_el is None:
                    name_el = ET.SubElement(trk, 'name')
                    trk.insert(0, name_el)
                name_el.text = activity_name
            content = ET.tostring(root, encoding='unicode', xml_declaration=True).encode()
        except Exception as e:
            logger.warning("GPX name injection failed: %s", e)

    with tempfile.NamedTemporaryFile(suffix=suffix or ".fit", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        result = client.upload_activity(tmp_path)
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
            "message": f"Aktivitetsfil lastet opp til Garmin Connect (ID: {internal_id}). Merk: FIT/TCX/GPX lastes opp som gjennomførte aktiviteter. Bruk JSON for planlagte økter.",
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
