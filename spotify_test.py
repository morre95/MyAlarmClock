#!/usr/bin/env python3
"""Enkelt Spotify-väckningstest (Python 3.10+).

INSTALLERA (Linux/macOS, i mappen där filen ligger):
  python3 -m venv .venv
  source .venv/bin/activate
  python -m pip install 'spotipy==2.25.1'

SPOTIFY, ENGÅNGSINSTÄLLNING:
  1. Skapa en app på https://developer.spotify.com/dashboard med Web API.
  2. Lägg in exakt redirect URI: http://127.0.0.1:8888/callback
  3. Lägg vid behov ditt konto under appens User Management.
  4. Ange Client ID (Client Secret behövs inte; vi använder PKCE):
       export SPOTIPY_CLIENT_ID='ditt_client_id'
  5. Kör på en dator med webbläsare och logga in:
       python spotify_test.py --login
     Öppna Spotify, spela något på önskad spelare och pausa sedan.
       python spotify_test.py --list-devices

TESTA (ersätt namnet och länken):
  python spotify_test.py --device 'Min dator' --playlist 'https://open.spotify.com/playlist/DITT_ID'
  python spotify_test.py --device 'Min dator' --playlist 'https://open.spotify.com/playlist/DITT_ID' --delay 7200

LARMLÄGE MED STOPP OCH SNOOZE:
  python spotify_test.py --alarm --device 'Web Player (Chrome)' --playlist 'https://open.spotify.com/playlist/DITT_ID'
  Skriv stop eller snooze och tryck Enter. Ctrl+C stänger också av larmet.
  Standard: 9 minuters snooze, automatiskt stopp efter 30 minuter från
  första bekräftade starten. Snooze ingår i dessa 30 minuter.
  Ändra med --snooze-minutes 9 och --auto-stop-minutes 30.
  Kort test: --snooze-minutes 0.1 --auto-stop-minutes 1
  --delay och --fallback fungerar även här. MP3-reserven startas om från
  början efter snooze. Spotify återupptar sin befintliga uppspelning.
  stop under startfördröjningen avbryter starten; under snooze avbryts
  återstarten. Stängd terminalindata avslutar också larmet med ett stoppförsök.
  Håll terminalen och datorn igång. Processdöd eller strömavbrott kan hindra
  automatisk avstängning. Spotify-anrop kan fördröja kommandon några sekunder.
  Utan --alarm används det tidigare starttestet som lämnar musiken igång.

DIAGNOSTIK (kör detta innan testet efter två timmars inaktivitet):
  python spotify_test.py --device 'Web Player (Chrome)' --playlist 'https://open.spotify.com/playlist/DITT_ID' --diagnose
  Lägg till | tee spotify-diagnostik.log för att spara utskriften.
  Efter bekräftad start läses status ungefär varannan sekund i 60 sekunder.
  DIAG-raderna är JSON med lokal tid, förfluten tid, spelare, spelar/pausad,
  låt och position i millisekunder. playing=null betyder att status saknas.
  API-fel loggas med feltyp/HTTP-status, aldrig tokens eller råa felsvar.
  Vid HTTP 429 respekteras Retry-After inom återstående diagnostiktid.
  Diagnostiken fortsätter vid paus, saknad spelare och API-fel. Den gör inga
  nya startförsök och startar inte MP3-reserven vid avbrott efter start.
  Låt webbspelaren vara öppen och lyssna under hela testet. Notera klockslaget
  om ljudet tystnar; API-status kan fortfarande visa att musiken spelas.
  Lyssna även några sekunder efter att skriptet avslutats.
  Exitkod 0 betyder bekräftad start och avslutad observation, inte 60 s ljud.

VALFRI MP3-RESERV (spelas på datorn där skriptet körs):
  Installera mpv, t.ex. sudo apt install mpv på Debian/Ubuntu/Raspberry Pi OS.
  Lägg till --fallback '/absolut/sokvag/reserv.mp3' vid testet ovan.
  Prova reservljudet separat:
    python spotify_test.py --fallback '/absolut/sokvag/reserv.mp3' --test-fallback

Kör första testet med hörbar men låg volym. Skriptet ändrar inte volymen.
Datorn och spelaren måste vara vakna; håll terminalen öppen under väntan.
Ctrl+C avbryter väntan/reservljudet; i --alarm pausas även Spotify.
Spotify fortsätter spela efter lyckat starttest utan --alarm.
Annan uppspelning på samma konto
kan avbrytas när testet startar. Använd inte prototypen som enda väckarklocka.

Token sparas privat intill skriptet i .spotify-test/token.json. Dela inte den
mappen. Radera den för att logga ut lokalt. Inga kontouppgifter ingår i filen.
Testet verifierar API-status, målspelare och spellista, inte fysiskt hörbart
ljud. Reserven används vid startfel; senare avbrott loggas bara med --diagnose.
Detta installerar ingen Spotify-spelare på Raspberry Pi.
"""

