"""Start / stop from the phone (ntfy): PIN hash, code flow, wrong PIN, lock, expiry, replay, alarm, own messages,
secrecy, settings. Every network call is faked (nothing goes to ntfy.sh); no serial port, no boiler: the start /
stop gate is a fake that only records. Run: py scada\\_phone_test.py"""
import os, sys, json, time, tempfile, shutil, io, ssl, types, threading
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F
import burner_notify as N
import burner_phone as P
import burner_ext as X
import burner_common as C

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

class FakeBackend:
    name = "fake"
    def __init__(self): self.sent, self.n = [], 0
    def send(self, server, topic, title, body, priority="default", tags="", actions=""):
        self.n += 1
        self.sent.append({"topic": topic, "title": title, "body": body, "priority": priority, "actions": actions})
        return f"own{self.n}"

class Mod:
    """Stands in for burner_responder.Module: request_run_mode only records (and refuses like the real one)."""
    def __init__(self):
        self.calls, self.remote_armed = [], True
        self.row(0)
    def row(self, state, alarm=0):
        self.latest = {"epoch": time.time(), "fields": {"state": state, "alarm": alarm}}
    def request_run_mode(self, cmd, source, from_hub=False):
        self.calls.append((cmd, source, from_hub))
        f = self.latest["fields"]
        if from_hub and (f["state"] == 6 or f["alarm"]):
            return False, "boiler is in ALARM - reset it on the boiler or the web page (never from MQTT)", 409
        return True, f"{cmd} queued", 200

class Clock:
    def __init__(self): self.t = time.time()
    def __call__(self): return self.t

PIN = "482913"

def make(d, lang="en", log=None):
    log = log or Log()
    st = F.Settings(d, log)
    ok, msg = st.update({"ntfy": {"enabled": True, "lang": lang, "cmd_pin": PIN}})
    assert ok, msg
    ok, msg = st.update({"ntfy": {"cmd_enabled": True}})
    assert ok, msg
    be, mod, clk = FakeBackend(), Mod(), Clock()
    nt = N.Notifier(st, mod, d, log, backend=be, sleep=lambda s: None, start=False)
    pc = P.PhoneCommands(nt, mod, log, clock=clk, start=False)
    return st, be, mod, clk, nt, pc, log

ids = iter(range(1, 10**6))
def msg(pc, text, t=None):
    return {"event": "message", "id": f"m{next(ids)}", "time": int(t if t is not None else pc.clock()) + 1,
            "topic": "x", "message": text}

def pump(nt):
    while not nt.q.empty():
        nt.deliver(nt.q.get(), retries=())

def code_of(be):
    return be.sent[-1]["body"].split("reply: ")[1].split(" ")[0]

