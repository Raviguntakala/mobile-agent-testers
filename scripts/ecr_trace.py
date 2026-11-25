"""Turns what an agent did on the phone into a trace that can be replayed with no model.

Usage: python3 scripts/ecr_trace.py <results dir of one persona> --persona ID --app-id ID
           [--personas personas.json] [--log actions.tsv] [--agent-output agent-output.json]
           [--now 2026-01-01T00:00:00Z]

Reads the notes taken during the run (scripts/android/shim/adb, screen.py, type.py)
and the screen trees screen.py saved, and writes in the results dir:
- trace.json: the path, in the Explore-Compile-Replay format
  (https://github.com/pierridotite/explore-compile-replay). A tap is stored with the
  name of the element that was under it, not with its coordinates, so the path
  still replays when the layout moves.
  Each step also says what was seen (`evidence`: the screenshot before the action,
  where the finger landed, the screen size), for tools that show the path.
- trace.params.json: every text the agent typed. The trace holds only their names
  (TEXT_1, TEXT_2...), so it can be committed. This file must not be.

The trace is a path, with no checks yet: replayed, it fails when a step no longer
finds its target. Android only for now.
"""
import argparse
import datetime
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

KEYS = {"KEYCODE_BACK": "Back", "4": "Back", "KEYCODE_ENTER": "Enter", "66": "Enter",
        "KEYCODE_HOME": "Home", "3": "Home", "KEYCODE_DEL": "Backspace", "67": "Backspace"}