import argparse
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


def playlist_uri(value):
    match = re.fullmatch(r"(?:spotify:playlist:|https://open\.spotify\.com/(?:intl-[a-z]+/)?playlist/)([A-Za-z0-9]+)(?:\?[^\s]*)?", value)
    if not match:
        raise argparse.ArgumentTypeError("Ange en Spotify-spellistelänk eller spotify:playlist:ID.")
    return "spotify:playlist:" + match.group(1)


def delay_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("Fördröjningen måste vara ett ändligt tal >= 0.")
    return seconds


def positive_minutes(value):
    minutes = float(value)
    if minutes <= 0 or not math.isfinite(minutes * 60):
        raise argparse.ArgumentTypeError("Tiden måste vara ett ändligt antal minuter > 0.")
    return minutes


def select_device(devices, target):
    matches = [d for d in devices if d.get("id") == target]
    if not matches:
        matches = [d for d in devices if d.get("name") == target]
    if len(matches) != 1:
        raise RuntimeError("Spelaren saknas eller namnet är tvetydigt. Kör --list-devices och välj ID.")
    device = matches[0]
    if not device.get("id") or device.get("is_restricted"):
        raise RuntimeError("Spelaren kan inte styras via Spotify API.")
    return device


def start_and_verify(sp, target, uri):
    device = select_device(sp.devices().get("devices", []), target)
    sp.start_playback(device_id=device["id"], context_uri=uri)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = sp.current_playback() or {}
        if (state.get("is_playing")
                and (state.get("device") or {}).get("id") == device["id"]
                and (state.get("context") or {}).get("uri") == uri):
            print("OK: Spotify rapporterar uppspelning av vald spellista på", device["name"], flush=True)
            print("Kontrollera själv att ljudet hörs. Spotify stoppas i Spotify-appen.")
            return device, state
        time.sleep(1)
    raise RuntimeError("Kunde inte bekräfta uppspelning på rätt spelare inom 15 sekunder.")


def diagnostic_event(event, started, **fields):
    record = {
        "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "elapsed_s": round(time.monotonic() - started, 3),
        "event": event,
        **fields,
    }
    print("DIAG " + json.dumps(record, ensure_ascii=False), flush=True)


def log_playback(state, device_id, uri, started, request_ms=None):
    # Välj uttryckligen fält; skriv aldrig hela API-svar eller auth-objekt.
    device = (state or {}).get("device") or {}
    item = (state or {}).get("item") or {}
    context_uri = ((state or {}).get("context") or {}).get("uri")
    diagnostic_event(
        "sample", started,
        playback_available=bool(state),
        device={key: device.get(key) for key in
                ("id", "name", "type", "is_active", "is_restricted")},
        target_matches=device.get("id") == device_id,
        playing=(state or {}).get("is_playing"),
        position_ms=(state or {}).get("progress_ms"),
        track={key: item.get(key) for key in ("id", "name", "uri", "duration_ms")},
        context_uri=context_uri,
        playlist_matches=context_uri == uri,
        spotify_timestamp_ms=(state or {}).get("timestamp"),
        request_ms=request_ms,
    )


