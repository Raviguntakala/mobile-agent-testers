"""What the phone showed and did around one replay, in words that hold no text of the app.

replay_set.py asks after every replay how long the app took to show its first screen. When a
replay does not give what its trace recorded, it also asks what kind of window was in front,
whether the app was still running, whether the system saw a window that does not respond or a
crash of the app, and how busy the phone and the runner were. A protocol that counts unstable
replays must give their cause: these are causes, with no text of the app. The system log and the
list of windows are read here and never copied.
"""
import os
import re

FOCUS = re.compile(r"mCurrentFocus=Window\{\S+ \S+ ([^}\n]*)\}")
DISPLAYED = re.compile(r"Displayed ([\w.]+)/\S*?(?: for user \d+)?: \+(?:(\d+)s)?(\d+)ms")
NOT_RESPONDING = re.compile(r"ANR in ([\w.:]+)")
CRASHED = re.compile(r"Process: ([\w.:]+), PID: \d+")
APP_ID = re.compile(r"[A-Za-z0-9._]{1,200}")


def phone_time(run, env):
    """Seconds since 1970 on the phone: the log is read from there after the replay. None when the phone does not answer."""
    code, out = run(["adb", "shell", "date", "+%s"], None, env, 20)
    found = re.search(r"\b\d{9,11}\b", out or "") if code == 0 else None
    return found.group(0) if found else None


def app_starts(log, app_id):
    """Seconds the app took to show its first screen, once for each start the system logged."""
    return [round(int(m.group(2) or 0) + int(m.group(3)) / 1000, 1) for m in DISPLAYED.finditer(log or "") if m.group(1) == app_id]


def in_front(windows, app_id):
    """The kind of window in front, never its title."""
    found = FOCUS.search(windows or "")
    title = found.group(1).strip() if found else ""
    if title in ("", "null"):
        return "no window"
    if "Not Responding" in title:
        return "a window that does not respond, of the app" if app_id in title else "a window that does not respond, of the system"
    if app_id in title:
        return "the app"
    if "launcher" in title.lower():
        return "the home screen"
    if "InputMethod" in title:
        return "the keyboard"
    return "another window of the system"


def load(run, env):
    """(phone, runner): the load average over one minute, or None where it could not be read."""
    code, out = run(["adb", "shell", "cat", "/proc/loadavg"], None, env, 20)
    phone = re.match(r"\s*(\d+(?:\.\d+)?)\s", out or "") if code == 0 else None
    try:
        runner = round(os.getloadavg()[0], 1)
    except (OSError, AttributeError):
        runner = None
    return (round(float(phone.group(1)), 1) if phone else None), runner


def after_replay(run, env, since, app_id, look):
    """Facts about the replay that started at `since`. With look=False, only the start times of the app."""
    if not since or not app_id or not APP_ID.fullmatch(app_id):
        return {}
    code, log = run(["adb", "logcat", "-d", "-T", f"{since}.000"], None, env, 60)
    log = log if code == 0 else ""
    facts = {"appStartSeconds": app_starts(log, app_id)}
    if not look:
        return facts
    code, windows = run(["adb", "shell", "dumpsys", "window"], None, env, 30)
    running, _ = run(["adb", "shell", "pidof", app_id], None, env, 20)
    phone, runner = load(run, env)
    facts.update(inFront=in_front(windows if code == 0 else "", app_id), appRunning=running == 0,
                 notResponding=sorted({"the app" if name.split(":")[0] == app_id else "the system" for name in NOT_RESPONDING.findall(log)}),
                 appCrashes=sum(1 for name in CRASHED.findall(log) if name.split(":")[0] == app_id),
                 load={"phone": phone, "runner": runner})
    return facts


def words(facts):
    """The facts of one replay as public words."""
    if not facts:
        return "the phone did not answer"
    starts = facts.get("appStartSeconds") or []
    said = []
    if "inFront" in facts:
        said.append(f"on screen: {facts['inFront']}, the app {'running' if facts.get('appRunning') else 'not running'}")
    said.append(f"app start {', '.join(f'{s:g} s' for s in starts)}" if starts else "no start of the app in the log")
    if "notResponding" in facts:
        said.append(f"not responding: {' and '.join(facts['notResponding'])}" if facts["notResponding"] else "nothing stopped responding")
        crashes = facts.get("appCrashes", 0)
        said.append(f"{crashes} crash{'es' if crashes > 1 else ''} of the app" if crashes else "no crash")
    busy = facts.get("load") or {}
    if busy.get("phone") is not None or busy.get("runner") is not None:
        said.append("load " + ", ".join(f"{value:g} on the {where}" for where, value in busy.items() if value is not None))
    return "; ".join(said)
