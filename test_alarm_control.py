"""Beteendetester med simulerad tid; ingen musik eller nätverkstrafik."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import alarm_control as alarm


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Player:
    def __init__(self, clock):
        self.clock = clock
        self.events = []
        self.playing = False
        self.pause_error = None
        self.resume_error = None

    def start(self):
        self.playing = True
        self.events.append((self.clock.now, "start"))

    def pause(self):
        self.events.append((self.clock.now, "pause"))
        if self.pause_error:
            raise self.pause_error
        self.playing = False

    def resume(self):
        self.events.append((self.clock.now, "resume"))
        if self.resume_error:
            raise self.resume_error
        self.playing = True

    def check(self):
        if not self.playing:
            raise RuntimeError("Ljudkällan är inte igång.")


class AlarmLoopTests(unittest.TestCase):
    def run_alarm(self, commands=(), delay=0, auto_stop=30, snooze=9, player=None):
        clock = player.clock if player else Clock()
        player = player or Player(clock)
        pending = list(commands)

        def read_command(timeout):
            if pending and pending[0][0] <= clock.now + timeout:
                when, command = pending.pop(0)
                clock.now = max(clock.now, when)
                if isinstance(command, BaseException):
                    raise command
                return command
            clock.sleep(timeout)
            return None

        output = io.StringIO()
        with patch.object(alarm.time, "monotonic", clock.monotonic), \
                redirect_stdout(output), redirect_stderr(output):
            code = alarm.run_alarm(player, read_command, delay=delay,
                                   auto_stop_seconds=auto_stop, snooze_seconds=snooze)
        return code, clock, player, output.getvalue()

    def test_stop_before_start_does_not_touch_any_player(self):
        code, clock, player, _ = self.run_alarm([(2, "stop")], delay=10)
        self.assertEqual(code, 0)
        self.assertEqual(player.events, [])
        self.assertEqual(clock.now, 2)

    def test_eof_or_ctrl_c_before_start_does_not_touch_any_player(self):
        for command in (EOFError(), KeyboardInterrupt()):
            with self.subTest(command=type(command).__name__):
                code, clock, player, _ = self.run_alarm([(2, command)], delay=10)
                self.assertIn(code, (0, 130))
                self.assertEqual(player.events, [])
                self.assertEqual(clock.now, 2)

    def test_stop_pauses_playing_alarm(self):
        code, _, player, _ = self.run_alarm([(2, "stop")])
        self.assertEqual(code, 0)
        self.assertFalse(player.playing)
        self.assertEqual(player.events[-1], (2, "pause"))

    def test_snooze_resumes_then_auto_stop_keeps_original_deadline(self):
        code, clock, player, _ = self.run_alarm([(2, "snooze")])
        self.assertEqual(code, 0)
        self.assertIn((2, "pause"), player.events)
        self.assertIn((11, "resume"), player.events)
        self.assertEqual(clock.now, 30)
        self.assertFalse(player.playing)

    def test_stop_during_snooze_prevents_all_later_restarts(self):
        code, clock, player, _ = self.run_alarm([(2, "snooze"), (4, "stop")])
        self.assertEqual(code, 0)
        self.assertEqual(clock.now, 4)
        self.assertNotIn("resume", [event for _, event in player.events])
        self.assertFalse(player.playing)

    def test_auto_stop_wins_when_snooze_expires_at_the_same_time(self):
        code, clock, player, _ = self.run_alarm([(21, "snooze")])
        self.assertEqual(code, 0)
        self.assertEqual(clock.now, 30)
        self.assertNotIn("resume", [event for _, event in player.events])

    def test_repeated_snooze_never_extends_auto_stop(self):
        code, clock, player, _ = self.run_alarm([(2, "snooze"), (10, "snooze"),
                                                (18, "snooze"), (26, "snooze")])
        self.assertEqual(code, 0)
        self.assertEqual(clock.now, 30)
        self.assertNotIn("resume", [event for _, event in player.events])

    def test_snooze_before_start_and_unknown_commands_do_not_change_timers(self):
        code, clock, player, output = self.run_alarm([(2, "snooze"), (3, "unknown")], delay=10)
        self.assertEqual(code, 0)
        self.assertEqual(player.events[0], (10, "start"))
        self.assertEqual(clock.now, 40)
        self.assertIn("inte startat", output)

    def test_eof_and_ctrl_c_stop_playback_or_cancel_snooze(self):
        for command in (EOFError(), KeyboardInterrupt()):
            for snoozing in (False, True):
                with self.subTest(command=type(command).__name__, snoozing=snoozing):
                    commands = [(1, "snooze")] if snoozing else []
                    commands.append((2, command))
                    code, clock, player, _ = self.run_alarm(commands)
                    self.assertIn(code, (0, 130))
                    self.assertEqual(clock.now, 2)
                    self.assertFalse(player.playing)
                    self.assertNotIn("resume", [event for _, event in player.events])

    def test_failed_pause_never_schedules_a_resume_or_claims_success(self):
        clock = Clock()
        player = Player(clock)
        player.pause_error = RuntimeError("SECRET_TOKEN")
        code, _, player, output = self.run_alarm([(2, "snooze")], player=player)
        self.assertEqual(code, 1)
        self.assertNotIn("resume", [event for _, event in player.events])
        self.assertNotIn("SECRET_TOKEN", output)
        self.assertNotIn("Larmet är avstängt", output)
        self.assertIn("Kontrollera Spotify", output)

    def test_failed_resume_is_not_retried_and_attempts_to_silence_player(self):
        clock = Clock()
        player = Player(clock)
        player.resume_error = RuntimeError("SECRET_TOKEN")
        code, _, player, output = self.run_alarm([(2, "snooze")], player=player)
        self.assertEqual(code, 1)
        self.assertEqual([event for _, event in player.events].count("resume"), 1)
        self.assertFalse(player.playing)
        self.assertNotIn("SECRET_TOKEN", output)


class SpotifyFake:
    def __init__(self):
        self.commands = []
        self.state = {"device": {"id": "chrome"}, "is_playing": False,
                      "context": {"uri": "spotify:playlist:test"}}

    def start_playback(self, **kwargs):
        self.commands.append(("start", kwargs))
        self.state["is_playing"] = True

    def pause_playback(self, **kwargs):
        self.commands.append(("pause", kwargs))
        self.state["is_playing"] = False

    def current_playback(self):
        return self.state


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.sp = SpotifyFake()
        self.output = io.StringIO()
        for context in (patch.object(alarm.time, "monotonic", self.clock.monotonic),
                        patch.object(alarm.time, "sleep", self.clock.sleep),
                        redirect_stdout(self.output), redirect_stderr(self.output)):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def spotify_player(self):
        return alarm.SpotifyAlarmPlayer(self.sp, "chrome", "spotify:playlist:test")

    def test_spotify_stop_and_resume_control_target_without_restarting_playlist(self):
        player = self.spotify_player()
        player.start()
        player.pause()
        self.assertFalse(self.sp.state["is_playing"])
        player.resume()
        self.assertTrue(self.sp.state["is_playing"])
        self.assertEqual(self.sp.commands, [
            ("start", {"device_id": "chrome", "context_uri": "spotify:playlist:test"}),
            ("pause", {"device_id": "chrome"}),
            ("start", {"device_id": "chrome"}),
        ])

    def test_stop_before_spotify_start_is_a_noop(self):
        self.spotify_player().pause()
        self.assertEqual(self.sp.commands, [])

    def test_pause_is_idempotent_for_confirmed_paused_player(self):
        player = self.spotify_player()
        player.start()
        player.pause()
        player.pause()
        self.assertEqual([cmd for cmd, _ in self.sp.commands].count("pause"), 1)

    def test_missing_wrong_or_malformed_state_never_confirms_pause(self):
        for state in (None, {}, {"device": {"id": "other"}, "is_playing": False},
                      {"device": {"id": "chrome"}, "is_playing": "false"}):
            with self.subTest(state=state):
                self.sp = SpotifyFake()
                player = self.spotify_player()
                player.start()
                self.sp.pause_playback = lambda **kwargs: None
                self.sp.state = state
                started = self.clock.now
                with self.assertRaises(RuntimeError):
                    player.pause()
                self.assertLessEqual(self.clock.now - started, 5)

    def test_read_error_does_not_prevent_attempt_to_send_pause(self):
        player = self.spotify_player()
        player.start()
        real_read = self.sp.current_playback
        self.sp.current_playback = Mock(side_effect=[TimeoutError("SECRET"), real_read()])
        player.pause()
        self.assertFalse(self.sp.state["is_playing"])
        self.assertEqual(self.sp.commands[-1], ("pause", {"device_id": "chrome"}))

    def test_mp3_snooze_releases_process_and_resume_starts_new_owned_process(self):
        first, second = Mock(), Mock()
        for process in (first, second):
            process.poll.return_value = None
        with patch.object(alarm.subprocess, "Popen", side_effect=[first, second]) as popen:
            player = alarm.Mp3AlarmPlayer(Path("/tmp/reserv.mp3"))
            player.start()
            player.pause()
            first.terminate.assert_called_once()
            first.wait.assert_called_once()
            player.resume()
            player.pause()
            second.terminate.assert_called_once()
            self.assertEqual(popen.call_count, 2)
            options = popen.call_args.kwargs
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            self.assertTrue(options["start_new_session"])
            self.assertIn("--input-terminal=no", popen.call_args.args[0])

    def test_mp3_that_does_not_terminate_is_killed_and_reaped(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("mpv", 5), 0]
        with patch.object(alarm.subprocess, "Popen", return_value=process):
            player = alarm.Mp3AlarmPlayer(Path("/tmp/reserv.mp3"))
            player.start()
            player.pause()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_mp3_immediate_exit_is_a_start_failure(self):
        process = Mock()
        process.poll.return_value = 2
        with patch.object(alarm.subprocess, "Popen", return_value=process):
            with self.assertRaises(RuntimeError):
                alarm.Mp3AlarmPlayer(Path("/tmp/reserv.mp3")).start()

    def test_mp3_exit_while_ringing_is_reported_and_not_restarted(self):
        process = Mock()
        process.poll.side_effect = lambda: 2 if self.clock.now >= 2 else None
        factory = Mock(side_effect=RuntimeError("SECRET_TOKEN"))

        def read_command(timeout):
            self.clock.sleep(timeout)
            return None

        with patch.object(alarm.subprocess, "Popen", return_value=process) as popen:
            player = alarm.AlarmPlayback(factory, Path("/tmp/reserv.mp3"))
            code = alarm.run_alarm(player, read_command, auto_stop_seconds=30)
        self.assertEqual(code, 1)
        self.assertEqual(self.clock.now, 2)
        popen.assert_called_once()
        self.assertIn("Larmstyrningen misslyckades", self.output.getvalue())
        self.assertNotIn("SECRET_TOKEN", self.output.getvalue())

    def test_failed_mp3_restart_after_snooze_is_reported(self):
        first, second = Mock(), Mock()
        first.poll.return_value = None
        second.poll.return_value = 2
        factory = Mock(side_effect=RuntimeError("SECRET_TOKEN"))
        commands = iter(["snooze"])

        def read_command(timeout):
            self.clock.sleep(timeout)
            return next(commands, None)

        with patch.object(alarm.subprocess, "Popen", side_effect=[first, second]) as popen:
            player = alarm.AlarmPlayback(factory, Path("/tmp/reserv.mp3"))
            code = alarm.run_alarm(player, read_command, snooze_seconds=2, auto_stop_seconds=30)
        self.assertEqual(code, 1)
        self.assertEqual(popen.call_count, 2)
        self.assertIn("Larmstyrningen misslyckades", self.output.getvalue())

    def test_owned_real_child_process_is_stopped_and_can_be_restarted_without_audio(self):
        real_popen = subprocess.Popen
        children = []

        def silent_process(command, **kwargs):
            process = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            children.append(process)
            return process

        def cleanup():
            for process in children:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

        self.addCleanup(cleanup)
        with patch.object(alarm.subprocess, "Popen", side_effect=silent_process):
            player = alarm.Mp3AlarmPlayer(Path("/tmp/reserv.mp3"))
            player.start()
            player.pause()
            self.assertIsNotNone(children[0].poll())
            player.resume()
            player.pause()
            self.assertIsNotNone(children[1].poll())
        self.assertNotEqual(children[0].pid, children[1].pid)

    def test_start_failure_selects_mp3_and_stop_does_not_restart_it(self):
        spotify, mp3 = Mock(), Mock()
        spotify.start.side_effect = RuntimeError("SECRET_TOKEN")
        with patch.object(alarm, "Mp3AlarmPlayer", return_value=mp3):
            player = alarm.AlarmPlayback(lambda: spotify, Path("/tmp/reserv.mp3"))
            player.start()
            player.pause()
        spotify.pause.assert_called()
        mp3.start.assert_called_once()
        mp3.pause.assert_called_once()
        self.assertNotIn("SECRET_TOKEN", self.output.getvalue())

    def test_unconfirmed_spotify_cleanup_does_not_leave_mp3_running_on_stop(self):
        spotify, mp3 = Mock(), Mock()
        spotify.start.side_effect = RuntimeError("SECRET_TOKEN")
        spotify.pause.side_effect = RuntimeError("SECRET_TOKEN")
        with patch.object(alarm, "Mp3AlarmPlayer", return_value=mp3):
            player = alarm.AlarmPlayback(lambda: spotify, Path("/tmp/reserv.mp3"))
            player.start()
            with self.assertRaises(RuntimeError):
                player.pause()
        mp3.pause.assert_called_once()
        self.assertNotIn("SECRET_TOKEN", self.output.getvalue())

    def test_ctrl_c_during_start_still_pauses_partially_started_spotify(self):
        real_start = self.sp.start_playback

        def interrupted_start(**kwargs):
            real_start(**kwargs)
            raise KeyboardInterrupt

        self.sp.start_playback = interrupted_start
        player = alarm.AlarmPlayback(self.spotify_player)
        code = alarm.run_alarm(player, lambda timeout: None)
        self.assertEqual(code, 130)
        self.assertFalse(self.sp.state["is_playing"])


class TerminalTests(unittest.TestCase):
    def test_real_input_timeout_allows_automatic_stop_with_open_stdin(self):
        reader, writer = os.pipe()
        self.addCleanup(os.close, writer)
        player, output = Player(Clock()), io.StringIO()
        with os.fdopen(reader) as stream, redirect_stdout(output):
            code = alarm.run_alarm(player, alarm.TerminalCommands(stream).read,
                                   auto_stop_seconds=0.02)
        self.assertEqual(code, 0)
        self.assertFalse(player.playing)
        self.assertIn("Automatisk stopptid nådd", output.getvalue())

    def test_real_buffered_snooze_and_stop_control_the_loop(self):
        reader, writer = os.pipe()
        os.write(writer, b"snooze\nstop\n")
        os.close(writer)
        player, output = Player(Clock()), io.StringIO()
        with os.fdopen(reader) as stream, redirect_stdout(output):
            code = alarm.run_alarm(player, alarm.TerminalCommands(stream).read)
        self.assertEqual(code, 0)
        self.assertFalse(player.playing)
        self.assertIn("Snooze i 9 minuter", output.getvalue())
        self.assertNotIn("resume", [event for _, event in player.events])

    def test_buffered_lines_and_final_partial_line_are_not_lost(self):
        reader, writer = os.pipe()
        with os.fdopen(reader) as stream:
            commands = alarm.TerminalCommands(stream)
            os.write(writer, b"snooze\nstop\nstop")
            os.close(writer)
            self.assertEqual(commands.read(0.01), "snooze")
            self.assertEqual(commands.read(0.01), "stop")
            self.assertEqual(commands.read(0.01), "stop")
            with self.assertRaises(EOFError):
                commands.read(0.01)

    def test_incomplete_line_times_out_without_blocking_timers(self):
        reader, writer = os.pipe()
        self.addCleanup(os.close, writer)
        with os.fdopen(reader) as stream:
            commands = alarm.TerminalCommands(stream)
            os.write(writer, b"sno")
            self.assertIsNone(commands.read(0.01))
            os.write(writer, b"oze\n")
            self.assertEqual(commands.read(0.01), "snooze")


if __name__ == "__main__":
    unittest.main()
