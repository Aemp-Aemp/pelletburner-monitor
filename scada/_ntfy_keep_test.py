"""Phone alerts while the internet is down: important messages are kept (ntfy-state.json, at most 50) and sent when
it is back, oldest first, each with the time it happened; low-value ones are dropped; the daily summary is done only
once it was sent; the rate limit counts sends. Also: the update message has its own rate key, the Danish roll-back
text, and the start message's waiting loop (fake sleep). Made-up data, no network. Run: py scada\\_ntfy_keep_test.py"""
import os, sys, json, time, datetime, tempfile, shutil, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F
import burner_notify as N

fails = 0
def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails += 1

def no_net(*a, **k):
    raise AssertionError("a test tried to use the network")
N._urlopen = no_net

class Log:
    def __init__(self): self.lines = []
    def __call__(self, *a): self.lines.append(" ".join(str(x) for x in a))

class Backend:
    """down=True: every send fails like a missing internet connection."""
    name = "fake"
    def __init__(self): self.sent, self.down = [], False
    def send(self, server, topic, title, body, priority="default", tags="", actions=""):
        if self.down:
            raise N.SendError("net", "no connection to the server (URLError)", True)
        self.sent.append({"title": title, "body": body})
        return None

def make(lang="en", events=None):
    d = tempfile.mkdtemp()
    dirs.append(d)
    st = F.Settings(d, Log())
    ev = dict({"monitor_start": True, "burner_start": True, "daily": True}, **(events or {}))
    assert st.update({"ntfy": {"enabled": True, "lang": lang, "events": ev}})[0]
    be = Backend()
    n = N.Notifier(st, types.SimpleNamespace(latest=None, optimiser=None, store=None), d, Log(), backend=be,
                   sleep=lambda s: None, start=False)
    return d, st, be, n

def pump(n):
    while not n.q.empty():
        n.send_one(n.q.get(), retries=())

