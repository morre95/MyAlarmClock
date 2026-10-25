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


class AlarmCliTests(unittest.TestCase):
    def test_invalid_alarm_flags_and_times_fail_before_authentication(self):
        invalid = [
            ["--alarm", "--diagnose"], ["--alarm", "--login"],
            ["--alarm", "--list-devices"], ["--alarm", "--test-fallback"],
            ["--auto-stop-minutes", "1"], ["--snooze-minutes", "1"],
        ]
        for option in ("--auto-stop-minutes", "--snooze-minutes"):
            for value in ("0", "-1", "nan", "inf", "1e308", "text"):
                invalid.append(["--alarm", option, value])
        for arguments in invalid:
            with self.subTest(arguments=arguments), \
                    patch.object(app.sys, "argv", ["spotify_test.py", *arguments]), \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                app.main()
            self.assertEqual(error.exception.code, 2)

    def execute_alarm(self, commands, flags=(), fail_pause=False, fail_resume=False,
                      devices=None, confirm_start=True, valid_login=True):
        import alarm_control as alarm
        import requests

        clock, output, sent = Clock(), io.StringIO(), []
        pending = list(commands)
        state = playback(playing=False)

        def read_command(timeout):
            if pending and pending[0][0] <= clock.now + timeout:
                when, command = pending.pop(0)
                clock.now = max(clock.now, when)
                return command
            clock.sleep(timeout)
            return None

        def send(session, request, **kwargs):
            sent.append((clock.now, request.method, request.url, request.body))
            status, body = 200, state
            if "/devices" in request.url:
                body = {"devices": [DEVICE] if devices is None else devices}
            elif request.method == "PUT":
                status, body = 204, {}
                if "/pause" in request.url:
                    if fail_pause:
                        status, body = 500, {"error": {"message": SECRET}}
                    else:
                        state["is_playing"] = False
                else:
                    if fail_resume and request.body is None:
                        status, body = 503, {"error": {"message": SECRET}}
                    else:
                        state["is_playing"] = confirm_start
            response = requests.Response()
            response.status_code = status
            response._content = json.dumps(body).encode()
            response.url, response.request = request.url, request
            return response

        auth, cache = Mock(), Mock()
        token = {"access_token": SECRET, "refresh_token": SECRET}
        cache.get_cached_token.return_value = token
        auth.validate_token.return_value = token if valid_login else None
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            reserve = Path(directory) / "reserv.mp3"
            reserve.touch()
            arguments = [str(reserve) if flag == "RESERVE" else flag for flag in flags]
            stack.enter_context(patch.object(app, "__file__", str(Path(directory) / "spotify_test.py")))
            stack.enter_context(patch.object(app.sys, "argv", [
                "spotify_test.py", "--alarm", "--device", DEVICE["name"],
                "--playlist", URI, *arguments,
            ]))
            stack.enter_context(patch.dict(app.os.environ, {"SPOTIPY_CLIENT_ID": "test-client"}))
            stack.enter_context(patch.object(app.os, "umask"))
            stack.enter_context(patch("spotipy.oauth2.SpotifyPKCE", return_value=auth))
            stack.enter_context(patch("spotipy.cache_handler.CacheFileHandler", return_value=cache))
            stack.enter_context(patch.object(requests.Session, "send", send))
            stack.enter_context(patch.object(app.shutil, "which", return_value="/usr/bin/mpv"))
            stack.enter_context(patch.object(alarm, "TerminalCommands", return_value=Mock(read=read_command)))
            stack.enter_context(patch.object(alarm.time, "monotonic", clock.monotonic))
            stack.enter_context(patch.object(alarm.time, "sleep", clock.sleep))
            reserve_player = stack.enter_context(patch.object(alarm, "Mp3AlarmPlayer"))
            for name in ("spotipy.client", "spotipy.oauth2", "spotipy.cache_handler"):
                logger = logging.getLogger(name)
                stack.enter_context(patch.object(logger, "disabled", False))
                stack.enter_context(patch.object(logger, "level", logging.DEBUG))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(output))
            code = app.main()
            reserve_player.assert_not_called()
        self.assertNotIn(SECRET, output.getvalue())
        return code, clock, state, sent, output.getvalue()

    def test_cli_device_selection_failure_explains_reason_without_sending_start(self):
        cases = [([], "device_not_found"), ([DEVICE, DEVICE], "device_ambiguous"),
                 ([{**DEVICE, "is_restricted": True}], "device_uncontrollable")]
        for devices, reason in cases:
            with self.subTest(reason=reason):
                code, _, _, sent, output = self.execute_alarm([], devices=devices)
                self.assertEqual(code, 1)
                self.assertIn(reason, output)
                self.assertIn("--list-devices", output)
                self.assertFalse(any(method == "PUT" for _, method, _, _ in sent))

    def test_cli_unconfirmed_start_logs_only_selected_state_and_still_cleans_up(self):
        code, clock, state, sent, output = self.execute_alarm([], confirm_start=False)
        self.assertEqual(code, 1)
        self.assertEqual(clock.now, 5)
        self.assertIn("playback_unconfirmed", output)
        self.assertIn("start", output)
        records = [json.loads(line[6:]) for line in output.splitlines()
                   if line.startswith("ALARM ")]
        self.assertEqual(records, [{"event": "verification_timeout", "expected_playing": True,
                                   "playback_available": True, "target_matches": True,
                                   "playing": False, "playlist_matches": True}])
        self.assertFalse(state["is_playing"])
        self.assertEqual(sum(method == "PUT" for _, method, _, _ in sent), 1)

    def test_cli_invalid_cached_login_explains_how_to_renew_without_network_request(self):
        code, _, _, sent, output = self.execute_alarm([], valid_login=False)
        self.assertEqual(code, 1)
        self.assertIn("login_required", output)
        self.assertIn("--login", output)
        self.assertEqual(sent, [])

    def test_cli_snooze_resumes_existing_queue_and_auto_stops_at_original_time(self):
        code, clock, state, sent, _ = self.execute_alarm(
            [(1, "snooze")], ["--snooze-minutes", "0.1", "--auto-stop-minutes", "0.2"])
        self.assertEqual(code, 0)
        self.assertEqual(clock.now, 12)
        self.assertFalse(state["is_playing"])
        starts = [(t, body) for t, method, url, body in sent if method == "PUT" and "/play?" in url]
        self.assertEqual(starts, [(0, json.dumps({"context_uri": URI})), (7, None)])
        pauses = [t for t, method, url, _ in sent if method == "PUT" and "/pause?" in url]
        self.assertEqual(pauses, [1, 12])
        self.assertTrue(all("device_id=chrome" in url for _, method, url, _ in sent if method == "PUT"))

    def test_cli_defaults_and_stop_during_snooze(self):
        code, clock, state, sent, output = self.execute_alarm([(1, "snooze"), (2, "stop")])
        self.assertEqual(code, 0)
        self.assertEqual(clock.now, 2)
        self.assertFalse(state["is_playing"])
        self.assertIn("30 minuter", output)
        self.assertIn("Snooze i 9 minuter", output)
        self.assertEqual(sum(method == "PUT" and "/play?" in url for _, method, url, _ in sent), 1)

    def test_cli_stop_before_delay_sends_no_spotify_requests(self):
        code, _, _, sent, _ = self.execute_alarm([(1, "stop")], ["--delay", "10"])
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])

    def test_cli_pause_error_is_not_reported_as_success_and_does_not_start_reserve(self):
        code, _, _, _, output = self.execute_alarm(
            [(1, "stop")], ["--fallback", "RESERVE"], fail_pause=True)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 500", output)
        self.assertIn("Kontrollera Spotify", output)
        self.assertNotIn("Larmet är avstängt", output)

    def test_cli_resume_failure_is_not_retried_or_replaced_with_reserve(self):
        code, _, state, sent, output = self.execute_alarm(
            [(1, "snooze")], ["--fallback", "RESERVE", "--snooze-minutes", "0.1"],
            fail_resume=True)
        self.assertEqual(code, 1)
        self.assertFalse(state["is_playing"])
        self.assertIn("HTTP 503", output)
        self.assertEqual(sum(method == "PUT" and "/play?" in url for _, method, url, _ in sent), 2)


if __name__ == "__main__":
    unittest.main()
