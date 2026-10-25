"""Larmlivscykel: en ägare av uppspelning, snooze och monoton stopptid.

Spelare tillhandahåller start(), pause(), resume() och check(); fel kastas som
undantag. check() kontrollerar en ägd lokal process, utan Spotify-statuspollning.
Kommandoläsaren tar en timeout i sekunder och returnerar en rad eller None.
EOFError betyder stängd indata. Ingen timer lever vidare efter run_alarm().
"""

import os
import select
import subprocess
import sys
import time


def report_error(message, exc):
    """Skriv bara vald felmetadata, aldrig undantagstext eller API-svar."""
    status = getattr(exc, "http_status", None)
    status = status if isinstance(status, int) else "-"
    print(f"{message} ({type(exc).__name__}, HTTP {status}).", file=sys.stderr, flush=True)


class SpotifyAlarmPlayer:
    def __init__(self, sp, device_id, uri):
        self.sp = sp
        self.device_id = device_id
        self.uri = uri
        self.start_requested = False

    def matches(self, state, playing, uri=None):
        if not isinstance(state, dict):
            return False
        device = state.get("device")
        context = state.get("context")
        return (isinstance(device, dict) and device.get("id") == self.device_id
                and state.get("is_playing") is playing
                and (uri is None or isinstance(context, dict) and context.get("uri") == uri))

    def verify(self, playing, uri=None):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.matches(self.sp.current_playback(), playing, uri):
                return
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        raise RuntimeError("Kunde inte bekräfta spelarens tillstånd.")

    def start(self):
        # Ett avbrutet/tidsöverskridet anrop kan ändå ha nått Spotify.
        self.start_requested = True
        self.sp.start_playback(device_id=self.device_id, context_uri=self.uri)
        self.verify(True, self.uri)
        print("Spotify-starten är bekräftad. Kontrollera själv att ljudet hörs.", flush=True)

    def pause(self):
        if not self.start_requested:
            return
        try:
            state = self.sp.current_playback()
        except Exception:
            state = None  # Försök fortfarande skicka paus om statusläsningen misslyckas.
        if self.matches(state, False):
            return
        self.sp.pause_playback(device_id=self.device_id)
        self.verify(False)

    def resume(self):
        self.sp.start_playback(device_id=self.device_id)
        self.verify(True)


