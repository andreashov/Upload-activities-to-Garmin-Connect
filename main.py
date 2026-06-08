from __future__ import annotations

import json as json_module
import logging
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, date as date_type
from pathlib import Path
from typing import Optional

from html import escape as html_escape

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse as StarletteJSONResponse

import requests

import garminconnect

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Garmin Workout Scheduler")

TOKEN_STORE = Path(os.getenv("TOKEN_DIR", str(Path(__file__).parent / "data" / "garmin_tokens")))
APP_PIN = os.getenv("APP_PIN", "")
GROUP_WORKOUT_FILE = Path(__file__).parent / "data" / "group_workout.json"

# ── AI workout generation (TEST — fjern denne seksjonen for å kutte funksjonen) ──
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
AI_WORKOUT_GEN_ENABLED = bool(GROQ_API_KEY)
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# session_id (uuid hex) → Garmin client (None = PIN ok, not yet logged in to Garmin)
_sessions: dict[str, Optional[garminconnect.Garmin]] = {}

# session_id → (client mid-login, display name) — set when Garmin challenges
# the login with a one-time verification code (MFA). Garmin does this far more
# often when sign-ins keep coming from "new" places, e.g. a server whose IP
# changes on every redeploy — see _restore_client/_save_tokens.
_pending_mfa: dict[str, tuple[garminconnect.Garmin, str]] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sid(request: Request) -> str:
    return request.cookies.get("sid", "")


def _get_client(request: Request) -> Optional[garminconnect.Garmin]:
    return _sessions.get(_sid(request))


def _set_cookie(response, sid: str) -> None:
    # samesite="lax" (ikke "strict"): "strict" sender ikke cookien ved den
    # første navigeringen når appen åpnes som installert PWA fra hjemskjermen
    # — den telles som en ekstern navigering. Da fant ikke serveren økten, og
    # man ble bedt om å logge inn på nytt hver gang, selv om cookien lå lagret.
    response.set_cookie(
        "sid", sid,
        httponly=True, samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )


def _token_dir(sid: str) -> Path:
    return TOKEN_STORE / sid


def _save_tokens(sid: str, client: garminconnect.Garmin) -> None:
    try:
        d = _token_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        client.client.dump(str(d))
    except Exception:
        logger.warning("Kunne ikke lagre tokens for session %s", sid[:8])


def _restore_client(sid: str) -> Optional[garminconnect.Garmin]:
    d = _token_dir(sid)
    if not d.exists():
        return None
    try:
        client = garminconnect.Garmin()
        # login(tokenstore=...) loads the saved tokens, refreshes them if
        # needed, and fetches the profile — which both populates
        # display_name/full_name and proves the tokens are still valid
        # against Garmin's API (a bare token load can't tell us that).
        client.login(tokenstore=str(d))
        return client
    except Exception:
        return None


def _save_group_workout(workout_def: dict, scheduled_date: str) -> None:
    share_id = uuid.uuid4().hex[:8]
    GROUP_WORKOUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    GROUP_WORKOUT_FILE.write_text(json_module.dumps({
        "workoutName": workout_def.get("workoutName", "Treningsøkt"),
        "workoutDef": workout_def,
        "date": scheduled_date,
        "uploadedAt": datetime.utcnow().isoformat(),
        "shareId": share_id,
    }, ensure_ascii=False))


_NO_WEEKDAYS = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
_NO_MONTHS = ["januar", "februar", "mars", "april", "mai", "juni",
              "juli", "august", "september", "oktober", "november", "desember"]


def _fmt_date_no(iso: str) -> str:
    d = date_type.fromisoformat(iso)
    return f"{_NO_WEEKDAYS[d.weekday()]} {d.day}. {_NO_MONTHS[d.month - 1]}"


def _load_group_workout() -> Optional[dict]:
    if not GROUP_WORKOUT_FILE.exists():
        return None
    try:
        data = json_module.loads(GROUP_WORKOUT_FILE.read_text())
        if data.get("date") and date_type.fromisoformat(data["date"]) < date_type.today():
            return None
        return data
    except Exception:
        return None