def read_screen(path):
    """Elements of a saved screen tree, in document order. Each one is a dict:
    label (its text, else its description), names (every text Maestro can match it by),
    id, bounds [x1, y1, x2, y2] and parent (its place in the list, None at the top)."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    nodes = []
    todo = [(child, None) for child in reversed(list(root))]
    while todo:   # no recursion: a screen tree can be deep
        element, parent = todo.pop()
        here = parent
        bounds = re.findall(r"-?\d+", element.get("bounds", ""))
        if element.tag == "node" and len(bounds) == 4:
            names = [value for value in (element.get(key) or "" for key in ("text", "content-desc", "hint")) if value.strip()]
            nodes.append({"label": names[0] if names else "", "names": names, "id": (element.get("resource-id") or "").strip(),
                          "bounds": [int(b) for b in bounds], "parent": parent})
            here = len(nodes) - 1
        todo.extend((child, here) for child in reversed(list(element)))
    return nodes


def shows(label, typed):
    """True when a name on the screen holds a text the agent typed (its e-mail in a greeting)."""
    return any(len(text) >= 3 and text in label for text in typed)


def found_by(nodes, key, name):
    """The places of the elements a replay finds for this name or id, in the order it counts them.
    Maestro keeps the deepest elements that match (a button and the text inside it share a name:
    it keeps the text), then counts them from the top of the screen, then from the left."""
    if key == "text":
        found = [i for i, node in enumerate(nodes) if name in node["names"]]
    else:
        found = [i for i, node in enumerate(nodes) if node["id"] == name or node["id"].rsplit("/", 1)[-1] == name]
    above = set()
    for i in found:
        parent = nodes[i]["parent"]
        while parent is not None and parent not in above:
            above.add(parent)
            parent = nodes[parent]["parent"]
    return sorted((i for i in found if i not in above), key=lambda i: (nodes[i]["bounds"][1], nodes[i]["bounds"][0]))


def inside(nodes, i, ancestor):
    while i is not None and i != ancestor:
        i = nodes[i]["parent"]
    return i is not None


def target_at(nodes, x, y, width, height, typed=None):
    """How a replay should find what was tapped: by name, else by id, else by position.
    Returns the target and the bounds of the element it names (None for a position).
    typed: {text the agent typed: its name}. A name on the screen that is exactly a typed text
    (a task it just added) is stored as that name; one that holds a typed text is not used.
    So typed texts stay out of the trace."""
    typed = typed or {}
    under = lambda i: nodes[i]["bounds"][0] <= x <= nodes[i]["bounds"][2] and nodes[i]["bounds"][1] <= y <= nodes[i]["bounds"][3]  # noqa: E731
    area = lambda i: (nodes[i]["bounds"][2] - nodes[i]["bounds"][0]) * (nodes[i]["bounds"][3] - nodes[i]["bounds"][1])  # noqa: E731
    hits = sorted((i for i in range(len(nodes)) if under(i) and (nodes[i]["label"] or nodes[i]["id"])), key=area)   # the smallest first
    for key in ("text", "id"):   # a name first: it survives a refactoring better than an id
        for hit in hits:
            name = nodes[hit]["label" if key == "text" else "id"]
            if not name or "${" in name or len(name) > 80 or "\n" in name or name != name.strip():
                continue
            if key == "text" and name in typed:
                value = {"param": typed[name]}
            elif shows(name, typed):
                continue
            else:
                value = name
            found = found_by(nodes, key, name)
            # The element the replay will act on: the one tapped, or the deepest one with that name inside it
            chosen = [i for i in found if inside(nodes, i, hit)]
            chosen = [i for i in chosen if under(i)] or chosen
            if not chosen:
                continue
            target = {key: value}
            if len(found) > 1:
                target["index"] = found.index(chosen[0])
            return target, list(nodes[chosen[0]]["bounds"])
    if width > 0 and height > 0:
        return {"point": f"{min(100, max(0, round(100 * x / width)))}%,{min(100, max(0, round(100 * y / height)))}%"}, None
    return None, None


def input_args(fields):
    """['shell', 'input', 'tap', '1', '2'] or ['shell', 'input tap 1 2'] -> ['tap', '1', '2']"""
    words = " ".join(fields[1:]).split()
    return words[1:] if words[:1] == ["input"] else []


def model_of(path):
    """The model that did most of the run, read from what `claude -p --output-format json` printed. None when unknown."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if isinstance(data, list):   # some versions print every message: the result comes last
        data = next((m for m in reversed(data) if isinstance(m, dict) and m.get("type") == "result"), None)
    usage = data.get("modelUsage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None
    written = {name: (use.get("outputTokens") or 0) if isinstance(use, dict) else 0 for name, use in usage.items()}
    names = [name for name in written if isinstance(name, str) and name and isinstance(written[name], (int, float))]
    return max(names, key=lambda name: written[name]) if names else None


def build(log_lines, persona, app_id, now, model=None):
    steps, params, values, skipped = [], [], {}, []
    nodes, width, height, screen, last_screen = [], 0, 0, None, None

    def add(action, **seen):
        """seen: what a tool needs to show the step (screenshot, finger, screen size). Never used to replay."""
        step = {"id": f"s{len(steps) + 1}", "action": action, "checkpoints": []}
        evidence = {key: value for key, value in dict(screen=screen, **seen).items() if value is not None}
        if width > 0 and height > 0 and ("point" in evidence or "from" in evidence):
            evidence["size"] = [width, height]
        if evidence:
            step["evidence"] = evidence
        steps.append(step)
        nonlocal last_screen
        last_screen = None   # known again at the next screenshot

    add({"kind": "launchApp", "appId": app_id, "clearState": True})
    for line in log_lines:
        fields = line.rstrip("\n").split("\t")
        if len(fields) < 3:
            continue
        kind = fields[1]
        if kind == "screen":
            nodes = read_screen(fields[2])
            screen = last_screen = Path(fields[2]).stem
            if len(fields) >= 5 and fields[3].isdigit() and fields[4].isdigit():
                width, height = int(fields[3]), int(fields[4])
        elif kind == "type":
            text = fields[2]
            if not text:
                continue
            if text not in values:
                values[text] = f"TEXT_{len(values) + 1}"
                params.append({"name": values[text], "secret": True, "description": "A text the agent typed"})
            add({"kind": "eraseText"})
            add({"kind": "inputText", "text": {"param": values[text]}})
        elif kind == "adb":
            args = input_args(fields[2:])
            if args[:1] == ["tap"] and len(args) >= 3 and args[1].lstrip("-").isdigit() and args[2].lstrip("-").isdigit():
                x, y = int(args[1]), int(args[2])
                target, bounds = target_at(nodes, x, y, width, height, values)
                if target:
                    add({"kind": "tapOn", "target": target}, point=[x, y], bounds=bounds)
                else:
                    skipped.append(line.strip())
            elif args[:1] == ["swipe"] and len(args) >= 5 and all(a.lstrip("-").isdigit() for a in args[1:5]):
                x1, y1, x2, y2 = map(int, args[1:5])
                if abs(x2 - x1) > abs(y2 - y1):
                    direction = "LEFT" if x2 < x1 else "RIGHT"
                else:
                    direction = "UP" if y2 < y1 else "DOWN"
                add({"kind": "swipe", "direction": direction}, **{"from": [x1, y1], "to": [x2, y2]})
            elif args[:1] == ["keyevent"] and len(args) == 2 and args[1] in KEYS:
                add({"kind": "pressKey", "key": KEYS[args[1]]})
            elif args:
                skipped.append(line.strip())

    device = persona.get("device") or {}
    context = {"platform": "android"}
    for key, name in (("name", "device"), ("font_scale", "fontScale"), ("theme", "theme")):
        if device.get(key) is not None:
            context[name] = device[key]
    who = {"id": persona["id"], "profile": persona.get("profile", ""), "goal": persona.get("goal", "")}
    if persona.get("locale"):
        who["locale"] = persona["locale"]
    who["context"] = context
    trace = {
        "schema": "ecr.trace/0",
        "id": f"{persona['id']}-{now[:19].replace(':', '').replace('-', '')}",
        "adapter": {"id": "mobile-maestro"},
        "persona": who,
        "params": params,
        "invariants": [],
        "steps": steps,
        **({"evidence": {"lastScreen": last_screen}} if last_screen else {}),
        "provenance": {"exploredAt": now, "agent": "mobile-agent-testers", **({"model": model} if model else {})},
    }
    return trace, {name: text for text, name in values.items()}, skipped


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("results")
    parser.add_argument("--persona", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--personas", default="personas.json")
    parser.add_argument("--log")
    parser.add_argument("--agent-output")
    parser.add_argument("--now")
    args = parser.parse_args(argv)

    results = Path(args.results)
    log = Path(args.log) if args.log else results / "actions.tsv"
    if not log.exists():
        print(f"trace: no notes at {log}, nothing written")
        return 0
    persona = {"id": args.persona}
    try:
        known = json.loads(Path(args.personas).read_text(encoding="utf-8"))
        persona = next((p for p in known if p.get("id") == args.persona), persona)
    except (OSError, ValueError):
        pass
    now = args.now or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    model = model_of(args.agent_output) if args.agent_output else None
    trace, values, skipped = build(log.read_text(encoding="utf-8").splitlines(), persona, args.app_id, now, model)
    if len(trace["steps"]) < 2:
        print("trace: the agent did nothing that can be replayed, nothing written")
        return 0
    (results / "trace.json").write_text(json.dumps(trace, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (results / "trace.params.json").write_text(json.dumps(values, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"trace: {len(trace['steps'])} steps, {len(values)} typed texts kept apart, {len(skipped)} commands left out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