class Mp3AlarmPlayer:
    def __init__(self, path):
        self.path = path
        self.process = None

    def start(self):
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("Reservljudet körs redan.")
        self.process = subprocess.Popen(
            ["mpv", "--no-video", "--loop-file=inf", "--input-terminal=no", "--", str(self.path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.check()

    def check(self):
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError("MP3-processen avslutades oväntat.")

    def pause(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None

    def resume(self):
        self.start()


class AlarmPlayback:
    """Välj reserv endast vid startfel och behåll ägarskap över alla startförsök."""

    def __init__(self, spotify_factory, fallback_path=None):
        self.spotify_factory = spotify_factory
        self.fallback_path = fallback_path
        self.players = []
        self.current = None
        self.reserve = None

    def start(self):
        try:
            self.current = self.spotify_factory()
            self.players.append(self.current)
            self.current.start()
            return
        except Exception as exc:
            if self.fallback_path is None:
                raise
            report_error("Spotify-start misslyckades; försöker MP3-reserven", exc)
        try:
            self.pause()
        except Exception as exc:
            # Försök igen vid stopp; glöm inte ett okänt Spotify-startresultat.
            report_error("Spotify-paus kunde inte bekräftas. Kontrollera Spotify", exc)
        else:
            self.players.clear()
        self.reserve = Mp3AlarmPlayer(self.fallback_path)
        self.current = self.reserve
        self.players.append(self.current)
        self.current.start()
        print("Lokal MP3-reserv är startad. Kontrollera själv att ljudet hörs.", flush=True)

    def pause(self):
        first_error = None
        # Tysta MP3 först, även om ett tidigare Spotify-startförsök är okänt.
        for player in reversed(self.players):
            try:
                player.pause()
            except (Exception, KeyboardInterrupt) as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def resume(self):
        self.current.resume()

    def check(self):
        # Spotify körs separat; här kontrolleras bara vår egen lokala ljudprocess.
        if self.reserve is not None:
            self.reserve.check()


class TerminalCommands:
    """Läs kompletta rader med timeout utan TextIO:s dolda read-ahead-buffer."""

    def __init__(self, stream):
        self.fd = stream.fileno()
        self.buffer = b""
        self.eof = False

    def read(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                return line.decode("utf-8", errors="replace")
            if self.eof:
                if self.buffer:
                    line, self.buffer = self.buffer, b""
                    return line.decode("utf-8", errors="replace")
                raise EOFError
            ready, _, _ = select.select([self.fd], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                return None
            chunk = os.read(self.fd, 4096)
            if not chunk:
                self.eof = True
            else:
                self.buffer += chunk
                if len(self.buffer) > 8192:
                    raise RuntimeError("För lång kommandorad.")


def run_alarm(player, read_command, *, delay=0, snooze_seconds=540, auto_stop_seconds=1800):
    """Kör larmet tills stopp, EOF, Ctrl+C, fel eller automatisk avstängning."""
    start_deadline = time.monotonic() + delay
    stop_deadline = None
    snooze_deadline = None
    attempted_start = False
    result = 0
    print(f"Larmstart om {delay:g} sekunder. Skriv stop eller snooze och tryck Enter. "
          "Ctrl+C stänger av larmet.", flush=True)
    try:
        while True:
            now = time.monotonic()
            # Stopptiden har företräde om både snooze och avstängning förfaller.
            if stop_deadline is not None and now >= stop_deadline:
                print("Automatisk stopptid nådd.", flush=True)
                break
            if not attempted_start and now >= start_deadline:
                attempted_start = True  # Städa även en delvis lyckad start.
                player.start()
                player.check()
                stop_deadline = time.monotonic() + auto_stop_seconds
                print(f"Larmet är igång. Automatisk avstängning om "
                      f"{auto_stop_seconds / 60:g} minuter, inklusive snooze.", flush=True)
                continue
            if attempted_start and snooze_deadline is None:
                player.check()
            if snooze_deadline is not None and now >= snooze_deadline:
                snooze_deadline = None
                player.resume()
                player.check()
                print("Snooze är slut. Larmet spelar igen.", flush=True)
                continue

            next_deadline = stop_deadline if attempted_start else start_deadline
            if snooze_deadline is not None:
                next_deadline = min(next_deadline, snooze_deadline)
            command = read_command(min(1, max(0, next_deadline - time.monotonic())))
            if command is None:
                continue
            command = command.strip().lower()
            if command == "stop":
                break
            if command == "snooze":
                if not attempted_start:
                    print("Larmet har inte startat; snooze ändrar inte starttiden.", flush=True)
                else:
                    player.pause()
                    snooze_deadline = time.monotonic() + snooze_seconds
                    print(f"Snooze i {snooze_seconds / 60:g} minuter. "
                          "Den automatiska stopptiden ändras inte.", flush=True)
            else:
                print("Skriv stop eller snooze och tryck Enter.", flush=True)
    except EOFError:
        print("Inmatningen stängdes. Avslutar larmet.", flush=True)
    except KeyboardInterrupt:
        print("\nAvbryter larmet.", flush=True)
        result = 130
    except Exception as exc:
        report_error("Larmstyrningen misslyckades", exc)
        result = 1
    finally:
        try:
            if attempted_start:
                player.pause()
        except (Exception, KeyboardInterrupt) as exc:
            report_error("Kunde inte bekräfta att larmet tystnat. Kontrollera Spotify och reservljudet", exc)
            result = 1
        else:
            print("Larmet är avstängt. Ingen snooze återstartas.", flush=True)
    return result