_WORKOUT_GEN_PROMPT = """Du er en assistent som lager treningsøkt-filer i Garmin Connect sitt JSON-format for "workout-service".

Svar KUN med gyldig JSON — ingen markdown-formatering, ingen forklaringer, ingen kodeblokk-merker.

Skjemaet ser slik ut (bruk nøyaktig disse feltnavnene og strukturen):

{
  "workoutName": "Navn på økten",
  "description": "Kort beskrivelse av økten (kan være null)",
  "sportType": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
  "subSportType": null,
  "estimatedDurationInSecs": 2880,
  "workoutSegments": [
    {
      "segmentOrder": 1,
      "sportType": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
      "workoutSteps": [ ... se eksempel under ... ]
    }
  ]
}

sportTypeKey kan være "running" (id 1), "cycling" (id 2) eller "swimming" (id 5) — velg ut fra beskrivelsen, bruk "running" hvis uklart.

Hvert steg i workoutSteps er enten et vanlig steg (ExecutableStepDTO) eller en gjentakelsesgruppe (RepeatGroupDTO).

Vanlig steg (ExecutableStepDTO) — eksempel for 5 minutters løpsintervall uten måltall:
{
  "type": "ExecutableStepDTO",
  "stepId": 3,
  "stepOrder": 3,
  "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
  "childStepId": 1,
  "description": null,
  "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
  "endConditionValue": 300.0,
  "endConditionCompare": null,
  "endConditionZone": null,
  "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
  "targetValueOne": null,
  "targetValueTwo": null,
  "zoneNumber": null
}

stepTypeKey-verdier å bruke: "warmup" (id 1, oppvarming), "cooldown" (id 2, avkjøling/nedjogg), "interval" (id 3, drag/løpsdel), "recovery" (id 4, restitusjon/pause mellom drag), "rest" (id 5, hvile), "repeat" (id 6, gjentakelse).

endCondition-typer:
- Fast varighet i sekunder: {"conditionTypeId": 2, "conditionTypeKey": "time"} med endConditionValue = antall sekunder (som flyttall, f.eks. 300.0)
- Fast distanse i meter*100: {"conditionTypeId": 3, "conditionTypeKey": "distance"} med endConditionValue = meter * 100 (f.eks. 1000 m = 100000.0)
- Åpen/valgfri (avsluttes med rundeknapp på klokken — typisk for oppvarming og nedjogg): {"conditionTypeId": 1, "conditionTypeKey": "lap.button"} med endConditionValue = null

For steg uten spesifikt måltall (fart, puls, etc.): bruk targetType {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"} og sett targetValueOne/targetValueTwo/zoneNumber til null.

Gjentakelsesgruppe (RepeatGroupDTO) — eksempel på "5 x (5 min løp + 2 min pause)":
{
  "type": "RepeatGroupDTO",
  "stepId": 2,
  "stepOrder": 2,
  "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
  "childStepId": 1,
  "numberOfIterations": 5,
  "workoutSteps": [
    { ...interval-steg som over... },
    { ...recovery-steg, samme struktur men stepType "recovery" (id 4)... }
  ],
  "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
  "endConditionValue": 5.0,
  "endConditionCompare": null,
  "smartRepeat": false,
  "skipLastRestStep": true
}

"skipLastRestStep": true betyr at den siste pausen/restitusjonen i siste runde hoppes over (vanlig for intervalløkter — du går rett fra siste drag til nedjogg). Sett til true med mindre beskrivelsen sier noe annet.

Sett childStepId til samme verdi for steg som hører sammen i en gruppe (f.eks. 1 for hele gjentakelsesgruppen og dens barn), og null for frittstående steg som oppvarming/nedjogg. stepId og stepOrder skal øke fortløpende gjennom hele økten.

estimatedDurationInSecs skal være et realistisk anslag på total varighet i sekunder, basert på summen av alle steg (bruk en fornuftig standardverdi for "lap.button"-steg, f.eks. 600 sekunder for oppvarming/nedjogg).
"""