d = tempfile.mkdtemp()
try:
    # ---------------------------------------------------------------- PIN hash
    h = F.hash_pin(PIN)
    check("PIN hash: salted pbkdf2, PIN not in it", h.startswith("pbkdf2$") and PIN not in h and F.hash_pin(PIN) != h)
    check("PIN check: right / wrong / junk", F.check_pin(h, PIN) and not F.check_pin(h, "482914")
          and not F.check_pin(h, "") and not F.check_pin("", PIN) and not F.check_pin("nonsense", PIN))

    # ---------------------------------------------------------------- settings validation
    log = Log()
    st = F.Settings(d, log)
    c = st.get("ntfy")
    check("off by default; own command topic made at first start", c["cmd_enabled"] is False
          and c["cmd_topic"].startswith("burner-cmd-") and len(c["cmd_topic"]) >= 40 and c["cmd_topic"] != c["topic"])
    check("command topic kept after a restart", F.Settings(d, Log()).get("ntfy")["cmd_topic"] == c["cmd_topic"])
    check("can't turn on without a PIN", st.update({"ntfy": {"enabled": True}})[0]
          and st.update({"ntfy": {"cmd_enabled": True}}) == (False, "set a PIN first"))
    bad_pins = ["123", "abcdef", "1234567", 482913, "12 3456"]
    res = [st.update({"ntfy": {"cmd_pin": p}}) for p in bad_pins]
    check("bad PINs refused with a clear reason, never echoed", all(not ok and m == "PIN: use 4-6 digits only" for ok, m in res))
    weak = ["0000", "111111", "1234", "123456", "654321", "0123", "1212", "121212", "2580", "1004", "6969", "4321",
            "123123", "112233", "159753", "34567"]
    check("easy PINs are spotted", all(F.weak_pin(p) for p in weak))
    check("ordinary PINs are not", all(not F.weak_pin(p) for p in ("4829", "73915", "482913", "1357", "9024")))
    ok, m = st.update({"ntfy": {"cmd_pin": "2580"}})
    pub = st.public()["ntfy"]
    check("an easy PIN is accepted, with the warning flag", ok and pub["cmd_pin_easy"] is True and pub["cmd_pin"] == "2580")
    ok, m = st.update({"ntfy": {"cmd_pin": PIN, "cmd_enabled": True}})
    check("PIN saved with its hash", ok and F.check_pin(st.get("ntfy")["cmd_pin_hash"], PIN))
    pub = st.public()["ntfy"]
    check("the owner's page gets the PIN (to show it), not the hash", "cmd_pin_hash" not in pub and pub["cmd_pin_set"] is True
          and pub["cmd_pin"] == PIN and pub["cmd_pin_easy"] is False)
    st2 = F.Settings(d, Log())
    check("PIN, hash and switch kept after a restart", st2.get("ntfy")["cmd_enabled"] and st2.get("ntfy")["cmd_pin"] == PIN
          and F.check_pin(st2.get("ntfy")["cmd_pin_hash"], PIN))
    ok, _ = st.update({"ntfy": {"cmd_pin_hash": F.hash_pin("000000")}})
    check("the hash can't be set from the web", F.check_pin(st.get("ntfy")["cmd_pin_hash"], PIN))
    old = st.get("ntfy")["cmd_topic"]
    ok, _ = st.update({"ntfy": {"new_cmd_topic": True}})
    check("'New command topic' makes another one", ok and st.get("ntfy")["cmd_topic"] not in ("", old))
    ok, m = st.update({"ntfy": {"cmd_topic": st.get("ntfy")["topic"]}})
    check("command topic must differ from the alerts topic", not ok and "differ" in m)
    ok, m = st.update({"ntfy": {"cmd_topic": "secret topic!!" + "x" * 20}})
    check("bad command topic refused, never echoed", not ok and "secret" not in m)
    st.update({"ntfy": {"enabled": False}})
    check("alerts off: commands off too", st.get("ntfy")["cmd_enabled"] is False)
    check("can't turn on while alerts are off", st.update({"ntfy": {"cmd_enabled": True}}) == (False, "turn alerts on first"))
    st.update({"ntfy": {"enabled": True, "cmd_enabled": True}})
    st.update({"ntfy": {"cmd_pin": ""}})
    check("PIN removed: commands off", st.get("ntfy")["cmd_enabled"] is False and not st.get("ntfy")["cmd_pin_hash"])
    check("no PIN or hash in the log", all(PIN not in l and "pbkdf2" not in l for l in log.lines))
    shutil.rmtree(d); os.makedirs(d)

    # ---------------------------------------------------------------- happy path
    st, be, mod, clk, nt, pc, log = make(d)
    cmd_topic = st.get("ntfy")["cmd_topic"]
    r = pc.handle(msg(pc, "start")); pump(nt)
    check("'start' -> asks for code + PIN on the ALERTS topic", r == "asked" and be.sent[-1]["topic"] == st.get("ntfy")["topic"]
          and be.sent[-1]["title"] == "Start the boiler?" and "(valid 2 min)" in be.sent[-1]["body"] and not mod.calls)
    code = code_of(be)
    check("one-time code: 4 digits", len(code) == 4 and code.isdigit())
    check("the answer carries no buttons and no command topic", not be.sent[-1]["actions"]
          and cmd_topic not in json.dumps(be.sent[-1]))
    r = pc.handle(msg(pc, f"{code} {PIN}")); pump(nt)
    check("code + PIN -> the same gate as MQTT/web, source 'phone'", r == "sent" and mod.calls == [("start", "phone", True)]
          and be.sent[-1]["title"] == "Boiler start sent")
    r = pc.handle(msg(pc, f"{code} {PIN}")); pump(nt)
    check("code is single-use", r == "no_pending" and len(mod.calls) == 1 and be.sent[-1]["title"] == "No command waiting")
    pc.handle(msg(pc, "STOP")); pump(nt); code = code_of(be)
    r = pc.handle(msg(pc, f"{code}  {PIN}")); pump(nt)
    check("stop works the same (any case, extra spaces)", r == "sent" and mod.calls[-1] == ("stop", "phone", True)
          and be.sent[-1]["title"] == "Boiler stop sent")
    r = pc.handle(msg(pc, "hello")); pump(nt)
    check("anything else: a short help", r == "help" and be.sent[-1]["title"] == "Not understood"
          and be.sent[-1]["body"].startswith("Not understood. Write: start, stop or status.") and PIN not in be.sent[-1]["body"])
    n_sent = len(be.sent)
    r = pc.handle(msg(pc, "what?")); pump(nt)
    check("help at most once a minute", r == "help_quiet" and len(be.sent) == n_sent)
    clk.t += P.HELP_S + 1
    check("... and again after a minute", pc.handle(msg(pc, "??")) == "help")
    pump(nt)
    mod.latest["fields"].update(state=4, boiler_c=63.4)
    mod.ignitions = types.SimpleNamespace(records=[{"epoch": clk.t - 600, "source": "phone", "result": "ok"}])
    r = pc.handle(msg(pc, "  Status. ")); pump(nt)
    s = be.sent[-1]
    check("status: no PIN needed, state, °C, last start with who", r == "status" and s["title"] == "Boiler status"
          and "State: Running" in s["body"] and "Boiler: 63 °C" in s["body"] and "Last start:" in s["body"]
          and "(phone (ntfy))" in s["body"] and "Alarm" not in s["body"] and len(mod.calls) == 2)
    check("status: nothing secret in it", all(cmd_topic not in s[k] and st.get("ntfy")["topic"] not in s[k]
          and PIN not in s[k] for k in ("title", "body")))
    check("status rate limited (15 s)", pc.handle(msg(pc, "status")) == "status_quiet")
    clk.t += P.STATUS_S + 1
    mod.row(6, 5); pc.handle(msg(pc, "STATUS")); pump(nt)
    check("status in alarm names the alarm", "Alarm: No fuel" in be.sent[-1]["body"])
    mod.row(0); mod.ignitions = None; clk.t += P.STATUS_S + 1
    check("source label EN + DA", C.SOURCES["phone"] == {"en": "phone (ntfy)", "da": "telefon (ntfy)"}
          and X.source_of("phone", 0, 2) == "phone")

    # ---------------------------------------------------------------- expiry, one pending at a time
    pc.handle(msg(pc, "start")); pump(nt); code = code_of(be)
    clk.t += P.CODE_S + 1
    r = pc.handle(msg(pc, f"{code} {PIN}")); pump(nt)
    check("code runs out after 2 min", r == "no_pending" and len(mod.calls) == 2)
    pc.handle(msg(pc, "start")); pump(nt); c1 = code_of(be)
    pc.handle(msg(pc, "stop")); pump(nt); c2 = code_of(be)
    r = pc.handle(msg(pc, f"{c1} {PIN}")) if c1 != c2 else "wrong"
    check("one pending command: a new ask replaces the old code", r == "wrong" and len(mod.calls) == 2)
    pc.fails = 0

    # ---------------------------------------------------------------- wrong PIN, lock
    pc.handle(msg(pc, "start")); pump(nt); code = code_of(be)
    r1 = pc.handle(msg(pc, f"{code} 000000")); pump(nt)
    check("wrong PIN: refused, tries left told", r1 == "wrong" and be.sent[-1]["body"] == "Tries left: 2." and len(mod.calls) == 2)
    wrong_code = f"{(int(code) + 1) % 10000:04d}"
    r2 = pc.handle(msg(pc, f"{wrong_code} {PIN}")); pump(nt)
    check("wrong code with the right PIN: refused too", r2 == "wrong" and len(mod.calls) == 2)
    r3 = pc.handle(msg(pc, f"{code} 111111")); pump(nt)
    check("3rd wrong try: locked 15 min + alert", r3 == "locked_now" and be.sent[-1]["priority"] == "high"
          and be.sent[-1]["title"] == "Wrong PIN 3 times — phone commands locked 15 min")
    r = pc.handle(msg(pc, "start")); pump(nt)
    n_sent = len(be.sent)
    r2 = pc.handle(msg(pc, "start")); pump(nt)
    check("while locked: nothing asked, told once", r == r2 == "locked" and len(be.sent) == n_sent
          and be.sent[-1]["title"] == "Phone commands are locked")
    r = pc.handle(msg(pc, f"{code} {PIN}"))
    check("while locked: even the right code + PIN does nothing", r == "locked" and len(mod.calls) == 2)
    clk.t += P.LOCK_S + 1
    pc.handle(msg(pc, "start")); pump(nt); code = code_of(be)
    r = pc.handle(msg(pc, f"{code} {PIN}")); pump(nt)
    check("after 15 min it works again", r == "sent" and len(mod.calls) == 3)
    check("the monitor never sends the PIN in any message", be.sent and all(PIN not in json.dumps(x, ensure_ascii=False) for x in be.sent))
    check("log: no PIN, no command topic", log.lines and all(PIN not in l and cmd_topic not in l for l in log.lines))

    # ---------------------------------------------------------------- alarm
    mod.row(6, 4)
    r = pc.handle(msg(pc, "start")); pump(nt)
    check("in alarm: refused at once and said so", r == "alarm" and be.sent[-1]["title"] == "Refused: alarm" and len(mod.calls) == 3)
    mod.row(0)
    pc.handle(msg(pc, "start")); pump(nt); code = code_of(be)
    mod.row(6, 5)                                         # the alarm came while the code was waiting
    r = pc.handle(msg(pc, f"{code} {PIN}")); pump(nt)
    check("alarm after the ask: the gate refuses, no reset, said so", r == "refused" and mod.calls[-1] == ("start", "phone", True)
          and be.sent[-1]["title"] == "Refused: alarm")
    mod.row(0); mod.remote_armed = False
    mod.request_run_mode = lambda cmd, src, from_hub=False: (False, "remote start/stop is off (--allow-remote-start)", 403)
    pc.handle(msg(pc, "start")); pump(nt); code = code_of(be)
    pc.handle(msg(pc, f"{code} {PIN}")); pump(nt)
    check("remote start/stop off: refused with the reason", be.sent[-1]["title"] == "Refused" and "allow-remote-start" in be.sent[-1]["body"])
    del mod.request_run_mode; mod.remote_armed = True

    # ---------------------------------------------------------------- replay, old, own, off
    m1 = msg(pc, "start")
    check("same message twice: second ignored", pc.handle(m1) == "asked" and pc.handle(dict(m1)) == "seen")
    check("message from before the listener started: ignored", pc.handle(msg(pc, "start", t=pc.t0 - 60)) == "old")
    pump(nt)
    own = be.sent[-1]
    check("own message ids are kept", "own%d" % be.n in nt.own_ids)
    check("a message the monitor sent itself is ignored", pc.handle({"event": "message", "id": "own%d" % be.n,
                                                                     "time": int(clk.t) + 5, "message": "start"}) == "own")
    check("keepalive / open events ignored", pc.handle({"event": "keepalive", "id": "k1", "time": int(clk.t) + 5}) == "skip"
          and pc.feed(b"not json") == "bad")
    st.update({"ntfy": {"cmd_enabled": False}})
    check("switched off: messages do nothing", pc.handle(msg(pc, "start")) == "off" and not pc.actions())
    st.update({"ntfy": {"cmd_enabled": True}})

    # ---------------------------------------------------------------- alert buttons
    nt.send_test()
    a = be.sent[-1]["actions"]
    check("alerts carry Start / Stop buttons that POST start / stop to the command topic",
          a.count(f"/{st.get('ntfy')['cmd_topic']}") == 2 and "body=start" in a and "body=stop" in a and "method=POST" in a)

    # ---------------------------------------------------------------- Danish
    shutil.rmtree(d); os.makedirs(d)
    st, be, mod, clk, nt, pc, log = make(d, lang="da")
    pc.handle(msg(pc, "start")); pump(nt)
    check("Danish answer", be.sent[-1]["title"] == "Start kedlen?" and "(gælder 2 min)" in be.sent[-1]["body"])
    pc.handle(msg(pc, "hvad?")); pump(nt)
    check("Danish help", be.sent[-1]["body"].startswith("Ikke forstået. Skriv: start, stop eller status."))
    mod.latest["fields"]["boiler_c"] = 55
    pc.handle(msg(pc, "status")); pump(nt)
    check("Danish status", be.sent[-1]["title"] == "Kedel-status" and "Tilstand: Stoppet" in be.sent[-1]["body"]
          and "Kedel: 55 °C" in be.sent[-1]["body"])
    check("every answer has EN and DA", all(set(v[2]) == {"en", "da"} for v in P.TEXTS.values()))

    # ---------------------------------------------------------------- the stream (faked HTTP)
    reqs = []
    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    code_box = {}
    def fake_open(req, timeout=None, context=None):
        reqs.append((req, timeout, context))
        lines = [{"event": "open", "id": "o1", "time": int(clk.t)},
                 {"event": "message", "id": "s1", "time": int(clk.t) - 999, "message": "start"},      # old
                 {"event": "message", "id": "s2", "time": int(clk.t) + 1, "message": "start"},
                 {"event": "keepalive", "id": "k2", "time": int(clk.t) + 2}]
        return Resp("\n".join(json.dumps(x) for x in lines).encode() + b"\n")
    N._urlopen = fake_open
    pc.stream_once()
    N._urlopen = no_net
    r, to, ctx = reqs[-1]
    check("listens with GET <server>/<cmd topic>/json, outbound https, certificates checked",
          r.get_method() == "GET" and r.full_url.startswith(f"https://ntfy.sh/{st.get('ntfy')['cmd_topic']}/json?since=")
          and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname and to)
    check("stream: old message skipped, new one handled", bool(pc.pending) and pc.pending["cmd"] == "start" and pc.since == "s2")
    def fail_open(req, timeout=None, context=None):
        raise OSError("down " + req.full_url)
    N._urlopen = fail_open
    try:
        pc.stream_once(); e = None
    except OSError as ex:
        e = ex
    N._urlopen = no_net
    kind, m = N.classify(e)
    check("connection errors are classified (backoff in the loop), no topic in the text", kind == "net"
          and st.get("ntfy")["cmd_topic"] not in m)

    # ---------------------------------------------------------------- Pi clock off the ntfy server's (chaos item 25)
    for off in (7200, -7200, 26 * 3600):
        lg = Log()
        mono = Clock(); mono.t = 1000.0
        pk = P.PhoneCommands(nt, mod, lg, clock=lambda: mono.t + 1e9 + off, start=False, mono=mono)
        srv = lambda: int(mono.t + 1e9)                   # the server's clock (right)
        mono.t += 3
        r_open = pk.handle({"event": "open", "id": "o", "time": srv()})
        mono.t += 2
        r = pk.handle({"event": "message", "id": f"sk{off}a", "time": srv(), "message": "start"})
        r_old = pk.handle({"event": "message", "id": f"sk{off}b", "time": srv() - 60, "message": "start"})
        r_old2 = pk.handle({"event": "message", "id": f"sk{off}c", "time": srv() - 90, "message": "start"})
        nlog = [l for l in lg.lines if "older than the start" in l]
        check(f"Pi clock {off:+d} s: a new message is a command, an old one ignored, logged once",
              r_open == "open" and r == "asked" and r_old == r_old2 == "old" and len(nlog) == 1
              and "Pi clock is wrong" in nlog[0])
        check(f"Pi clock {off:+d} s: said in the log; the start is kept in the server's clock",
              sum("off the ntfy server" in l for l in lg.lines) == 1 and abs(pk.t0_srv - (1e9 + 1000)) < 1)
        pk.pending = None
    lg = Log()
    pk = P.PhoneCommands(nt, mod, lg, clock=lambda: mono.t + 1e9 - 3600, start=False, mono=mono)
    t_start = mono.t + 1e9
    mono.t += 50; pk.handle({"event": "open", "time": int(mono.t + 1e9)})
    check("no message yet: the reconnect asks from the start in the server's clock", pk.since == str(int(t_start)))
    mono.t += 600
    pk.clock = lambda: mono.t + 1e9 + 7200                  # the Pi clock jumps (set later) after the start
    check("a Pi clock that jumps after the start changes nothing",
          pk.handle({"event": "message", "id": "j1", "time": int(mono.t + 1e9), "message": "start"}) == "asked")
    pk.pending = None
    pk = P.PhoneCommands(nt, mod, lg, clock=lambda: mono.t + 1e9 + 30, start=False, mono=mono)
    pk.handle({"event": "open", "time": int(mono.t + 1e9)})
    check("no 'clock off' line when the clocks nearly agree", sum("off the ntfy" in l for l in lg.lines) == 1)
    lg = Log()
    pk = P.PhoneCommands(nt, mod, lg, clock=clk, start=False)
    check("no open event: within a few seconds of the start still counts, a minute before does not",
          pk.handle(msg(pk, "status", t=pk.t0 - 3)) in ("status", "status_quiet")
          and pk.handle(msg(pk, "status", t=pk.t0 - 60)) == "old")
    pump(nt)

    # ---------------------------------------------------------------- other digit scripts (chaos item 26)
    lg = Log()
    pk = P.PhoneCommands(nt, mod, lg, clock=clk, start=False)
    n_calls = len(mod.calls)
    arab = lambda s: "".join(chr(0x660 + int(x)) for x in s)
    wide = lambda s: "".join(chr(0xFF10 + int(x)) for x in s)
    pk.handle(msg(pk, "start")); pump(nt); code = pk.pending["code"]
    r = pk.feed(json.dumps({"event": "message", "id": "u1", "time": int(clk.t) + 1,
                            "message": f"{arab(code)} {wide(PIN)}"}).encode()); pump(nt)
    check("code + PIN in Arabic-Indic / full-width digits read as 0-9", r == "sent" and len(mod.calls) == n_calls + 1)
    pk.handle(msg(pk, "start")); pump(nt); code = pk.pending["code"]
    r = pk.feed(json.dumps({"event": "message", "id": "u2", "time": int(clk.t) + 1,
                            "message": f"{arab(code)} {arab('000000')}"}).encode()); pump(nt)
    r2 = pk.handle(msg(pk, f"{code} ²{PIN[1:]}")); pump(nt)
    check("wrong PIN in other digits: a wrong try, no error", r == "wrong" and pk.fails == 1 and r2 == "help"
          and not any("error" in l for l in lg.lines))
    check("ascii_digits", P.ascii_digits("٤٨ ２x²") == "48 2x²")
    pk.fails = 0

    # ---------------------------------------------------------------- seen ids kept by age (chaos item 30)
    mono = Clock(); mono.t = 0.0
    pk = P.PhoneCommands(nt, mod, Log(), clock=clk, start=False, mono=mono)
    r0 = pk.handle(msg(pk, "status"))
    first = {"event": "message", "id": "first", "time": int(clk.t) + 1, "message": "start"}
    r0 = pk.handle(first); pump(nt)
    for i in range(2000):
        pk.handle({"event": "message", "id": f"fl{i}", "time": int(clk.t) + 1, "message": "status"})
    pump(nt)
    check("the same id after 2000 others is still ignored", r0 == "asked" and pk.handle(dict(first)) == "seen")
    mono.t += P.SEEN_S + 10; clk.t += P.SEEN_S + 10
    pk.handle({"event": "message", "id": "late", "time": int(clk.t), "message": "x"}); pump(nt)
    check("ids are dropped after SEEN_S, and a message that old is ignored by its time",
          "first" not in pk.seen and len(pk.seen) == 1 and pk.handle(dict(first)) == "old")
    for i in range(P.SEEN_MAX + 5):
        pk.handle({"event": "message", "id": f"cap{i}", "time": int(clk.t), "message": "x"})
    pump(nt)
    check("at most SEEN_MAX ids kept", len(pk.seen) <= P.SEEN_MAX)
    pk.pending = None
    nt.phone = pc

    # ---------------------------------------------------------------- 'New topic': one last message to the old one
    old_topic = st.get("ntfy")["topic"]
    st.update({"ntfy": {"new_topic": True}}); pump(nt)
    fw = be.sent[-1]
    check("new alerts topic: 'Abonnementet er afsluttet' sent to the OLD topic, no secrets, no hint", fw["topic"] == old_topic
          and fw["title"] == "Abonnementet er afsluttet" and fw["body"] == "Der kommer ikke flere beskeder her." and st.get("ntfy")["topic"] not in json.dumps(fw)
          and st.get("ntfy")["cmd_topic"] not in json.dumps(fw) and not fw["actions"])

    # ---------------------------------------------------------------- never on MQTT, never on the page's /api/ntfy as a PIN
    pub = []
    class FakeClient:
        def __init__(self, *a, **k): self.connected, self.stop = True, False
        def publish(self, topic, payload, retain=False): pub.append((topic, json.dumps(payload, default=str) if not isinstance(payload, (bytes, str)) else str(payload)))
        def subscribe(self, t): pass
    X.MqttClient = FakeClient
    secret = st.get("ntfy")["cmd_topic"]
    m2 = types.SimpleNamespace(hist=[], settings=st, lock=threading.RLock(), recent_events=[], vvb=None, ash=None, fan=None,
                               scheduler=None, auto_reset=None, store=None, lux=None, tracker=None, optimiser=None,
                               ignitions=None, warm=None, weather=None, cfg_sync=None,
                               feed_snapshot=lambda: {"latest": {"epoch": time.time(), "fields": {"state": 4, "alarm": 0}},
                                                      "cfg": {}, "labels": {"alarms": {}}, "connected": True})
    args = types.SimpleNamespace(mqtt_prefix="burner", mqtt_discovery="homeassistant", allow_remote_start=True,
                                 mqtt_read_only=False, mqtt_flue="auto", data_dir=d, mqtt="127.0.0.1", mqtt_pass=None,
                                 mqtt_user=None)
    br = X.MqttBridge(m2, args, Log())
    br._on_connect(); br.publish_state(); br.publish_stats()
    br.event({"type": "settings", "keys": ["ntfy"], "msg": "saved"})
    br.c.stop = True
    pin_now = st.get("ntfy")["cmd_pin"]
    check("command topic, PIN and PIN hash never published on MQTT", pin_now and pub and not any(
        secret in t or secret in p or "pbkdf2" in p or pin_now in p or "cmd_pin" in p for t, p in pub))
    s = nt.status()
    check("/api/ntfy: command status, no PIN", s["cmd"] and s["cmd"]["active"] and "pbkdf2" not in json.dumps(s)
          and "cmd_pin" not in json.dumps(s) and pin_now not in json.dumps(s))
    check("no PIN in any ntfy message", not any(pin_now in json.dumps(x) for x in be.sent))
    gold = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "golden")
    txt = "".join(open(os.path.join(gold, f), encoding="utf-8").read() for f in os.listdir(gold))
    check("goldens hold no command topic and no PIN hash", "burner-cmd-" not in txt and "pbkdf2" not in txt
          and "cmd_pin_hash" not in txt)
finally:
    N._urlopen = no_net
    shutil.rmtree(d, ignore_errors=True)
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
