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

VALFRI MP3-RESERV (spelas på datorn där skriptet körs):
  Installera mpv, t.ex. sudo apt install mpv på Debian/Ubuntu/Raspberry Pi OS.
  Lägg till --fallback '/absolut/sokvag/reserv.mp3' vid testet ovan.
  Prova reservljudet separat:
    python spotify_test.py --fallback '/absolut/sokvag/reserv.mp3' --test-fallback

Kör första testet med hörbar men låg volym. Skriptet ändrar inte volymen.
Datorn och spelaren måste vara vakna; håll terminalen öppen under väntan.
Ctrl+C avbryter väntan/reservljudet. Spotify stoppas i Spotify-appen.
Spotify fortsätter spela efter lyckat test. Annan uppspelning på samma konto
kan avbrytas när testet startar. Använd inte prototypen som enda väckarklocka.

Token sparas privat intill skriptet i .spotify-test/token.json. Dela inte den
mappen. Radera den för att logga ut lokalt. Inga kontouppgifter ingår i filen.
Testet verifierar API-status, målspelare och spellista, inte fysiskt hörbart
ljud. Reserven används vid startfel; avbrott senare under låten övervakas inte.
Detta installerar ingen Spotify-spelare på Raspberry Pi.
"""

import argparse
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
            print("Kontrollera själv att ljudet hörs. Stoppa sedan i Spotify.")
            return
        time.sleep(1)
    raise RuntimeError("Kunde inte bekräfta uppspelning på rätt spelare inom 15 sekunder.")


def fallback(path):
    print("Startar lokal MP3-reserv. Ctrl+C stoppar ljudet.", flush=True)
    return subprocess.call(["mpv", "--no-video", "--loop-file=inf", "--", str(path)])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--login", action="store_true")
    modes.add_argument("--list-devices", action="store_true")
    modes.add_argument("--test-fallback", action="store_true")
    parser.add_argument("--device", help="Exakt spelarnamn eller ID")
    parser.add_argument("--playlist", type=playlist_uri)
    parser.add_argument("--delay", type=delay_seconds, default=0, help="Sekunder före start")
    parser.add_argument("--fallback", type=Path, help="Lokal reservfil, kräver mpv")
    args = parser.parse_args()
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

    sp = spotipy.Spotify(auth_manager=UnattendedAuth(), requests_timeout=5,
                         retries=0, status_retries=0)
    if args.list_devices:
        devices = sp.devices().get("devices", [])
        for d in devices:
            print(f"{d['name']} | ID: {d.get('id')} | aktiv: {d.get('is_active')} | begränsad: {d.get('is_restricted')}")
        if not devices:
            print("Inga spelare hittades. Öppna Spotify, spela något och pausa sedan.")
        return 0
    print(f"Start om {args.delay:g} sekunder. Låt datorn vara vaken. Ctrl+C avbryter.", flush=True)
    deadline = time.monotonic() + args.delay
    while time.monotonic() < deadline:
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    try:
        start_and_verify(sp, args.device, args.playlist)
        return 0
    except Exception as exc:
        # Skriv inte tokens, API-svar eller autentiseringsuppgifter i loggen.
        status = getattr(exc, "http_status", None)
        print(f"Spotify-testet misslyckades ({type(exc).__name__}, HTTP {status or '-'}).", file=sys.stderr)
        if isinstance(exc, RuntimeError):
            print(str(exc), file=sys.stderr)
        if args.fallback:
            fallback(args.fallback)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAvbrutet. Eventuell Spotify-uppspelning stoppas i Spotify-appen.")
        sys.exit(130)
    except Exception as exc:
        print(f"Fel ({type(exc).__name__}). Kontrollera nätverk och Spotify-inställningar.", file=sys.stderr)
        sys.exit(1)
