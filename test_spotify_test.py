"""Offlinekontroller: ingen inloggning, Spotify-trafik eller ljuduppspelning."""

from contextlib import ExitStack, redirect_stdout, redirect_stderr
from datetime import datetime
import io
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import spotify_test as app


DEVICE = {"id": "chrome", "name": "Web Player (Chrome)", "type": "Computer"}
URI = "spotify:playlist:test"
SECRET = "DO_NOT_LOG_THIS_TOKEN"


def playback(playing=True, position=1000, device=None):
    return {
        "device": dict(DEVICE if device is None else device),
        "is_playing": playing,
        "progress_ms": position,
        "item": {"id": "song", "name": "Testlåt", "duration_ms": 200000},
        "context": {"uri": URI},
        "timestamp": 123456789,
        "access_token": SECRET,
    }


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError("Negativ väntetid")
        self.now += seconds


class ApiError(Exception):
    def __init__(self, status=500, retry_after=None):
        super().__init__(SECRET)
        self.http_status = status
        self.headers = {"Retry-After": retry_after, "Authorization": SECRET}


class DiagnosticsTests(unittest.TestCase):
    def observe(self, responses):
        clock = Clock()
        calls = []
        remaining = iter(responses)

        def current_playback():
            calls.append(clock.now)
            result = next(remaining, playback(position=9000))
            if isinstance(result, BaseException):
                raise result
            return result

        # Endast läsmetoden finns; varje försök att styra spelaren får testet att fallera.
        sp = Mock(spec=["current_playback"])
        sp.current_playback.side_effect = current_playback
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(app.time, "monotonic", clock.monotonic), \
                patch.object(app.time, "sleep", clock.sleep), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            app.diagnose_playback(sp, DEVICE, playback(), URI)
        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(SECRET, output)
        records = [json.loads(line[5:]) for line in stdout.getvalue().splitlines()
                   if line.startswith("DIAG ")]
        self.assertEqual(clock.now, 60)
        self.assertEqual(records[0]["event"], "start")
        self.assertEqual(records[-1]["event"], "end")
        self.assertEqual(records[-1]["elapsed_s"], 60)
        for record in records:
            self.assertIsNotNone(datetime.fromisoformat(record["time"]).tzinfo)
        return records, calls

    def test_pause_device_change_no_playback_and_errors_do_not_end_observation(self):
        records, calls = self.observe([
            playback(position=3000),
            playback(playing=False, position=3000),
            playback(device={"id": "other", "name": "Annan spelare"}),
            None,
            ApiError(),
            TimeoutError(SECRET),
            playback(position=7000),
        ])
        samples = [r for r in records if r["event"] == "sample"]
        errors = [r for r in records if r["event"] == "api_error"]
        self.assertEqual(calls, list(range(2, 60, 2)))
        self.assertTrue(samples[0]["target_matches"])
        self.assertEqual(samples[0]["position_ms"], 1000)
        self.assertFalse(samples[2]["playing"])
        self.assertFalse(samples[3]["target_matches"])
        self.assertFalse(samples[4]["playback_available"])
        self.assertIsNone(samples[4]["playing"])
        self.assertEqual(errors[0]["http_status"], 500)
        self.assertEqual(errors[1]["error_type"], "TimeoutError")
        self.assertIsNone(errors[1]["http_status"])
        self.assertTrue(samples[5]["playing"])
        self.assertEqual(records[-1]["api_errors"], 2)
        self.assertEqual(records[-1]["samples"], 28)

    def test_retry_after_is_respected_and_bounded_by_deadline(self):
        for retry_after, expected_second_call in [(10, 12), (120, None), (SECRET, 4),
                                                   ("NaN", 4), (-1, 4)]:
            with self.subTest(retry_after=retry_after):
                records, calls = self.observe([ApiError(429, retry_after)])
                self.assertEqual(calls[0], 2)
                if expected_second_call is None:
                    self.assertEqual(calls, [2])
                else:
                    self.assertEqual(calls[1], expected_second_call)
                self.assertEqual(records[-1]["api_errors"], 1)

    def test_null_device_item_and_context_are_logged(self):
        records, _ = self.observe([
            {"is_playing": False, "device": None, "item": None, "context": None,
             "progress_ms": None},
        ])
        sample = records[2]
        self.assertFalse(sample["playing"])
        self.assertIsNone(sample["device"]["id"])
        self.assertIsNone(sample["track"]["id"])
        self.assertIsNone(sample["position_ms"])
        self.assertFalse(sample["playlist_matches"])

    def test_slow_api_requests_are_timed_and_no_new_request_starts_after_deadline(self):
        clock, output = Clock(), io.StringIO()

        def current_playback():
            clock.sleep(5)
            return playback()

        sp = Mock(spec=["current_playback"])
        sp.current_playback.side_effect = current_playback
        with patch.object(app.time, "monotonic", clock.monotonic), \
                patch.object(app.time, "sleep", clock.sleep), redirect_stdout(output):
            app.diagnose_playback(sp, DEVICE, playback(), URI)
        records = [json.loads(line[5:]) for line in output.getvalue().splitlines()
                   if line.startswith("DIAG ")]
        samples = [r for r in records if r["event"] == "sample"]
        self.assertEqual(samples[1]["request_ms"], 5000)
        # Ett redan pågående anrop kan slutföras efter observationsfönstret.
        self.assertGreaterEqual(clock.now, 60)
        self.assertLessEqual(clock.now, 65)

    def test_ctrl_c_propagates_without_playback_commands(self):
        sp, clock = Mock(spec=["current_playback"]), Clock()
        sp.current_playback.side_effect = KeyboardInterrupt
        with patch.object(app.time, "monotonic", clock.monotonic), \
                patch.object(app.time, "sleep", clock.sleep), \
                redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            app.diagnose_playback(sp, DEVICE, playback(), URI)

    def test_start_returns_confirmed_state_and_sends_one_start_command(self):
        sp, state = Mock(), playback()
        sp.devices.return_value = {"devices": [DEVICE]}
        sp.current_playback.return_value = state
        with redirect_stdout(io.StringIO()):
            device, initial = app.start_and_verify(sp, DEVICE["name"], URI)
        self.assertEqual(device, DEVICE)
        self.assertIs(initial, state)
        sp.start_playback.assert_called_once_with(device_id=DEVICE["id"], context_uri=URI)
        self.assertEqual([call[0] for call in sp.mock_calls],
                         ["devices", "start_playback", "current_playback"])

    def test_diagnose_rejects_other_modes_before_authentication(self):
        for mode in ("--login", "--list-devices", "--test-fallback"):
            with self.subTest(mode=mode), \
                    patch.object(app.sys, "argv", ["spotify_test.py", "--diagnose", mode]), \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                app.main()
            self.assertEqual(error.exception.code, 2)

    def test_full_diagnostic_mode_with_real_spotipy_and_fake_transport(self):
        # Kontrollera även bibliotekets felhantering och loggning utan nätverk.
        import requests

        clock, output, sent = Clock(), io.StringIO(), []
        player_reads = 0

        def send(session, request, **kwargs):
            nonlocal player_reads
            sent.append((clock.now, request.method, request.url))
            retry = session.get_adapter(request.url).max_retries
            self.assertEqual(retry.total, 0)
            self.assertFalse(retry.status_forcelist)
            status, body, headers = 200, {}, {}
            if request.url.endswith("/devices"):
                body = {"devices": [DEVICE]}
            elif request.method == "PUT":
                status = 204
            else:
                player_reads += 1
                if player_reads in (2, 3):
                    status = 500 if player_reads == 2 else 429
                    body = {"error": {"message": SECRET}}
                    headers = {"Retry-After": "10", "Authorization": SECRET}
                else:
                    body = playback()
            response = requests.Response()
            response.status_code = status
            response._content = json.dumps(body).encode()
            response.headers.update(headers)
            response.url, response.request = request.url, request
            return response

        auth, cache = Mock(), Mock()
        token = {"access_token": SECRET, "refresh_token": SECRET}
        cache.get_cached_token.return_value = token
        auth.validate_token.return_value = token
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.object(app, "__file__", str(Path(directory) / "spotify_test.py")))
            stack.enter_context(patch.object(app.sys, "argv", [
                "spotify_test.py", "--device", DEVICE["name"],
                "--playlist", URI, "--diagnose",
            ]))
            stack.enter_context(patch.dict(app.os.environ, {"SPOTIPY_CLIENT_ID": "test-client"}))
            stack.enter_context(patch.object(app.os, "umask"))
            stack.enter_context(patch("spotipy.oauth2.SpotifyPKCE", return_value=auth))
            stack.enter_context(patch("spotipy.cache_handler.CacheFileHandler", return_value=cache))
            stack.enter_context(patch.object(requests.Session, "send", send))
            stack.enter_context(patch.object(app.time, "monotonic", clock.monotonic))
            stack.enter_context(patch.object(app.time, "sleep", clock.sleep))
            reserve = stack.enter_context(patch.object(app, "fallback"))
            for name in ("spotipy.client", "spotipy.oauth2", "spotipy.cache_handler"):
                logger = logging.getLogger(name)
                stack.enter_context(patch.object(logger, "disabled", False))
                stack.enter_context(patch.object(logger, "level", logging.DEBUG))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(output))
            self.assertEqual(app.main(), 0)
            reserve.assert_not_called()
        self.assertNotIn(SECRET, output.getvalue())
        records = [json.loads(line[5:]) for line in output.getvalue().splitlines()
                   if line.startswith("DIAG ")]
        self.assertEqual([r["http_status"] for r in records if r["event"] == "api_error"],
                         [500, 429])
        self.assertEqual(sum(method == "PUT" for _, method, _ in sent), 1)
        self.assertEqual([t for t, method, url in sent if method == "GET" and url.endswith("/player")][:4],
                         [0, 2, 4, 14])


if __name__ == "__main__":
    unittest.main()