def diagnose_playback(sp, device, initial_state, uri):
    """Observera i 60 s efter bekräftad start, utan att styra uppspelningen."""
    started = time.monotonic()
    deadline = started + 60
    interval = 2
    samples, errors = 1, 0
    diagnostic_event("start", started, duration_s=60, interval_s=interval,
                     target_device_id=device["id"], target_device_name=device["name"])
    log_playback(initial_state, device["id"], uri, started)
    wait = interval
    while time.monotonic() < deadline:
        time.sleep(min(wait, max(0, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            break
        requested = time.monotonic()
        wait = interval
        try:
            state = sp.current_playback()
        except Exception as exc:
            errors += 1
            # Feltext, response, headers och traceback kan innehålla hemligheter.
            # Endast numerisk HTTP-status och Retry-After får lämna undantaget.
            status = getattr(exc, "http_status", None)
            status = status if isinstance(status, int) else None
            retry_after = None
            if status == 429:
                try:
                    retry_after = float((getattr(exc, "headers", None) or {}).get("Retry-After"))
                    if not math.isfinite(retry_after) or retry_after < 0:
                        retry_after = None
                except (TypeError, ValueError):
                    pass
                if retry_after is not None:
                    wait = max(interval, retry_after)
            diagnostic_event("api_error", started, error_type=type(exc).__name__,
                             http_status=status, retry_after_s=retry_after,
                             request_ms=round((time.monotonic() - requested) * 1000))
        else:
            samples += 1
            log_playback(state, device["id"], uri, started,
                         request_ms=round((time.monotonic() - requested) * 1000))
    diagnostic_event("end", started, samples=samples, api_errors=errors)
    print("Diagnostiken är klar. Spotify stoppas inte. Jämför loggen med vad du hörde.", flush=True)


def fallback(path):
    print("Startar lokal MP3-reserv. Ctrl+C stoppar ljudet.", flush=True)
    return subprocess.call(["mpv", "--no-video", "--loop-file=inf", "--", str(path)])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--login", action="store_true")
    modes.add_argument("--list-devices", action="store_true")
    modes.add_argument("--test-fallback", action="store_true")
    modes.add_argument("--alarm", action="store_true", help="Larmläge med stop, snooze och automatisk avstängning")
    parser.add_argument("--device", help="Exakt spelarnamn eller ID")
    parser.add_argument("--playlist", type=playlist_uri)
    parser.add_argument("--delay", type=delay_seconds, default=0, help="Sekunder före start")
    parser.add_argument("--diagnose", action="store_true", help="Logga uppspelningsstatus i 60 sekunder efter bekräftad start")
    parser.add_argument("--fallback", type=Path, help="Lokal reservfil, kräver mpv")
    parser.add_argument("--snooze-minutes", type=positive_minutes, help="Snoozetid i minuter, endast --alarm (standard: 9)")
    parser.add_argument("--auto-stop-minutes", type=positive_minutes, help="Total larmtid inklusive snooze, endast --alarm (standard: 30)")
    args = parser.parse_args()
    if args.diagnose and (args.login or args.list_devices or args.test_fallback or args.alarm):
        parser.error("--diagnose används med --device och --playlist, inte med andra testlägen.")
    if not args.alarm and (args.snooze_minutes is not None or args.auto_stop_minutes is not None):
        parser.error("--snooze-minutes och --auto-stop-minutes kräver --alarm.")
    if args.fallback:
        args.fallback = args.fallback.expanduser().resolve()
        if not args.fallback.is_file() or not shutil.which("mpv"):
            parser.error("Reservfilen måste finnas och mpv måste vara installerat.")
    if args.test_fallback:
        if not args.fallback:
            parser.error("--test-fallback kräver --fallback.")
        return fallback(args.fallback)
    if not (args.login or args.list_devices) and not (args.device and args.playlist):
        parser.error("Ange --device och --playlist, eller kör --login / --list-devices.")
    if not os.getenv("SPOTIPY_CLIENT_ID"):
        parser.error("Ange miljövariabeln SPOTIPY_CLIENT_ID enligt instruktionerna i filen.")
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyPKCE
        from spotipy.cache_handler import CacheFileHandler
    except ImportError:
        parser.error("Installera beroendet: python -m pip install 'spotipy==2.25.1'")
    if args.diagnose or args.alarm:
        # Spotipy kan annars logga råa API-fel och, vid DEBUG, auth-headers.
        # Våra egna loggrader ovan innehåller bara valda statusfält.
        for name in ("spotipy.client", "spotipy.oauth2", "spotipy.cache_handler"):
            logging.getLogger(name).disabled = True
    os.umask(0o077)
    directory = Path(__file__).resolve().parent / ".spotify-test"
    directory.mkdir(mode=0o700, exist_ok=True)
    cache = CacheFileHandler(cache_path=str(directory / "token.json"))
    auth = SpotifyPKCE(
        redirect_uri="http://127.0.0.1:8888/callback",
        scope="user-read-playback-state user-modify-playback-state",
        cache_handler=cache, requests_timeout=5,
    )
    if args.login:
        auth.get_access_token()
        print("Inloggning sparad. Nästa steg: --list-devices")
        return 0
    if not cache.get_cached_token():
        parser.error("Kör --login först. Larmtestet öppnar ingen inloggningsdialog.")

    class UnattendedAuth:
        def get_access_token(self, as_dict=False):
            token = auth.validate_token(cache.get_cached_token())
            if not token:
                raise RuntimeError("Inloggningen behöver förnyas. Kör --login igen.")
            return token if as_dict else token["access_token"]

    session = True
    if args.diagnose or args.alarm:
        from requests import Session

        # Requests standardsession gör inga automatiska återförsök. Spotipys
        # retry-adapter kan annars dölja HTTP-status/Retry-After bakom RetryError.
        session = Session()
    sp = spotipy.Spotify(auth_manager=UnattendedAuth(), requests_timeout=5,
                         retries=0, status_retries=0, requests_session=session)
    if args.list_devices:
        devices = sp.devices().get("devices", [])
        for d in devices:
            print(f"{d['name']} | ID: {d.get('id')} | aktiv: {d.get('is_active')} | begränsad: {d.get('is_restricted')}")
        if not devices:
            print("Inga spelare hittades. Öppna Spotify, spela något och pausa sedan.")
        return 0
    if args.alarm:
        from alarm_control import AlarmPlayback, SpotifyAlarmPlayer, TerminalCommands, run_alarm

        commands = TerminalCommands(sys.stdin)

        def spotify_player():
            # Slå upp spelaren efter --delay; enhets-ID kan ändras under väntan.
            device = select_device(sp.devices().get("devices", []), args.device)
            return SpotifyAlarmPlayer(sp, device["id"], args.playlist)

        return run_alarm(
            AlarmPlayback(spotify_player, args.fallback), commands.read, delay=args.delay,
            snooze_seconds=(args.snooze_minutes if args.snooze_minutes is not None else 9) * 60,
            auto_stop_seconds=(args.auto_stop_minutes if args.auto_stop_minutes is not None else 30) * 60,
        )
    print(f"Start om {args.delay:g} sekunder. Låt datorn vara vaken. Ctrl+C avbryter.", flush=True)
    deadline = time.monotonic() + args.delay
    while time.monotonic() < deadline:
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    try:
        device, initial_state = start_and_verify(sp, args.device, args.playlist)
    except Exception as exc:
        # Skriv inte tokens, API-svar eller autentiseringsuppgifter i loggen.
        status = getattr(exc, "http_status", None)
        print(f"Spotify-testet misslyckades ({type(exc).__name__}, HTTP {status or '-'}).", file=sys.stderr)
        if isinstance(exc, RuntimeError) and not args.diagnose:
            print(str(exc), file=sys.stderr)
        if args.fallback:
            fallback(args.fallback)
        return 1
    if args.diagnose:
        diagnose_playback(sp, device, initial_state, args.playlist)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAvbrutet. Eventuell Spotify-uppspelning stoppas i Spotify-appen.")
        sys.exit(130)
    except Exception as exc:
        print(f"Fel ({type(exc).__name__}). Kontrollera nätverk och Spotify-inställningar.", file=sys.stderr)
        sys.exit(1)
