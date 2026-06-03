# Garmin Treningsplanlegger

Last opp treningsøkter (FIT/TCX/GPX) til Garmin Connect og planlegg dem til en fremtidig dato. Fungerer som en web-app på mobilen — legg til URL-en på hjemskjermen og bruk den som en app.

---

## Deploy til Railway (anbefalt — gratis, HTTPS, tilgjengelig fra mobil)

### 1. Opprett Railway-konto
Gå til [railway.app](https://railway.app) og logg inn med GitHub.

### 2. Opprett nytt prosjekt
- Velg **Deploy from GitHub repo**
- Velg `Upload-activities-to-Garmin-Connect`

### 3. Legg til persistent volum
I Railway-dashbordet for prosjektet:
- Gå til **Settings → Volumes**
- Legg til et volum montert på `/data`
- Dette lagrer Garmin-tokens mellom deploys

### 4. Sett miljøvariabler
I **Settings → Variables**, legg til:

| Variabel | Verdi | Beskrivelse |
|----------|-------|-------------|
| `APP_PIN` | f.eks. `1234` | PIN-kode for å beskytte appen |
| `TOKEN_DIR` | `/data/garmin_tokens` | Hvor tokens lagres (i volumet) |

### 5. Deploy
Railway deployer automatisk. Du får en URL som `ditt-prosjekt.up.railway.app`.

### 6. Legg til på hjemskjermen (mobil)
- **iPhone**: Åpne URL i Safari → Del-knapp → «Legg til på Hjem-skjerm»
- **Android**: Åpne URL i Chrome → meny → «Legg til på startskjerm»

---

## Kjøre lokalt (for utvikling)

```bash
git clone https://github.com/andreashov/Upload-activities-to-Garmin-Connect.git
cd Upload-activities-to-Garmin-Connect

python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt

uvicorn main:app --reload
# Åpne http://localhost:8000
```

---

## Bruk

1. Åpne appen — skriv inn PIN (hvis satt)
2. Logg inn med Garmin Connect e-post og passord (kun én gang)
3. Trykk på filopplastingsfeltet og velg en treningsfil
4. Velg dato du vil planlegge økten til
5. Trykk **Last opp til Garmin**

Treningsøkten legges til i Garmin-kalenderen og synkroniseres automatisk til klokken din.

---

## Om filformater

For planlagte fremtidige treningsøkter anbefales **FIT workout-filer**. Disse inneholder
strukturert treningsinformasjon (intervaller, pulssoner, varigheter) og synkroniseres
korrekt til Garmin-klokken.

> **Merk:** Garmin skiller mellom *workout FIT* (fremtidig planlagt økt) og *activity FIT*
> (registrering av gjennomført økt). Kun workout-filer kan legges til i kalenderen.

---

## Sikkerhet

- Passordet ditt lagres **aldri** — kun OAuth-tokens som automatisk fornyes
- Tokens lagres i Railway-volumet (privat, kun din instans)
- PIN-koden beskytter appen mot uautorisert tilgang
