"""scripts/replay_set.py, offline: a stand-in for Explore-Compile-Replay, no phone."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import replay_set  # noqa: E402

TRACE = {"schema": "ecr.trace/0", "id": "anna-20261125T120000", "adapter": {"id": "mobile-maestro"},
         "params": [{"name": "TEXT_1", "secret": False}], "invariants": [],
         "steps": [{"id": "s1", "action": {"kind": "launchApp", "appId": "com.example.todo", "clearState": True}, "checkpoints": []},
                   {"id": "s2", "action": {"kind": "tapOn", "target": {"text": "Send Tab 2 of 3"}}, "checkpoints": []},
                   {"id": "s3", "action": {"kind": "tapOn", "target": {"point": "90%,15%"}}, "checkpoints": []},
                   {"id": "s4", "action": {"kind": "inputText", "text": {"param": "TEXT_1"}}, "checkpoints": []}]}
TYPED = "A text that must stay out of every public file"


WINDOW = "mCurrentFocus=Window{1a2b3c u0 com.example.todo/com.example.todo.MainActivity}\n  mFocusedApp=ActivityRecord{9f u0 com.example.todo/.MainActivity t12}\n"
LOG = ("11-25 20:00:01.000   500   520 I ActivityTaskManager: Displayed com.example.todo/.MainActivity for user 0: +1s534ms\n"
       f"11-25 20:00:02.000  1234  1234 I ReactNativeJS: {TYPED}\n")


class Tools:
    """Stands in for node, sg, git and adb: writes the report `ecr replay --report` would write."""

    def __init__(self, closed=True, outcomes=None, validates=True, writes=True, window=WINDOW, log=LOG, phone=True):
        self.calls, self.envs = [], []
        self.closed, self.outcomes, self.validates, self.writes = closed, outcomes or {}, validates, writes
        self.window, self.log, self.phone = window, log, phone

    def adb(self, args):
        if not self.phone:
            return 1, "adb: no devices/emulators found\n"
        answers = {"date": (0, "1764100800\n"), "logcat": (0, self.log), "dumpsys": (0, self.window), "pidof": (0, "1234\n"),
                   "cat": (0, "3.52 2.10 1.00 2/300 1234\n")}
        return answers[next(word for word in args[1:] if word in answers)]

    def __call__(self, args, cwd, env, timeout):
        self.calls.append(args)
        self.envs.append(env)
        if args[0] == "adb":
            return self.adb(args)
        if args[0] == "git":
            return 0, "5b4d9f2aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        inside = args[0] == "sg"
        if inside and "socket" in args[3]:
            return (0 if self.closed else 1), ""
        words = args[3].split() if inside else args
        if "validate" in words:
            return (0 if self.validates else 1), "ok\n"
        times = int(words[words.index("--times") + 1])
        outcome = self.outcomes.get("cut" if inside else "open", "pass")
        one = {"outcome": outcome, "durationMs": 60000,
               "divergences": [] if outcome == "pass" else [{"type": "target_not_found", "stepId": "s3"}]}
        if self.writes:
            Path(words[words.index("--report") + 1]).write_text(json.dumps({"schema": "ecr.replays/0", "traces": [{
                "id": TRACE["id"], "steps": 4, "oracles": 0, "recorded": "pass", "artifact": {"sha256": "a" * 64}, "replays": [one] * times}]}))
        return (0 if outcome == "pass" else 1), f"said about {TYPED}\n"


class ReplaySet(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.folder, self.out = self.dir / "traces", self.dir / "results"
        (self.folder / "anna").mkdir(parents=True)
        (self.folder / "anna" / "first.trace.json").write_text(json.dumps(TRACE), encoding="utf-8")
        (self.folder / "anna" / "first.params.json").write_text(json.dumps({"TEXT_1": TYPED}), encoding="utf-8")

    def run_it(self, tools, *more):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = replay_set.main([str(self.folder), "--persona", "anna", "--ecr", str(self.dir), "--out", str(self.out), *more], run=tools)
        self.assertEqual(code, 0)
        kept = json.loads((self.out / "replays.json").read_text()) if (self.out / "replays.json").exists() else None
        return printed.getvalue(), kept

    def test_every_replay_is_kept_and_the_last_one_runs_with_the_network_closed(self):
        tools = Tools()
        printed, kept = self.run_it(tools, "--times", "20", "--closed-group", "noinet")
        trace = kept["traces"][0]
        self.assertEqual([r["internet"] for r in trace["replays"]], ["open"] * 19 + ["cut"])
        self.assertEqual([r["index"] for r in trace["replays"]], list(range(1, 21)))
        self.assertEqual(trace["taps"], {"name": 1, "id": 0, "point": 1})
        self.assertTrue(trace["freshState"])
        self.assertEqual(kept["explore-compile-replay"], "5b4d9f2" + "a" * 37)
        self.assertFalse(kept["modelKeyPresent"])
        self.assertIn("anna-20261125T120000: 20 of 20 replays give what the trace recorded (20 pass), 1 with the network of the tools closed (pass), median 60 s, "
                      "4 steps, taps 1 by name, 0 by id, 1 by position", printed)

    def test_no_typed_text_and_no_secret_reaches_a_public_place(self):
        tools = Tools()
        import os
        os.environ.update(ANTHROPIC_API_KEY="k", REPLAY_PARAMS=json.dumps({"other": {"TEXT_1": "x"}}))
        try:
            printed, _ = self.run_it(tools, "--times", "2")
        finally:
            del os.environ["ANTHROPIC_API_KEY"], os.environ["REPLAY_PARAMS"]
        self.assertNotIn(TYPED, printed)
        self.assertNotIn(TYPED, (self.out / "replays.json").read_text())
        self.assertIn(TYPED, (self.out / "replays.txt").read_text())   # private, encrypted with the results
        replay_env = [env for args, env in zip(tools.calls, tools.envs) if "replay" in args][0]
        self.assertEqual(replay_env["ECR_PARAM_TEXT_1"], TYPED)
        self.assertNotIn("ANTHROPIC_API_KEY", replay_env)
        self.assertNotIn("REPLAY_PARAMS", replay_env)

    def test_the_secret_adds_typed_texts_to_the_file_and_wins(self):
        values = replay_set.typed_texts(self.folder / "anna" / "first.trace.json", TRACE["id"],
                                        {"REPLAY_PARAMS": json.dumps({TRACE["id"]: {"TEXT_1": "from the secret", "PASSWORD": "p"}})})
        self.assertEqual(values, {"TEXT_1": "from the secret", "PASSWORD": "p"})

    def test_typed_texts_can_come_from_the_secret_when_no_file_is_next_to_the_trace(self):
        (self.folder / "anna" / "first.params.json").unlink()
        values = replay_set.typed_texts(self.folder / "anna" / "first.trace.json", TRACE["id"], {"REPLAY_PARAMS": json.dumps({TRACE["id"]: {"TEXT_1": "abc"}})})
        self.assertEqual(values, {"TEXT_1": "abc"})
        self.assertEqual(replay_set.typed_texts(self.folder / "anna" / "first.trace.json", TRACE["id"], {"REPLAY_PARAMS": "not json"}), {})

    def test_a_network_that_cannot_be_closed_is_said_and_never_claimed(self):
        printed, kept = self.run_it(Tools(closed=False), "--times", "3", "--closed-group", "noinet")
        self.assertIn("the network could not be closed around the tools", printed)
        self.assertEqual([r["internet"] for r in kept["traces"][0]["replays"]], ["open"] * 3)
        self.assertIn("none with the network closed", printed)

    def test_a_replay_that_stops_says_where_in_words_of_the_format(self):
        printed, kept = self.run_it(Tools(outcomes={"open": "fail"}), "--times", "3", "--closed-group", "noinet")
        self.assertIn("1 of 3 replays give what the trace recorded (1 pass, 2 FAIL)", printed)
        self.assertIn("stops: target_not_found at step s3", printed)
        self.assertEqual(kept["traces"][0]["replays"][0]["divergences"], [{"type": "target_not_found", "stepId": "s3"}])

    def test_each_replay_that_does_not_give_what_was_recorded_says_what_the_phone_showed(self):
        tools = Tools(outcomes={"open": "fail"})
        printed, kept = self.run_it(tools, "--times", "3", "--closed-group", "noinet")
        self.assertIn("replays:   anna-20261125T120000 replay 1 of 3: FAIL, target_not_found at step s3, after 60 s; "
                      "on screen: the app, the app running; app start 1.5 s; nothing stopped responding; no crash; load 3.5 on the phone", printed)
        self.assertIn("replay 2 of 3: FAIL", printed)
        self.assertNotIn("replay 3 of 3", printed)   # the third gave what the trace recorded
        self.assertIn("app start median 1.5 s, slowest 1.5 s", printed)
        replays = kept["traces"][0]["replays"]
        self.assertEqual(replays[0]["device"]["inFront"], "the app")
        self.assertEqual(replays[2]["device"], {"appStartSeconds": [1.5]})   # as recorded: only the start time is read
        self.assertIn(["adb", "logcat", "-d", "-T", "1764100800.000"], tools.calls)
        public = printed + (self.out / "replays.json").read_text()
        for private in (TYPED, "MainActivity", "ReactNativeJS"):
            self.assertNotIn(private, public)

    def test_a_phone_that_does_not_answer_never_stops_the_replays(self):
        printed, kept = self.run_it(Tools(outcomes={"open": "fail"}, phone=False), "--times", "2")
        self.assertIn("replay 1 of 2: FAIL, target_not_found at step s3, after 60 s; the phone did not answer", printed)
        self.assertEqual(kept["traces"][0]["status"], "replayed")
        self.assertNotIn("app start median", printed)

    def test_the_kind_of_window_in_front_is_said_never_its_title(self):
        front = replay_set.device_state.in_front
        self.assertEqual(front("mCurrentFocus=Window{4d u0 Application Not Responding: com.android.systemui}", "com.example.todo"),
                         "a window that does not respond, of the system")
        self.assertEqual(front("mCurrentFocus=Window{4d u0 Application Not Responding: com.example.todo}", "com.example.todo"),
                         "a window that does not respond, of the app")
        self.assertEqual(front("mCurrentFocus=Window{4d u0 com.google.android.apps.nexuslauncher/.NexusLauncherActivity}", "com.example.todo"), "the home screen")
        self.assertEqual(front("mCurrentFocus=null", "com.example.todo"), "no window")
        self.assertEqual(front("mCurrentFocus=Window{4d u0 Delete the file of Bob?}", "com.example.todo"), "another window of the system")

    def test_start_times_of_the_app_are_read_from_the_log_of_android_11_and_14(self):
        starts = replay_set.device_state.app_starts
        log = ("I ActivityTaskManager: Displayed com.example.todo/.MainActivity: +812ms\n"
               "I ActivityTaskManager: Displayed com.example.todo/.MainActivity for user 0: +24s310ms\n"
               "I ActivityTaskManager: Displayed com.other.app/.Main: +1s2ms\n")
        self.assertEqual(starts(log, "com.example.todo"), [0.8, 24.3])

    def test_not_responding_and_crashes_are_counted_for_the_app_and_the_system(self):
        log = ("E ActivityManager: ANR in com.android.systemui\nE ActivityManager: ANR in com.example.todo:remote\n"
               "E AndroidRuntime: FATAL EXCEPTION: main\nE AndroidRuntime: Process: com.example.todo, PID: 4321\n"
               "E AndroidRuntime: Process: com.other.app, PID: 99\n")
        facts = replay_set.device_state.after_replay(Tools(log=log), {}, "1764100800", "com.example.todo", look=True)
        self.assertEqual(facts["notResponding"], ["the app", "the system"])
        self.assertEqual(facts["appCrashes"], 1)
        self.assertIn("not responding: the app and the system; 1 crash of the app", replay_set.device_state.words(facts))

    def test_a_trace_the_tools_refuse_or_cannot_run_never_fails_the_run(self):
        printed, kept = self.run_it(Tools(validates=False))
        self.assertIn("does not accept the trace", printed)
        self.assertEqual(kept["traces"][0]["status"], "invalid")
        printed, kept = self.run_it(Tools(writes=False))
        self.assertIn("could not run", printed)
        (self.folder / "anna" / "first.trace.json").write_text("not json")
        printed, kept = self.run_it(Tools())
        self.assertEqual(kept["traces"][0]["status"], "invalid")

    def test_a_persona_with_no_trace_replays_nothing(self):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            replay_set.main([str(self.folder), "--persona", "malik", "--ecr", str(self.dir), "--out", str(self.out)], run=Tools())
        self.assertIn("no trace for malik", printed.getvalue())
        self.assertFalse((self.out / "replays.json").exists())


if __name__ == "__main__":
    unittest.main()
