#!/usr/bin/env python3
"""API golden snapshots: the program's JSON answers at one fixed replay moment, saved to files and compared later.
Use it around a cleanup: the answers must be the same before and after.

    py tools\\replay_golden.py --save     write tools/golden/*.json   (synthetic data, scada/_synth.py)
    py tools\\replay_golden.py            compare with the saved files; exit 1 and a list of differences if not equal

On a recording of your own (keep --dir outside the repo: it holds real readings):
    py tools\\replay_golden.py --save --data <data-folder> --from "2000-01-01 06:00" --to "2000-01-01 12:00" ^
        --at "09:00" --dir C:\\temp\\golden-before
    py tools\\replay_golden.py --data <data-folder> --from ... --to ... --at ... --dir C:\\temp\\golden-before

Uses tools/replay.py: no serial port, nothing is sent. Values that follow the PC's own clock or folders are left out
(SKIP_KEYS), and so is the secret phone-alert topic. The saved times are local time: save again if the PC's time zone changes."""
import argparse, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scada"))
import _synth

PATHS = {"status": "/status.json", "settings": "/api/settings", "optimiser": "/api/optimiser",
         "ignitions": "/api/ignitions", "lux": "/api/lux", "fan": "/api/fan", "history": "/history.json?h=1",
         "insights": "/api/insights"}
SKIP_KEYS = {"generated",           # insights.json: the PC's real time when the cards were built
             "topic",               # settings.ntfy.topic: a new random secret each run; never saved in a file
             "cmd_topic"}           # settings.ntfy.cmd_topic (phone commands): the same
LONG = 40                           # keeps the saved files small


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in sorted(o.items()) if k not in SKIP_KEYS}
    if isinstance(o, list) and len(o) > 2 * LONG:      # long row lists: the count and about LONG rows spread over it
        return {"count": len(o), "sample": [clean(v) for v in o[::len(o) // LONG]]}
    return [clean(v) for v in o] if isinstance(o, list) else o


def diff(a, b, path=""):
    """Differences between two JSON values as lines 'path: saved -> now'."""
    if isinstance(a, dict) and isinstance(b, dict):
        return [d for k in sorted(set(a) | set(b)) for d in
                (diff(a[k], b[k], f"{path}.{k}") if k in a and k in b else [f"{path}.{k}: only in {'saved' if k in a else 'now'}"])]
    if isinstance(a, list) and isinstance(b, list):
        out = [d for i, (x, y) in enumerate(zip(a, b)) for d in diff(x, y, f"{path}[{i}]")]
        return out + ([f"{path}: {len(a)} items -> {len(b)} items"] if len(a) != len(b) else [])
    return [] if a == b and type(a) is type(b) else [f"{path}: {json.dumps(a)[:80]} -> {json.dumps(b)[:80]}"]


def fetch(a):
    kw = dict(data=a.data, t_from=a.t_from, t_to=a.t_to) if a.data else {}
    with _synth.Replay(a.port, at=a.at, **kw) as rp:
        return {name: clean(rp.get(path)) for name, path in PATHS.items()}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Save or compare the program's API answers at one replay moment.")
    ap.add_argument("--save", action="store_true", help="write the files (default: compare with them)")
    ap.add_argument("--dir", default=os.path.join(HERE, "golden"), help="folder of the saved files")
    ap.add_argument("--data", default="", help="a recorded data folder (default: synthetic data)")
    ap.add_argument("--from", dest="t_from", default="2000-01-03 05:30")
    ap.add_argument("--to", dest="t_to", default="2000-01-03 09:30")
    ap.add_argument("--at", default="08:00", help="the moment the answers are taken")
    ap.add_argument("--port", type=int, default=8817)
    a = ap.parse_args(argv)
    now = fetch(a)
    if a.save:
        os.makedirs(a.dir, exist_ok=True)
        for name, obj in now.items():
            with open(os.path.join(a.dir, name + ".json"), "w", encoding="utf-8", newline="\n") as fh:
                json.dump(obj, fh, ensure_ascii=False, indent=0, separators=(",", ":"), sort_keys=True)
                fh.write("\n")
        print(f"saved {len(now)} files in {a.dir}")
        return 0
    bad = 0
    for name, obj in now.items():
        try:
            with open(os.path.join(a.dir, name + ".json"), encoding="utf-8") as fh:
                d = diff(json.load(fh), obj, name)
        except (OSError, ValueError) as e:
            d = [f"{name}: no saved file ({e})"]
        print(("SAME  " if not d else "DIFF  ") + name + (f"  ({len(d)} differences)" if d else ""))
        for line in d[:20]:
            print("      " + line)
        bad += bool(d)
    print("golden: all the same" if not bad else f"golden: {bad} of {len(now)} answers differ")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
