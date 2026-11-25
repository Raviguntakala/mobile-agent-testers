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


class Tools:
    """Stands in for node, sg, git: writes the report `ecr replay --report` would write."""

    def __init__(self, closed=True, outcomes=None, validates=True, writes=True):
        self.calls, self.envs = [], []
        self.closed, self.outcomes, self.validates, self.writes = closed, outcomes or {}, validates, writes

    def __call__(self, args, cwd, env, timeout):
        self.calls.append(args)
        self.envs.append(env)
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
