from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles

import garminconnect

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Garmin Workout Scheduler")

TOKEN_STORE = Path.home() / ".garmin_workout_tokens"
_client: garminconnect.Garmin | None = None


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
                    " Merk: Planlegging til kalender feilet — sjekk at filen "
                    "er en workout-fil (fremtidig økt), ikke en activity-fil (gjennomført økt)."
                )

        if scheduled:
            message = f"Treningsøkt lastet opp og planlagt til {scheduled_date}!"
        else:
            message = f"Fil lastet opp til Garmin Connect (ID: {internal_id}).{schedule_note}"

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
