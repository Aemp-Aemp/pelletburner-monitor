"""Phone commands after an outage: a start / stop older than 5 min (its own time against the ntfy server's clock)
is never acted on, a backlog burst gets one "too old" answer, and duplicates act at most once. Made-up messages,
no network, no boiler. Run: py scada\\_phone_old_test.py"""
import os, sys, time, tempfile, shutil
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F
import burner_notify as N
import burner_phone as P

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
    name = "fake"
    def __init__(self): self.sent, self.n = [], 0
    def send(self, server, topic, title, body, priority="default", tags="", actions=""):
        self.n += 1
        self.sent.append({"title": title, "body": body})
        return f"own{self.n}"

class Mod:
    def __init__(self):
        self.calls, self.remote_armed = [], True
        self.latest = {"epoch": time.time(), "fields": {"state": 0, "alarm": 0}}
    def request_run_mode(self, cmd, source, from_hub=False):
        self.calls.append(cmd)
        return True, "queued", 200

class Mono:
    t = 1000.0
    def __call__(self): return self.t

PIN = "482913"
d = tempfile.mkdtemp()
try:
    for lang, word in (("en", "Too old: send it again."), ("da", "For gammel: send den igen.")):
        log = Log()
        os.makedirs(os.path.join(d, lang))
        st = F.Settings(os.path.join(d, lang), log)
        assert st.update({"ntfy": {"enabled": True, "lang": lang, "cmd_pin": PIN}})[0]
        assert st.update({"ntfy": {"cmd_enabled": True}})[0]
        be, mod, mono = Backend(), Mod(), Mono()
        nt = N.Notifier(st, mod, os.path.join(d, lang), log, backend=be, sleep=lambda s: None, start=False)
        srv0 = 1.7e9
        pc = P.PhoneCommands(nt, mod, log, clock=lambda: srv0 + mono.t + 3600, start=False, mono=mono)  # Pi clock 1 h off
        pc.handle({"event": "open", "time": srv0 + mono.t})
        pump = lambda: [nt.deliver(nt.q.get(), retries=()) for _ in range(nt.q.qsize())]
        srv = lambda: srv0 + mono.t
        # the listener ran before the outage: messages from the outage arrive after a 20 h gap
        pc.t0_srv = srv() - 24 * 3600
        mono.t += 20 * 3600
        pc.handle({"event": "open", "time": srv()})
        rs = [pc.handle({"event": "message", "id": f"b{lang}{i}", "time": int(srv() - 3 * 3600 + i), "message": "start"})
              for i in range(3)]
        pump()
        check(f"{lang}: three backlogged starts (3 h old): none acted on", rs == ["too_old"] * 3 and pc.pending is None
              and not mod.calls)
        check(f"{lang}: one 'too old' answer for the burst", [x["body"] for x in be.sent] == [word])
        r = pc.handle({"event": "message", "id": f"r{lang}", "time": int(srv() - 600), "message": f"1234 {PIN}"})
        check(f"{lang}: an old code reply is not acted on", r == "too_old" and not mod.calls)
        r = pc.handle({"event": "message", "id": f"s{lang}", "time": int(srv() - 3600), "message": "status"})
        check(f"{lang}: an old 'status' is still answered", r == "status")
        # fresh but sent twice and arriving together: one code, at most one action
        pump(); be.sent.clear()
        rs = [pc.handle({"event": "message", "id": f"f{lang}{i}", "time": int(srv() - 30), "message": "start"})
              for i in range(3)]
        pump()
        check(f"{lang}: a fresh burst of starts asks once", rs == ["asked", "dup", "dup"] and len(be.sent) == 1)
        code = pc.pending["code"]
        r1 = pc.handle({"event": "message", "id": f"c{lang}1", "time": int(srv()), "message": f"{code} {PIN}"})
        r2 = pc.handle({"event": "message", "id": f"c{lang}2", "time": int(srv()), "message": f"{code} {PIN}"})
        check(f"{lang}: the code acts once", r1 == "sent" and r2 == "no_pending" and mod.calls == ["start"])
        mono.t += 60
        r = pc.handle({"event": "message", "id": f"n{lang}", "time": int(srv() - 290), "message": "stop"})
        check(f"{lang}: just under 5 min old still asks", r == "asked")
finally:
    shutil.rmtree(d, ignore_errors=True)
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
