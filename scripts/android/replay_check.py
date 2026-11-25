"""A tester with no model, to check on a real phone that a path written as a trace replays.

Usage: python3 scripts/android/replay_check.py <results dir> --ecr <checkout of explore-compile-replay>

Follows a fixed path on Tiny Todo (demo-app) with the tools an agent uses: screen.py to
look, adb to tap (through scripts/android/shim/adb, which notes each tap), type.py to type.
Then scripts/ecr_trace.py writes the trace and scripts/ecr_replay.py replays it. Exits 1
when the trace is not the one expected or when the replay does not pass.

Everything is printed: the demo app holds no data and the account is made up.
Run by .github/workflows/replay-check.yml, with no model and no secret.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
APP_ID = "com.example.tinytodo"
# (what to do, the name on the screen or the text to type, which one when several share the name)
STEPS = [
    ("tap", "Name", 0), ("type", "Anna", 0),
    ("tap", "Email", 0), ("type", "anna@example.com", 0),
    ("tap", "Password", 0), ("type", "only-for-this-test", 0),
    ("hide", "", 0),                     # the keyboard, with Back, as an agent does
    ("tap", "Continue", 0),              # a button and its label share this name
    ("tap", "New task", 0), ("type", "Buy milk", 0),
    ("tap", "Add", 0),
    ("hide", "", 0),
    ("tap", "x", 1),                     # four tasks, four "x": the second one deletes "Call the plumber"
    ("tap", "Buy apples", 0),            # the three tasks that must be left: a replay that deleted another one stops here
    ("tap", "Pay the rent", 0),
    ("tap", "Book the dentist", 0),
    ("tap", "Buy milk", 0),              # a text that was typed: stored under its name, not as text
]
SEEN = re.compile(r'^- (\w+) "(.*)" centre=\((\d+),(\d+)\)$')


def run(*args):
    done = subprocess.run(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", check=False)
    return done.returncode, done.stdout or ""


def look(results, number):
    """What screen.py shows an agent: [(name, x, y)] in the order it prints them."""
    _, printed = run(sys.executable, "scripts/android/screen.py", str(results), f"{number:02d}")
    return [(found.group(2), int(found.group(3)), int(found.group(4))) for found in map(SEEN.match, printed.splitlines()) if found]


def follow(results):
    screens = 0
    for kind, value, which in STEPS:
        if kind == "type":
            run(sys.executable, "scripts/android/type.py", value)
            continue
        if kind == "hide":
            if "mInputShown=true" in run("adb", "shell", "dumpsys", "input_method")[1]:
                run("adb", "shell", "input", "keyevent", "KEYCODE_BACK")
            continue
        for attempt in (1, 2):
            screens += 1
            named = [(x, y) for name, x, y in look(results, screens) if name == value]
            if len(named) > which:
                break
            if attempt == 1:   # the keyboard may be over it: an agent presses Back, so does this
                run("adb", "shell", "input", "keyevent", "KEYCODE_BACK")
        else:
            sys.exit(f'check: "{value}" is not on screen {screens:02d}')
        run("adb", "shell", "input", "tap", str(named[which][0]), str(named[which][1]))
    left = [name for name, _, _ in look(results, screens + 1)]
    if "Call the plumber" in left or "Buy milk" not in left:
        sys.exit("check: the path did not do what was planned (a task added, another one deleted)")


def taps(trace, **target):
    return [step for step in trace["steps"] if step["action"].get("target") == target]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("results")
    parser.add_argument("--ecr", required=True)
    args = parser.parse_args(argv)
    results = Path(args.results)
    results.mkdir(parents=True, exist_ok=True)

    follow(results)
    code, printed = run(sys.executable, "scripts/ecr_trace.py", str(results), "--persona", "anna", "--app-id", APP_ID)
    print(printed.strip())
    if code != 0 or not (results / "trace.json").exists():
        sys.exit("check: no trace was written")
    trace = json.loads((results / "trace.json").read_text(encoding="utf-8"))
    for step in trace["steps"]:
        print(f"  {step['id']}: {json.dumps(step['action'], ensure_ascii=False)}")

    problems = []
    for kind, wanted in (("inputText", "type"), ("tapOn", "tap")):
        planned = sum(1 for step, _, _ in STEPS if step == wanted)
        if sum(1 for step in trace["steps"] if step["action"]["kind"] == kind) != planned:
            problems.append(f"the trace does not have {planned} {kind} steps")
    if not taps(trace, text="Continue"):
        problems.append('the tap on "Continue" is not stored by its name alone')
    if not taps(trace, text="x", index=1):
        problems.append('the tap on the second "x" is not stored as the second one')
    if not [step for step in trace["steps"] if isinstance(step["action"].get("target", {}).get("text"), dict)]:
        problems.append("the tap on the typed text is not stored under its name")
    if "Buy milk" in json.dumps(trace) or "only-for-this-test" in json.dumps(trace):
        problems.append("a typed text is in the trace")

    code, printed = run(sys.executable, "scripts/ecr_replay.py", str(results), "--ecr", str(Path(args.ecr).resolve()))
    print(printed.strip())
    try:
        result = json.loads((results / "replay.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = {"status": "no result"}
    if result.get("status") != "pass":
        problems.append(f"the replay did not pass: {json.dumps(result)}")
        try:
            print((results / "replay.txt").read_text(encoding="utf-8")[-6000:])
        except OSError:
            pass
    for problem in problems:
        print(f"check: {problem}")
    print("check: FAILED" if problems else f"check: ok, {len(trace['steps'])} steps written from the taps and replayed with no model")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