dirs = []
try:
    # ---------------------------------------------------------------- kept while down, sent oldest first when back
    d, st, be, n = make()
    be.down = True
    t1 = datetime.datetime.now().replace(hour=14, minute=32, second=0, microsecond=0).timestamp()
    n.notify("link_lost", now=t1, min=6); pump(n)
    n.notify("burner_start", now=t1 + 60, who=""); pump(n)
    n.notify("alarm", now=t1 + 120, code=3, name="Flame lost"); pump(n)
    check("down: the alarm and 'no contact' are kept, 'burner started' is dropped",
          [x["key"] for x in n.pending()] == ["link_lost", "alarm"] and not be.sent)
    saved = json.load(open(os.path.join(d, "ntfy-state.json"), encoding="utf-8"))
    check("kept in ntfy-state.json (survives a restart)", [x["key"] for x in saved["pending"]] == ["link_lost", "alarm"])
    n2 = N.Notifier(st, n.mod, d, Log(), backend=be, sleep=lambda s: None, start=False)
    check("a restart loads them", len(n2.pending()) == 2)
    check("rate limit counts sends: 'no contact' was not sent, so it is not yet counted as sent",
          "link_lost" not in (n.state.get("sent") or {}))
    be.down = False
    n.notify("link_back", now=t1 + 600); pump(n)
    titles = [x["title"] for x in be.sent]
    check("back: kept ones first, oldest first, then the new one",
          titles == ["No contact with the boiler", "Boiler alarm: Flame lost", "Contact with the boiler is back"])
    check("each kept one says when it happened", be.sent[0]["body"].startswith("at 14:32: ")
          and be.sent[1]["body"].startswith("at 14:34: ") and not be.sent[2]["body"].startswith("at "))
    check("nothing kept after sending", n.pending() == [] and "link_lost" in n.state["sent"])
    n.notify("link_lost", min=6)
    check("sent once: the 30-min rate limit counts from the send", n.q.empty())

    # ---------------------------------------------------------------- the minute check sends them without a new message
    d, st, be, n = make(lang="da")
    be.down = True
    n.notify("monitor_updated", now=t1, frm="v1", to="v2"); pump(n)
    be.down = False
    n._pi_t = 0; n.check(t1 + 600); pump(n)
    check("da: kept message sent by the once-a-minute try, with 'kl. 14:32: '",
          be.sent and be.sent[0]["body"].startswith("kl. 14:32: Monitoren kører nu v2"))

    # ---------------------------------------------------------------- one the server refuses for good does not block
    d, st, be, n = make()
    be.down = True
    n.notify("alarm", now=t1, code=3, name="A"); n.notify("alarm_clear", now=t1 + 60, name="A"); pump(n)
    be.down = False
    real_send = be.send
    def refuse_first(server, topic, title, body, *a, **k):
        if title.startswith("Boiler alarm"):
            raise N.SendError("http", "the server answered 400", False)
        return real_send(server, topic, title, body, *a, **k)
    be.send = refuse_first
    check("a kept message refused for good (4xx) is dropped, the next ones still go", n.flush() is True
          and n.pending() == [] and [x["title"] for x in be.sent] == ["Alarm cleared"])

    # ---------------------------------------------------------------- at most 50 kept, the newest
    d, st, be, n = make()
    be.down = True
    for i in range(60):
        n.notify("alarm", now=t1 + i, code=i, name=f"A{i}"); pump(n)
    p = n.pending()
    check("at most 50 kept, the oldest dropped", len(p) == N.PENDING_MAX and p[0]["title"].endswith("A10")
          and p[-1]["title"].endswith("A59"))

    # ---------------------------------------------------------------- the daily summary: done only once sent
    d, st, be, n = make()
    st.update({"ntfy": {"daily_at": "20:00"}})
    n.mod.store = types.SimpleNamespace(dir=d)
    n.state["link_lost"] = True                          # (the made-up boiler sends nothing: no 'no contact' here)
    at20 = datetime.datetime.now().replace(hour=20, minute=5, second=0, microsecond=0).timestamp()
    today = datetime.date.fromtimestamp(at20).isoformat()
    be.down = True
    n.check(at20); pump(n)
    check("daily failed: not marked done", n.state.get("daily_last") != today
          and [x["key"] for x in n.pending()] == ["daily"])
    n.check(at20 + 30); pump(n)
    check("... and not made twice while it waits", [x["key"] for x in n.pending()] == ["daily"])
    be.down = False
    n._pi_t = 0; n.check(at20 + 120); pump(n)
    check("daily sent when back, then marked done", n.state.get("daily_last") == today and len(be.sent) == 1
          and be.sent[0]["title"] == "Last 24 h at the boiler")

    # ---------------------------------------------------------------- 'monitor updated' after a reboot message
    d, st, be, n = make()
    with open(os.path.join(d, "update.json"), "w", encoding="utf-8") as fh:
        json.dump({"checked": time.time(), "code": "updated", "from": "v1.0.2", "to": "v1.0.5", "current": "v1.0.5"}, fh)
    n.notify("monitor_start", now=time.time() - 25 * 60); pump(n)
    n._tell_start(d, sleep=lambda s: None); pump(n)
    check("update message not swallowed by a reboot message 25 min earlier",
          [x["title"] for x in be.sent] == ["Monitor started", "Monitor updated"])
    with open(os.path.join(d, "update.json"), "w", encoding="utf-8") as fh:
        json.dump({"checked": time.time(), "code": "rolled_back", "current": "v1.0.5"}, fh)
    k, pp = N.Notifier.start_message(d, lang="da")
    check("da roll-back: no English 'the new version'", k == "monitor_rolled_back" and pp["tried"] == "den nye version")
    check("en roll-back keeps 'the new version'", N.Notifier.start_message(d)[1]["tried"] == "the new version")

    # ---------------------------------------------------------------- _tell_start waits while the updater tests
    d, st, be, n = make()
    with open(os.path.join(d, "update.json"), "w", encoding="utf-8") as fh:
        json.dump({"phase": "install", "current": "v1.0.2"}, fh)
    slept = []
    def fake_sleep(s):
        slept.append(s)
        if len(slept) == 3:                              # the updater finishes during the third wait
            with open(os.path.join(d, "update.json"), "w", encoding="utf-8") as fh:
                json.dump({"phase": "idle", "checked": time.time(), "code": "updated", "from": "v1.0.2",
                           "to": "v1.0.5", "current": "v1.0.5"}, fh)
    n._tell_start(d, step=10, sleep=fake_sleep); pump(n)
    check("start message waits while the update is tested, then says 'updated'", slept == [10, 10, 10]
          and [x["title"] for x in be.sent] == ["Monitor updated"])
    d, st, be, n = make()
    with open(os.path.join(d, "update.json"), "w", encoding="utf-8") as fh:
        json.dump({"phase": "install", "current": "v1.0.2"}, fh)
    slept = []
    n._tell_start(d, wait=60, step=10, sleep=slept.append); pump(n)
    check("... but not longer than the wait, then the plain start message", slept == [10] * 6
          and [x["title"] for x in be.sent] == ["Monitor started"])
finally:
    for x in dirs:
        shutil.rmtree(x, ignore_errors=True)
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
