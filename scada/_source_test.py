"""Who / what started (and stopped) the boiler: the `source` on every start / stop event and start record.
One case per source, kept over a restart, older events without a source stay empty. Made-up data only."""
import sys, os, json, tempfile, datetime
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_ext as X
import burner_features as F
import burner_responder as R

res = []
def check(name, cond, info=""): res.append(("PASS" if cond else "FAIL", name, "" if cond else info))
def ep(s): return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()
log = lambda m: None


class Mod:
    """Just what the cause needs: the last delivered command."""
    def __init__(self): self.last_cmd, self.latest, self.latest_cfg = None, None, None


d = tempfile.mkdtemp()
mod = Mod()
tr = X.Tracker(X.Store(d, 0, log))
tr.cause_fn = lambda frm, to, now: X.command_cause(mod.last_cmd, frm, to, now)
ign = F.IgnitionLog(F.Settings(d, log), tr, mod, d, log)


def row(t, st, alarm=0, gap=False):
    if tr.state is not None and not gap:         # the link was up: the old state was still seen 30 s before
        r = {"epoch": ep(t) - 30, "state": tr.state, "alarm": tr.alarm}
        tr.on_row(r, [r])
        ign.on_row(r)
    r = {"epoch": ep(t), "state": st, "alarm": alarm}
    tr.on_row(r, [r])
    ign.on_row(r)

def cmd(t, what, source):                       # a command of ours, delivered to the boiler at t
    mod.last_cmd = {"cmd": what, "source": source, "delivered": ep(t)}

def last(): return tr.transitions[-1]
def src(): return last().get("source")


row("2000-01-01 05:00", 0)
check("first row: no event", not tr.transitions)

# 1) heating plan start, normal steps, heating plan stop (low power)
cmd("2000-01-01 06:00", "start", "warm mode"); row("2000-01-01 06:01", 2)
check("plan start -> plan", src() == "plan" and last()["cause"] == "warm mode", str(last()))
row("2000-01-01 06:02", 3); check("OPSTART -> IGNITION: no source", src() is None)
row("2000-01-01 06:30", 4); check("IGNITION -> RUNNING: no source", src() is None)
cmd("2000-01-01 09:00", "stop", "warm mode (low power)"); row("2000-01-01 09:01", 0)
check("plan stop -> plan", src() == "plan")
check("start record has the source", ign.records[-1].get("source") == "plan" and ign.records[-1]["result"] == "ok")

# 2) Home Assistant (MQTT) start and stop
cmd("2000-01-01 10:00", "start", "mqtt"); row("2000-01-01 10:01", 2)
check("MQTT start -> mqtt", src() == "mqtt")
row("2000-01-01 10:02", 3); row("2000-01-01 10:30", 4)
cmd("2000-01-01 11:00", "stop", "mqtt"); row("2000-01-01 11:01", 0)
check("MQTT stop -> mqtt", src() == "mqtt")

# 3) web page button
cmd("2000-01-01 12:00", "start", "web"); row("2000-01-01 12:01", 2)
check("web start -> web", src() == "web")
row("2000-01-01 12:02", 3); row("2000-01-01 12:30", 4)

# 4) the boiler's own minimum mode: burns out by itself, starts again by itself
row("2000-01-01 14:00", 1)
check("burn-out DRIFT -> WAITING -> boiler_min", src() == "boiler_min")
check("an old command is not credited again", last().get("cause") is None)
row("2000-01-01 20:00", 2)
check("WAITING -> OPSTART by itself -> boiler_min", src() == "boiler_min" and last()["cause"] == "boiler")
row("2000-01-01 20:01", 3); row("2000-01-01 20:30", 4)
check("start record: boiler_min", ign.records[-1].get("source") == "boiler_min")

# 5) by hand on the boiler panel: stop, then start, with no command from us
row("2000-01-01 21:00", 0)
check("stop with no command -> panel", src() == "panel")
row("2000-01-01 21:05", 2)
check("STOPPET -> OPSTART with no command -> panel", src() == "panel" and not last().get("after_alarm"))

# 6) boiler still warm (OPSTART -> PAUSESTOP -> IGNITION is one start), then its own pause and relight
row("2000-01-01 21:06", 5); row("2000-01-01 21:20", 3)
check("PAUSESTOP -> IGNITION inside a start: no source", src() is None)
row("2000-01-01 21:40", 4); row("2000-01-01 22:30", 5)
check("RUNNING -> PAUSESTOP -> boiler_temp", src() == "boiler_temp")
row("2000-01-01 22:50", 3)
check("relight after a pause -> boiler_temp", src() == "boiler_temp")
row("2000-01-01 23:00", 4)
check("relight record: boiler_temp", ign.records[-1].get("source") == "boiler_temp" and ign.records[-1]["relight"])

# 7) schedule: ends the boiler's WAITING, then starts
row("2000-01-02 01:00", 1)
cmd("2000-01-02 02:00", "stop", "schedule (end WAITING)"); row("2000-01-02 02:01", 0)
check("schedule stop -> schedule", src() == "schedule")
cmd("2000-01-02 02:02", "start", "schedule"); row("2000-01-02 02:03", 2)
check("schedule start -> schedule", src() == "schedule")

# 8) alarm; auto alarm reset clears it and starts again
row("2000-01-02 02:04", 6, alarm=4)
check("into ALARM: no source", src() is None)
cmd("2000-01-02 02:10", "start", "auto_reset (clear)"); row("2000-01-02 02:11", 0)
check("alarm cleared by auto reset -> auto_reset", src() == "auto_reset")
cmd("2000-01-02 02:12", "start", "auto_reset (restart)"); row("2000-01-02 02:13", 2)
check("restart by auto reset -> auto_reset", src() == "auto_reset" and last().get("after_alarm") is True)

