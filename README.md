# Garmin Treningsplanlegger

Last opp treningsøkter (FIT/TCX/GPX) til Garmin Connect og planlegg dem til en fremtidig dato. Planlagte økter synkroniseres automatisk til Garmin-klokken din.

## Krav

- Python 3.10+
- En Garmin Connect-konto

## Kom i gang

```bash
# 1. Klon repoet
git clone https://github.com/andreashov/Upload-activities-to-Garmin-Connect.git
cd Upload-activities-to-Garmin-Connect

# 2. Opprett virtuelt miljø og installer avhengigheter
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 3. Start serveren
uvicorn main:app --reload

# 4. Åpne nettleseren
#    http://localhost:8000
```

## Bruk

1. Logg inn med din Garmin Connect e-post og passord
2. Dra og slipp en treningsfil inn i opplastingsområdet
3. Velg dato du vil planlegge økten til (valgfritt)
4. Klikk **Last opp til Garmin**

Innloggingstokens lagres i `~/.garmin_workout_tokens/` — du trenger ikke logge inn igjen neste gang.

## Filformater

| Format | Støttet | Anbefalt for |
|--------|---------|--------------|
| `.fit` | Ja | Strukturerte treningsøkter (workout-filer) |
| `.tcx` | Ja | Treningsprogram fra eldre Garmin-verktøy |
| `.gpx` | Ja | GPS-ruter |

For planlegging til fremtidig dato fungerer best **FIT workout-filer** — disse inneholder
strukturert treningsinformasjon (intervaller, soner, varigheter) og synkroniseres korrekt
til Garmin-klokken.

## Merk om filtyper

Garmin skiller mellom to typer FIT-filer:
- **Workout FIT** — fremtidig planlagt økt (kan legges på kalender)
- **Activity FIT** — registrering av en gjennomført økt (vises i historikk)

Hvis planlegging til kalender feiler, er det sannsynligvis fordi filen er en activity-fil.

## Sikkerhet

- Passordet ditt sendes kun til din lokale server
- Garmin-passordet lagres **aldri** på disk — kun OAuth-tokens som automatisk fornyes
- Slett `~/.garmin_workout_tokens/` for å logge ut permanent