# Redigerbar "oppskrift" — regler/preferanser for hvordan øktene skal bygges.
# Sett miljøvariabelen AI_WORKOUT_RECIPE for å overstyre uten kodeendring/redeploy
# (du kan endre den når som helst i Railway sine innstillinger).
_DEFAULT_AI_WORKOUT_RECIPE = """Regler for hvordan øktene skal bygges:

- Bruk alltid RepeatGroupDTO for gjentatte identiske drag — aldri flat liste med gjentakelser.
- stepId og stepOrder er globalt sekvensielle for alle steg, inkludert barn i grupper.
- Aldri to pauser på rad. Standard pauselengde: 1/3 av dragtid, med mindre annet er oppgitt.
- skipLastRestStep: true — kun når bolken etterfølges av noe som ikke er et intervalldrag (nedjogg, cooldown, slutt på økt).
- skipLastRestStep: false — når bolken etterfølges av en ny intervall-bolk, slik at siste pause blir overgangspause mellom blokkene. Ikke legg til en ekstra frittstående pause i tillegg.
- Oppvarming og nedjogg er åpne (lap.button, endConditionValue: null) som standard — avsluttes når utøver trykker lap. "Ca. X min" = åpen. Eksplisitt "X min" = bruk det som fast varighet.
- Ingen pulsmål som standard — styres på fart/følelse. Legg kun til HR-mål når det eksplisitt bes om. Sonesystem: Olympiatoppen (sone 3 ≈ 82-87 % av maks HR).
- Sonebasert HR-mål: sett zoneNumber + workoutTargetTypeKey "heart.rate.zone" med targetValueOne/Two = null. Faste BPM: bruk targetValueOne/targetValueTwo.
- description i selve økten skal være maks 200 tegn — kort og konkret, leses på en liten klokkeskjerm.
- Alltid metrisk: km, min/km, sekunder.
"""


def _ai_workout_recipe() -> str:
    return os.getenv("AI_WORKOUT_RECIPE", "").strip() or _DEFAULT_AI_WORKOUT_RECIPE


def _ask_ai_for_workout_json(system_prompt: str, description: str) -> str:
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": description.strip()},
            ],
            "temperature": 0.1,
            # Tvinger modellen til å returnere syntaktisk gyldig JSON — uten
            # denne hender det at den glemmer komma/anførselstegn og svaret
            # ikke lar seg parse (json.loads feiler med "Expecting ',' ...").
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )
    if not resp.ok:
        try:
            err = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            err = resp.text
        if resp.status_code == 429:
            wait = ""
            m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", err)
            if m:
                h, mi, s = (int(m.group(1) or 0), int(m.group(2) or 0), float(m.group(3) or 0))
                total_min = h * 60 + mi + (1 if s else 0)
                if total_min > 0:
                    wait = f" Prøv igjen om ca. {total_min} minutt{'er' if total_min != 1 else ''}."
            raise RuntimeError(
                "AI-generatoren har nådd dagens grense for antall forespørsler hos Groq."
                + wait
            )
        raise RuntimeError(f"Groq API-feil ({resp.status_code}): {err}")
    data = resp.json()
    text = data["choices"][0]["message"]["content"].strip()
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()