# 9) alarm reset by hand on the boiler, then started by hand: "after alarm reset"
row("2000-01-02 02:14", 6, alarm=4)
row("2000-01-02 03:00", 0)
check("alarm reset by hand -> panel", src() == "panel")
row("2000-01-02 03:05", 2)
check("start right after: panel + after_alarm", src() == "panel" and last().get("after_alarm") is True)
row("2000-01-02 03:06", 3); row("2000-01-02 03:30", 4); row("2000-01-02 04:00", 0)
check("start record keeps after_alarm", ign.records[-1].get("after_alarm") is True)
row("2000-01-02 05:00", 2)
check("a start long after the reset: no after_alarm", src() == "panel" and not last().get("after_alarm"))
row("2000-01-02 05:01", 3); row("2000-01-02 05:30", 4)

# 10) remote command file
row("2000-01-02 06:00", 0)
cmd("2000-01-02 07:00", "start", "trigger file"); row("2000-01-02 07:01", 2)
check("trigger file -> remote", src() == "remote")
row("2000-01-02 07:02", 3); row("2000-01-02 07:30", 4)

# 11) the link was down while it changed (power off / on): honest "not seen"
row("2000-01-02 08:30", 0, gap=True)
check("stop during a link gap -> link_down", src() == "link_down")
row("2000-01-02 09:30", 2, gap=True)
check("start during a link gap -> link_down", src() == "link_down")
cmd("2000-01-02 09:31", "stop", "web"); row("2000-01-02 09:32", 0)
check("web stop -> web", src() == "web")

# 12) stored: the event files hold the source, and a restart keeps it
lines = []
for p in sorted(os.listdir(d)):
    if p.startswith("events-"):
        lines += [json.loads(x) for x in open(os.path.join(d, p), encoding="utf-8")]
saved = [e.get("source") for e in lines if e.get("type") == "transition" and e.get("source")]
check("saved in the event log", saved[:3] == ["plan", "plan", "mqtt"] and "link_down" in saved, str(saved))
tr2 = X.Tracker(X.Store(d, 0, log))
check("kept over a restart", [t.get("source") for t in tr2.transitions] == [t.get("source") for t in tr.transitions])
ign2 = F.IgnitionLog(F.Settings(d, log), tr2, mod, d, log)
check("start records kept over a restart", [r.get("source") for r in ign2.records] == [r.get("source") for r in ign.records]
      and ign2.records[0].get("source") == "plan")

# 13) older data: a cause but no source gets one; nothing known stays empty (no "unknown")
old = [{"type": "transition", "from": 0, "to": 2, "epoch": ep("2000-01-03 06:00"), "cause": "warm mode"},
       {"type": "transition", "from": 2, "to": 3, "epoch": ep("2000-01-03 06:01")},
       {"type": "transition", "from": 3, "to": 0, "epoch": ep("2000-01-03 06:10"), "cause": "boiler"},
       {"type": "transition", "from": 0, "to": 2, "epoch": ep("2000-01-03 07:00")},
       {"type": "transition", "from": 2, "to": 0, "epoch": ep("2000-01-03 07:01")},
       {"type": "warning", "epoch": ep("2000-01-03 07:02"), "source": "lux"}]
X.add_sources(old)
check("older events: source from the saved cause", [e.get("source") for e in old[:3]] == ["plan", None, "panel"])
check("older events with nothing known: no source", "source" not in old[3] and "source" not in old[4])
check("other event types untouched", old[5]["source"] == "lux")
d3 = tempfile.mkdtemp()
with open(os.path.join(d3, "ignitions.jsonl"), "w", encoding="utf-8") as fh:
    for k, c in enumerate(["warm mode", "boiler", None]):
        fh.write(json.dumps({"epoch": ep("2000-01-03 06:00") + k * 3600, "result": "ok", "minutes": 30, "relight": False,
                             "batch": "-", "cause": c}) + "\n")
ign3 = F.IgnitionLog(F.Settings(d3, log), X.Tracker(None), mod, d3, log)
check("older start records: plan / boiler (cannot tell) / nothing",
      [r.get("source") for r in ign3.records] == ["plan", "boiler", None], str(ign3.records))

# 14) one table of words (EN + DA), sent to the pages in /status.json; the pages use it
used = {"plan", "schedule", "mqtt", "web", "phone", "remote", "auto_reset", "boiler_min", "boiler_temp", "panel", "boiler",
        "link_down", "after_alarm"}
check("every source has English and Danish words", set(X.SOURCES) == used
      and all(w.get("en") and w.get("da") for w in X.SOURCES.values()))
check("source_text", X.source_text("plan") == "heating plan" and X.source_text("mqtt", "da") and X.source_text(None) is None)
m = R.Module(0x01)
check("status.json carries the words", m.feed_snapshot()["labels"]["sources"] == X.SOURCES)
here = os.path.dirname(os.path.abspath(__file__))
for page in ("home.html", "dashboard.html"):
    txt = open(os.path.join(here, page), encoding="utf-8").read()
    check(f"{page}: shows the source, EN + DA, no own copy of the words",
          "labels.sources" in txt and (page == "home.html" or "labels:j.labels" in txt)
          and 'started_by:"Started by"' in txt and 'started_by:"Startet af"' in txt
          and 'stopped_by:"Stoppet af"' in txt and "causeText" not in txt and "heating plan\"}" not in txt)

for r in res:
    print(*r)
fails = [r for r in res if r[0] != "PASS"]
print(f"{len(res) - len(fails)}/{len(res)} passed")
sys.exit(1 if fails else 0)