def _generate_workout_with_ai(description: str) -> dict:
    system_prompt = (
        _WORKOUT_GEN_PROMPT
        + "\n"
        + _ai_workout_recipe()
        + "\nSvar KUN med JSON-objektet for økten — ingen markdown, ingen forklaringer."
    )
    last_parse_error: Optional[Exception] = None
    for attempt in range(2):
        text = _ask_ai_for_workout_json(system_prompt, description)
        try:
            return json_module.loads(text)
        except json_module.JSONDecodeError as exc:
            last_parse_error = exc
            logger.warning("AI returnerte ugyldig JSON (forsøk %d/2): %s", attempt + 1, exc)
    raise RuntimeError(
        "AI-en svarte med ugyldig JSON og dette skjedde to ganger på rad — "
        "prøv igjen, eller omformuler beskrivelsen."
    ) from last_parse_error


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
        if APP_PIN and path.startswith("/api/") and path not in ("/api/pin", "/api/status") and not path.startswith("/api/share/"):
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
async def verify_pin(request: Request, pin: str = Form(...)):
    # Gjenbruk en eksisterende sid fra cookien (hvis den finnes) i stedet for
    # alltid å lage en ny — ellers blir den lagrede Garmin-innloggingen, som
    # ligger i en mappe knyttet til sid-en, foreldreløs hver gang serveren
    # restartes (f.eks. ved en deploy) og man må taste PIN-koden på nytt.
    sid = _sid(request) or uuid.uuid4().hex
    if not APP_PIN:
        _sessions[sid] = None
        r = JSONResponse({"status": "ok", "pinRequired": False})
        _set_cookie(r, sid)
        return r
    if pin != APP_PIN:
        raise HTTPException(status_code=401, detail="Feil PIN-kode")
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

    r = JSONResponse({"loggedIn": True, "displayName": name, "aiWorkoutGenEnabled": AI_WORKOUT_GEN_ENABLED})
    if not APP_PIN:
        _set_cookie(r, sid)
    return r


# ── Login / Logout ────────────────────────────────────────────────────────────

def _finish_garmin_login(sid: str, client: garminconnect.Garmin, display_name: str) -> JSONResponse:
    _pending_mfa.pop(sid, None)
    _save_tokens(sid, client)
    (_token_dir(sid) / "display_name.txt").write_text(display_name)
    _sessions[sid] = client
    r = JSONResponse({"status": "ok", "displayName": display_name})
    _set_cookie(r, sid)
    return r


@app.post("/api/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    sid = _sid(request)
    if not sid:
        sid = uuid.uuid4().hex
    if sid not in _sessions:
        _sessions[sid] = None
    display_name = email.split("@")[0].replace(".", " ").replace("_", " ").title()
    try:
        # return_on_mfa=True: Garmin challenges sign-ins from "new" places
        # (e.g. a server whose IP changes on every redeploy) with a one-time
        # verification code sent by e-mail/SMS. Without this flag the library
        # raises a generic GarminConnectAuthenticationError ("MFA Required but
        # no prompt_mfa mechanism supplied"), which looked exactly like a wrong
        # password — leaving the user stuck retrying (or resetting) for nothing.
        client = garminconnect.Garmin(email=email, password=password, return_on_mfa=True)
        mfa_status, _ = client.login()
        if mfa_status == "needs_mfa":
            _pending_mfa[sid] = (client, display_name)
            r = JSONResponse({"status": "mfa_required"})
            _set_cookie(r, sid)
            return r
        return _finish_garmin_login(sid, client, display_name)
    except garminconnect.GarminConnectAuthenticationError:
        raise HTTPException(status_code=401, detail="Feil e-post eller passord")
    except Exception as exc:
        logger.exception("Login failed")
        msg = str(exc)
        if "HTTP 403" in msg or "strategies exhausted" in msg:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Garmin avviste innloggingsforsøket (HTTP 403). Dette skjer "
                    "vanligvis når Garmin midlertidig blokkerer innlogging fra "
                    "tjenerens IP-adresse eller krever en sikkerhetssjekk — det "
                    "er ikke noe galt med brukernavn/passord. Vent noen minutter "
                    "og prøv igjen."
                ),
            )
        raise HTTPException(status_code=500, detail=msg)


@app.post("/api/login-mfa")
async def login_mfa(request: Request, code: str = Form(...)):
    sid = _sid(request)
    pending = _pending_mfa.get(sid)
    if not pending:
        raise HTTPException(
            status_code=400,
            detail="Ingen pågående innlogging. Skriv inn e-post og passord på nytt.",
        )
    client, display_name = pending
    try:
        client.resume_login(None, code.strip())
    except garminconnect.GarminConnectAuthenticationError:
        raise HTTPException(status_code=401, detail="Feil engangskode — prøv igjen")
    except Exception as exc:
        logger.exception("MFA resume failed")
        _pending_mfa.pop(sid, None)
        raise HTTPException(status_code=500, detail=str(exc))
    return _finish_garmin_login(sid, client, display_name)


@app.post("/api/logout")
async def logout(request: Request):
    sid = _sid(request)
    _pending_mfa.pop(sid, None)
    if sid in _sessions:
        _sessions[sid] = None
        shutil.rmtree(_token_dir(sid), ignore_errors=True)
    return {"status": "ok"}


# ── AI workout generation (TEST — fjern denne seksjonen for å kutte funksjonen) ──

@app.post("/api/generate-workout")
async def generate_workout(request: Request, description: str = Form(...)):
    if not AI_WORKOUT_GEN_ENABLED:
        raise HTTPException(status_code=404, detail="Funksjonen er ikke aktivert")
    if _get_client(request) is None:
        raise HTTPException(status_code=401, detail="Ikke innlogget")
    if not description.strip():
        raise HTTPException(status_code=400, detail="Beskriv økten du vil generere")
    try:
        workout_def = _generate_workout_with_ai(description)
    except Exception as exc:
        logger.exception("AI workout generation failed")
        raise HTTPException(status_code=500, detail=f"Kunne ikke generere økt: {exc}")
    return {"status": "ok", "workoutDef": workout_def}


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
        message = f"Treningsøkt lagt til i Garmin-kalenderen din\n{_fmt_date_no(scheduled_date)} ✓"
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


# ── De Grønnes økt ───────────────────────────────────────────────────────────

@app.post("/api/group-workout/set")
async def set_group_workout(
    request: Request,
    file: UploadFile = File(...),
    scheduled_date: str = Form(...),
):
    if _get_client(request) is None:
        raise HTTPException(status_code=401, detail="Ikke innlogget")
    try:
        workout_def = json_module.loads(await file.read())
    except json_module.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Ugyldig JSON: {exc}")
    _save_group_workout(workout_def, scheduled_date)
    return {"status": "ok", "message": f"De Grønnes økt er delt ✓ — {_fmt_date_no(scheduled_date)}"}


@app.get("/api/group-workout")
async def get_group_workout():
    data = _load_group_workout()
    if not data:
        return {"active": False}
    return {
        "active": True,
        "workoutName": data["workoutName"],
        "workoutDef": data["workoutDef"],
        "date": data["date"],
        "uploadedAt": data["uploadedAt"],
        "shareId": data.get("shareId"),
    }


@app.get("/api/share/{share_id}")
async def get_shared_workout(share_id: str):
    data = _load_group_workout()
    if not data or data.get("shareId") != share_id:
        raise HTTPException(status_code=404, detail="Lenken er utløpt eller ugyldig")
    return {
        "workoutName": data["workoutName"],
        "workoutDef": data["workoutDef"],
        "date": data["date"],
        "uploadedAt": data["uploadedAt"],
    }


@app.post("/api/group-workout/claim")
async def claim_group_workout(request: Request):
    client = _get_client(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Ikke innlogget")
    data = _load_group_workout()
    if not data:
        raise HTTPException(status_code=404, detail="Ingen aktiv gruppeøkt")
    content = json_module.dumps(data["workoutDef"]).encode()
    return await _upload_json_workout(client, content, data["date"])


@app.get("/share/{share_id}", response_class=HTMLResponse)
async def share_page(share_id: str):
    html = (Path(__file__).parent / "static" / "index.html").read_text()
    data = _load_group_workout()
    if data and data.get("shareId") == share_id:
        title = f"De Grønnes økt — {_fmt_date_no(data['date'])}"
        description = data.get("workoutName", "Treningsøkt")
        url = f"/share/{share_id}"
        meta = (
            f'<meta property="og:title" content="{html_escape(title)}">\n'
            f'  <meta property="og:description" content="{html_escape(description)}">\n'
            f'  <meta property="og:type" content="website">\n'
            f'  <meta property="og:url" content="{html_escape(url)}">\n'
            f'  <meta name="twitter:card" content="summary">\n'
        )
        html = html.replace("</head>", meta + "</head>", 1)
        html = html.replace(
            "<title>Garmin Treningsplanlegger</title>",
            f"<title>{html_escape(title)}</title>",
        )
    return HTMLResponse(html)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
