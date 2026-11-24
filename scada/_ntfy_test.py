"""Phone alerts (ntfy): topic, settings, messages EN/DA, no resend, rate limit, retries, certificates, secrecy.
Every network call is faked: nothing goes to ntfy.sh or curl.se. Run: py scada\\_ntfy_test.py"""
import os, sys, json, time, tempfile, shutil, ssl, types, io, urllib.error, datetime
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F
import burner_notify as N
import burner_ext as X

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
    def __init__(self, fail=None):
        self.sent, self.fail = [], list(fail or [])     # fail: list of SendError to raise first
    def send(self, server, topic, title, body, priority="default", tags=""):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append({"server": server, "topic": topic, "title": title, "body": body, "priority": priority, "tags": tags})

class Mod:
    def __init__(self):
        self.latest, self.tracker, self.optimiser, self.store = None, None, None, None
    def row(self, state, alarm=0, silo=None, epoch=None):
        f = {"state": state, "alarm": alarm}
        if silo is not None:
            f["silo_kg"] = silo
        self.latest = {"epoch": epoch or time.time(), "fields": f}

def pump(n):
    """Deliver what is queued (no threads in the test)."""
    while not n.q.empty():
        n.deliver(n.q.get(), retries=())

def make(d, log=None, backend=None, events=None, lang="en"):
    st = F.Settings(d, log or Log())
    ok, _ = st.update({"ntfy": {"enabled": True, "lang": lang, **({"events": events} if events else {})}})
    assert ok
    m = Mod()
    n = N.Notifier(st, m, d, log or Log(), backend=backend or FakeBackend(), sleep=lambda s: None, start=False)
    return st, m, n

def pem_cert():
    """A real certificate (PEM) from this machine's trust store, for the certificate-file checks."""
    ctx = ssl.create_default_context()
    ders = ctx.get_ca_certs(binary_form=True)
    if not ders:
        p = N.CaBundle.image_bundle()
        with open(p, encoding="ascii") as fh:
            txt = fh.read()
        return txt[txt.index("-----BEGIN CERTIFICATE-----"):txt.index("-----END CERTIFICATE-----") + 25] + "\n"
    return ssl.DER_cert_to_PEM_cert(ders[0])

d = tempfile.mkdtemp()
try:
    # ---------------------------------------------------------------- topic
    st = F.Settings(d, Log())
    tp = st.get("ntfy")["topic"]
    check("topic made at first start: burner- + >= 20 random chars", tp.startswith("burner-") and len(tp) - 7 >= 20)
    check("topic: only letters, digits, - and _", F._NTFY_TOPIC.match(tp) is not None)
    check("topic kept after a restart", F.Settings(d, Log()).get("ntfy")["topic"] == tp)
    check("two new topics differ", F.new_ntfy_topic() != F.new_ntfy_topic())
    check("off by default", st.get("ntfy")["enabled"] is False)
    ok, _ = st.update({"ntfy": {"new_topic": True}})
    check("'new topic' makes another one and saves it", ok and st.get("ntfy")["topic"] != tp
          and F.Settings(d, Log()).get("ntfy")["topic"] == st.get("ntfy")["topic"])

    # ---------------------------------------------------------------- settings validation
    bad = [{"server": "ftp://x.org"}, {"server": "ntfy.sh"}, {"server": "https://user:pw@x.org"},
           {"server": "https://x.org/?a=1"}, {"server": "https:// x.org"}, {"server": "javascript:alert(1)"},
           {"topic": "short"}, {"topic": "has space in it 123456789"}, {"topic": "a/b" * 10},
           {"lang": "de"}, {"events": {"nope": True}}, {"events": []}, {"silo_low_kg": 0}, {"silo_low_kg": "x"},
           {"daily_at": "25:00"}, "not-an-object"]
    check("bad values refused", all(not st.update({"ntfy": b})[0] for b in bad))
    ok, msg = st.update({"ntfy": {"topic": "secret topic!!" + "x" * 20}})
    check("refusal never echoes the topic", not ok and "secret" not in msg)
    ok, _ = st.update({"ntfy": {"server": "https://ntfy.example.org/", "silo_low_kg": 80, "daily_at": "7:30",
                                "events": {"daily": True, "alarm": False}, "lang": "da",
                                "topic": "burner-OwnTopic_1234567890abc"}})
    c = st.get("ntfy")
    check("good values saved (trailing / removed, 07:30)", ok and c["server"] == "https://ntfy.example.org"
          and c["silo_low_kg"] == 80 and c["daily_at"] == "07:30" and c["events"]["daily"] and not c["events"]["alarm"]
          and c["events"]["alarm_clear"] and c["lang"] == "da" and c["topic"] == "burner-OwnTopic_1234567890abc")
    check("http (own server at home) allowed", st.update({"ntfy": {"server": "http://192.0.2.10:8080"}})[0])
    check("defaults: events as planned", {k: v for k, v in F.DEFAULTS["ntfy"]["events"].items()} ==
          {"alarm": True, "alarm_clear": True, "link_lost": True, "link_back": True, "silo_low": True, "ash": True,
           "pi": True, "net": True, "burner_start": False, "burner_stop": False, "monitor_start": False, "daily": False})
    called = []
    st.listeners.append(lambda: called.append(1))
    st.update({"ntfy": {"enabled": True}})
    check("a change is applied at once (listeners told, no restart)", called and st.get("ntfy")["enabled"])
    shutil.rmtree(d); os.makedirs(d)

    # ---------------------------------------------------------------- messages EN / DA
    t, b, p, g = N.message("alarm", "en", code=5, name=N.alarm_name(5, "en"))
    check("alarm EN: existing text, high priority", t == "Boiler alarm: No fuel" and "alarm 5" in b and p == "high")
    t, b, p, g = N.message("alarm", "da", code=5, name=N.alarm_name(5, "da"))
    check("alarm DA: page's Danish name", t == "Kedel-alarm: Mangler brændsel" and "Se til kedlen" in b)
    check("alarm clear EN/DA", N.message("alarm_clear", "en", name="Fan")[0] == "Alarm cleared"
          and N.message("alarm_clear", "da", name="Røgsuger")[0] == "Alarm væk")
    check("every message has EN and DA", all(set(v[2]) == {"en", "da"} for v in N.TEXTS.values()))
    import burner_ext as X
    check("start / stop: who, in the words of burner_ext.SOURCES",
          N.message("burner_start", "en", who=N.who_words("burner_start", "plan", "en"))[1]
          == "The burner started. Started by: " + X.SOURCES["plan"]["en"] + "." and
          N.message("burner_stop", "da", who=N.who_words("burner_stop", "web", "da"))[1]
          == "Brænderen stoppede. Stoppet af: " + X.SOURCES["web"]["da"] + "." and
          N.message("burner_start", "en", who=N.who_words("burner_start", None, "en"))[1] == "The burner started."
          and N.message("burner_start", "en")[1] == "The burner started.")
    check("unknown alarm number", N.alarm_name(99, "en") == "Alarm 99")

    # ---------------------------------------------------------------- alarm: once, cleared, not again after a restart
    be = FakeBackend()
    st, m, n = make(d, backend=be)
    m.row(4); n.check(); m.row(6, 4); n.check(); n.check(); pump(n)
    check("alarm raised sent once", [x["title"] for x in be.sent] == ["Boiler alarm: Start-up failed"]
          and be.sent[0]["priority"] == "high")
    n2 = N.Notifier(st, m, d, Log(), backend=be, sleep=lambda s: None, start=False)     # monitor restart, alarm on
    n2.check(); pump(n2)
    check("same alarm not sent again after a restart", len(be.sent) == 1)
    m.row(0); n2.check(); n2.check(); pump(n2)
    check("alarm cleared sent once", [x["title"] for x in be.sent][1:] == ["Alarm cleared"])
    m.row(6, 5); n2.check(); pump(n2); m.row(0); n2.check(); m.row(6, 5); n2.check(); pump(n2)
    check("a new alarm later is sent (pairs are never rate limited)", [x["title"] for x in be.sent][2:] ==
          ["Boiler alarm: No fuel", "Alarm cleared", "Boiler alarm: No fuel"])
    m.row(6, 0); n2.check(); pump(n2)
    check("ALARM state without number: same alarm, nothing new", len(be.sent) == 5)
    st.update({"ntfy": {"lang": "da"}}); m.row(0); n2.check(); pump(n2)
    check("language follows the setting at once", be.sent[-1]["title"] == "Alarm væk")

    # ---------------------------------------------------------------- contact lost / back
    shutil.rmtree(d); os.makedirs(d)
    be = FakeBackend()
    st, m, n = make(d, backend=be)
    now = time.time()
    m.row(4, epoch=now - 100); n.check(now); pump(n)
    check("short gap: nothing", not be.sent)
    m.row(4, epoch=now - 400); n.check(now); n.check(now + 10); pump(n)
    check("no data > 5 min: 'no contact' once", [x["title"] for x in be.sent] == ["No contact with the boiler"])
    m.row(4, epoch=now + 20); n.check(now + 20); pump(n)
    check("contact back", be.sent[-1]["title"] == "Contact with the boiler is back")

    # ---------------------------------------------------------------- rate limit
    m.row(4, epoch=now + 30)
    m.latest["epoch"] = now - 1000; n.check(now + 40); pump(n)
    check("contact lost again within 30 min: not sent", len(be.sent) == 2)
    m.row(4, epoch=now + 50); n.check(now + 50); pump(n)
    check("... and no 'back' without a 'lost'", len(be.sent) == 2)
    st.update({"ntfy": {"events": {"burner_start": True}}})
    for i in range(3):
        n.on_event({"type": "transition", "from": 0, "to": 2, "cause": "boiler", "source": "panel"})
    pump(n)
    check("same event at most once per 30 min", sum(x["title"] == "Burner started" for x in be.sent) == 1
          and be.sent[-1]["body"] == "The burner started. Started by: " + X.SOURCES["panel"]["en"] + ".")
    n.state["sent"]["burner_start"] -= N.RATE_S + 1
    n.on_event({"type": "transition", "from": 0, "to": 2, "cause": "web", "source": "mqtt"})
    pump(n)
    check("after 30 min again; the event's source field gives the words", be.sent[-1]["body"] ==
          "The burner started. Started by: Home Assistant (MQTT).")
    n.on_event({"type": "transition", "from": 0, "to": 2}); pump(n)
    check("burner stop is off by default", not n.notify("burner_stop"))

    # ---------------------------------------------------------------- off switches
    st.update({"ntfy": {"enabled": False}})
    check("alerts off: nothing queued", not n.notify("monitor_start"))
    st.update({"ntfy": {"enabled": True, "events": {"silo_low": False}}})
    check("event switched off: nothing queued", not n.notify("silo_low", kg=1, lim=2))

    # ---------------------------------------------------------------- silo, Pi, daily
    shutil.rmtree(d); os.makedirs(d)
    be = FakeBackend()
    st, m, n = make(d, backend=be)
    m.row(4, silo=0); n.check(); pump(n)
    check("silo counter 0 from the start (not used): nothing", not be.sent)
    m.row(4, silo=120); n.check(); m.row(4, silo=49); n.check(); n.check(); pump(n)
    check("silo below 50 kg: once", [x["title"] for x in be.sent] == ["Silo low"] and "49 kg" in be.sent[0]["body"])
    day = datetime.date.today().isoformat()
    with open(os.path.join(d, f"pi-health-{day}.jsonl"), "w") as fh:
        fh.write(json.dumps({"epoch": time.time(), "cpu_c": 85.2, "disk_free_mb": 300,
                             "power": {"undervolt_now": True}}) + "\n")
    n._pi_t = 0; n.check(); pump(n)
    check("Pi health: disk, power, heat", {x["title"] for x in be.sent[1:]} ==
          {"Monitor: disk almost full", "Monitor: power too low", "Monitor: too hot"})
    n._pi_t = 0; n.check(); pump(n)
    check("Pi problem told once", len(be.sent) == 4)
    # weak Wi-Fi / no internet: one pi-health line a minute (a fixed clock: t0 + i minutes)
    t0 = time.time() - 3 * 3600
    def net_lines(rows):            # rows: (signal or None, internet ok)
        with open(os.path.join(d, f"pi-health-{day}.jsonl"), "w") as fh:
            for i, (sig, ok) in enumerate(rows):
                fh.write(json.dumps({"epoch": t0 + i * 60, "net": {"wifi_signal": sig} if sig is not None else {},
                                     "internet": {"ok": ok, "dns_ok": ok}}) + "\n")
        X._NET_CACHE.clear()
        return t0 + (len(rows) - 1) * 60
    tn = net_lines([(80, True)] * 5 + [(20, True)] * 14)
    check("net: 14 min weak -> weak_min 14, no notice yet", X.net_trouble(d, now=tn) == {"weak_min": 14, "down_min": 0})
    tn = net_lines([(80, True)] * 5 + [(20, False)] * 16)
    check("net: weak and down 16 min", X.net_trouble(d, now=tn) == {"weak_min": 16, "down_min": 16})
    check("net: cached for a minute", X.net_trouble(d, now=tn + 30) == {"weak_min": 16, "down_min": 16})
    check("net: old lines (Pi health stopped) -> None", X.net_trouble(d, now=tn + 3600) is None)
    tn = net_lines([(None, True)] * 5)
    check("net: cable (no Wi-Fi signal), internet ok -> fine", X.net_trouble(d, now=tn) == {"weak_min": 0, "down_min": 0})
    with open(os.path.join(d, f"pi-health-{day}.jsonl"), "w") as fh:     # 11 min gap: the bad spell starts again
        for i, e in enumerate([0, 60, 120, 120 + 660, 120 + 720]):
            fh.write(json.dumps({"epoch": t0 + e, "net": {"wifi_signal": 20}, "internet": {"ok": False}}) + "\n")
    X._NET_CACHE.clear()
    check("net: a gap of more than 10 min ends a spell", X.net_trouble(d, now=t0 + 840)["down_min"] == 2)
    n0 = len(be.sent)
    tn = net_lines([(20, False)] * 29)
    n._check_net(tn); pump(n)
    check("net: 29 min -> no message", len(be.sent) == n0)
    tn = net_lines([(20, False)] * 31)
    n._check_net(tn); n._check_net(tn + 1); pump(n)
    check("net: weak Wi-Fi 30+ min -> one message (internet down: nothing while it is down)",
          [x["title"] for x in be.sent[n0:]] == ["Monitor: weak Wi-Fi"] and "31 minutes" in be.sent[-1]["body"])
    tn = net_lines([(20, False)] * 50 + [(80, True)])
    n._check_net(tn); pump(n)
    check("net: internet back -> 'was down' with the minutes", be.sent[-1]["title"] == "Monitor: internet was down"
          and "50 minutes" in be.sent[-1]["body"] and not n.state.get("net_down_since"))
    n._check_net(tn); pump(n)
    check("net: told once", len(be.sent) == n0 + 2)
    check("net: Danish texts", N.message("net_weak", "da", min=31)[0] == "Monitor: svag Wi-Fi"
          and "50 minutter" in N.message("net_down", "da", min=50)[1])
    st.update({"ntfy": {"events": {"net": False}}})
    check("net: own switch", not n.notify("net_weak", min=40))
    st.update({"ntfy": {"events": {"net": True}}})
    import burner_responder as R
    rm = R.Module(0x01); rm.store = types.SimpleNamespace(dir=d)
    net_lines([(80, True)] * 3 + [(20, False)] * 16)
    X._NET_CACHE[d] = (time.time(), X.net_trouble(d, now=t0 + 18 * 60))
    check("status.json: 'net' for the /home notice after 15 min", rm.feed_snapshot(since=0).get("net") ==
          {"weak_min": 16, "down_min": 16})
    X._NET_CACHE[d] = (time.time(), {"weak_min": 14, "down_min": 0})
    check("status.json: no 'net' when fine or shorter", "net" not in rm.feed_snapshot(since=0))
    # daily summary on a fixed clock (today 19:59 / 20:05), so the test does not depend on when it runs.
    # It covers the 24 h before it is sent, from the day files of both days (made-up rows, one a minute).
    at = lambda hh, mm, back=0: datetime.datetime.combine(datetime.date.today() - datetime.timedelta(days=back),
                                                          datetime.time(hh, mm)).timestamp()

    def hist(dd, burns):
        """Write made-up history rows: burns = [(start epoch, minutes, end state, alarm)]; 0.01 kg a minute."""
        rows, silo = [], 200.0
        for t, mins, end, al in burns:
            rows.append({"epoch": t - 60, "state": 1, "alarm": 0, "silo_kg": round(silo, 2)})
            for i in range(mins):
                rows.append({"epoch": t + i * 60, "state": 2 if i == 0 else 4, "alarm": 0, "silo_kg": round(silo, 2)})
                silo -= 0.01
            rows.append({"epoch": t + mins * 60, "state": end, "alarm": al, "silo_kg": round(silo, 2)})
        for r in rows:
            day = datetime.date.fromtimestamp(r["epoch"]).isoformat()
            with open(os.path.join(dd, f"hist-{day}.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(r) + "\n")

    hist(d, [(at(18, 0, 1), 60, 1, 0),             # yesterday before the window: not counted
             (at(23, 0, 1), 120, 1, 0),            # across midnight: counted in full
             (at(6, 0), 60, 6, 3)])                # this morning, ends in an alarm
    st.update({"ntfy": {"events": {"daily": True}, "daily_at": "20:00"}})
    n.state.pop("daily_last", None)   # run between 20:00 and 20:30 the checks above already took today's turn
    m.optimiser = types.SimpleNamespace(ash_status=lambda: {"emptied_at": 1, "full_pct": 95},
                                        health=lambda days: {"status": "ok"}, report_days=lambda: [])
    m.store = types.SimpleNamespace(dir=d)
    m.row(4, epoch=at(19, 59)); n.check(at(19, 59)); pump(n)
    check("daily summary: not before its time", "Last 24 h at the boiler" not in [x["title"] for x in be.sent])
    m.row(4, epoch=at(20, 5)); n._ash_t = 0; n.check(at(20, 5)); n.check(at(20, 5) + 10); pump(n)
    titles = [x["title"] for x in be.sent]
    check("ash bin almost full: once", titles.count("Ash bin almost full") == 1)
    check("daily summary once, last 24 h across midnight", titles.count("Last 24 h at the boiler") == 1 and
          "Burned 3.0 h, 2 start(s), about 1.8 kg pellets, 1 alarm(s)." in [x["body"] for x in be.sent])
    # early send time (08:00): the window is yesterday 08:00 .. today 08:00; a burn running at the window start
    # counts only its minutes inside the window and not as a start
    d8 = tempfile.mkdtemp()
    hist(d8, [(at(7, 30, 1), 60, 1, 0), (at(21, 0, 1), 90, 1, 0), (at(7, 0), 30, 1, 0), (at(9, 0), 60, 1, 0)])
    m8 = types.SimpleNamespace(store=types.SimpleNamespace(dir=d8))
    w8 = N.Notifier.window_summary(types.SimpleNamespace(mod=m8), at(8, 0))
    check("last 24 h at 08:00: burns by start time inside the window", w8["starts"] == 2)
    check("last 24 h at 08:00: only the minutes inside the window", w8["running_s"] == (30 + 90 + 30) * 60)
    check("last 24 h at 08:00: pellets inside the window", F.day_numbers(w8)["fuel_kg"] == 1.5)
    st8, mm8, n8 = make(tempfile.mkdtemp(), backend=FakeBackend(), lang="da")
    st8.update({"ntfy": {"events": {"daily": True}, "daily_at": "08:00"}})
    mm8.store = types.SimpleNamespace(dir=tempfile.mkdtemp())
    mm8.row(1, epoch=at(8, 1)); n8.check(at(8, 1)); pump(n8)
    sent8 = n8.backend.sent
    check("zero burns: daily summary still sent, in Danish", len(sent8) >= 1 and sent8[-1]["title"] ==
          "Seneste 24 t ved kedlen" and sent8[-1]["body"] == "Brændt 0.0 t, 0 start(er), ca. 0.0 kg piller, 0 alarm(er).")
    n.on_event({"type": "warning", "source": "ash_plate", "kind": "stuck_out", "text": "The ash plate is stuck."})
    pump(n)
    check("ash plate warning forwarded", be.sent[-1]["title"] == "Ash plate")

    # ---------------------------------------------------------------- retries / backoff / drop
    waits = []
    be = FakeBackend(fail=[N.SendError("net", "no connection", True), N.SendError("http", "the server answered 503", True)])
    st2, m2, n2 = make(tempfile.mkdtemp(), backend=be)
    n2.sleep = waits.append
    ok = n2.deliver({"key": "test", "title": "T", "body": "B"})
    check("retries with growing waits, then sent", ok and waits == [5, 20] and len(be.sent) == 1 and n2.last["ok"])
    be = FakeBackend(fail=[N.SendError("net", "x", True)] * 10)
    log = Log()
    st3, m3, n3 = make(tempfile.mkdtemp(), backend=be, log=log)
    waits = []; n3.sleep = waits.append
    check("keeps failing: dropped after 3 retries", n3.deliver({"key": "alarm", "title": "T", "body": "B"}) is False
          and waits == [5, 20, 60] and n3.last["ok"] is False)
    be = FakeBackend(fail=[N.SendError("http", "the server answered 403", False)])
    n3.backend = be; waits = []
    check("refused (4xx): no retry", n3.deliver({"key": "x", "title": "T", "body": "B"}) is False and waits == [])
    topic3 = st3.get("ntfy")["topic"]
    check("logs never hold the topic, only its last 4 chars", log.lines and all(topic3 not in l for l in log.lines)
          and any(N.mask(topic3) in l for l in log.lines) and N.mask(topic3) == "***" + topic3[-4:])
    # one log line per message result (responder.log), test send included; never the full topic
    log4 = Log()
    st4, m4, n4 = make(tempfile.mkdtemp(), backend=FakeBackend(), log=log4)
    topic4 = st4.get("ntfy")["topic"]
    n4.send_test()
    n4.backend = FakeBackend(fail=[N.SendError("http", "the server refused " + topic4, False)])
    n4.deliver({"key": "alarm", "title": "T", "body": "B"}, retries=())
    check("log: 'ntfy: sent test (ok)' after the test send", "ntfy: sent test (ok)" in log4.lines)
    check("log: 'ntfy: failed alarm: <reason>' with the topic masked",
          any(l.startswith("ntfy: failed alarm: ") and N.mask(topic4) in l for l in log4.lines))
    check("log lines never hold the full topic", log4.lines and all(topic4 not in l for l in log4.lines))

    # ---------------------------------------------------------------- ntfy request (faked)
    reqs = []
    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_open(req, timeout=None, context=None):
        reqs.append((req, timeout, context)); return Resp(b"{}")
    N._urlopen = fake_open
    ca = N.CaBundle(tempfile.mkdtemp(), Log())
    nb = N.NtfyBackend(ca)
    nb.send("https://ntfy.example.org", "burner-abcdefghijklmnopqrstuvwx", "Alarm væk", "Æble", "high", "rotating_light")
    r, to, ctx = reqs[-1]
    check("POST to server/topic with Title, Priority, Tags", r.get_method() == "POST" and
          r.full_url == "https://ntfy.example.org/burner-abcdefghijklmnopqrstuvwx" and r.get_header("Priority") == "high"
          and r.get_header("Tags") == "rotating_light" and r.get_header("Title").startswith("=?UTF-8?B?")
          and r.data == "Æble".encode())
    check("timeout set, certificates always checked", to and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname)
    def cert_fail(req, timeout=None, context=None):
        raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
    N._urlopen = cert_fail
    try:
        nb.send("https://ntfy.example.org", "burner-abcdefghijklmnopqrstuvwx", "T", "B"); e = None
    except N.SendError as ex:
        e = ex
    check("certificate error: clear message with the clock hint, no retry", e and e.kind == "cert" and not e.retry
          and "check the Pi's clock" in str(e))
    N._urlopen = no_net

    # ---------------------------------------------------------------- certificates
    one = pem_cert()
    good = ("##\n## Certificate data from Mozilla as of: Tue Sep  9 03:12:01 2025 GMT\n##\n" + ("\nFőtanúsítvány (UTF-8 name)\n=====\n" + one) * 120)
    cd = tempfile.mkdtemp()
    ca = N.CaBundle(cd, Log())
    ca.install(one, "upload")
    check("upload of a good PEM accepted", os.path.isfile(ca.path) and ca.info()["source"] == "upload")
    def serve(data):
        def f(req, timeout=None, context=None):
            assert req.full_url == "https://curl.se/ca/cacert.pem" and context.verify_mode == ssl.CERT_REQUIRED
            return Resp(data)
        return f
    N._urlopen = serve(good.encode())
    n_ok = ca.update()
    inf = ca.info()
    check("update: good bundle installed, old kept as .bak", n_ok == 120 and inf["source"] == "download"
          and inf["date"] == "2025-09-09" and os.path.isfile(ca.path + ".bak"))
    before = open(ca.path, encoding="utf-8").read()
    for name, data in (("bad PEM (html page)", b"<html>not found</html>" * 5000), ("too few certificates", one.encode() * 3),
                       ("cut-off file", good.encode()[:-40]), ("too big", b"x" * (4 * 1024 * 1024))):
        N._urlopen = serve(data)
        try:
            ca.update(); refused = False
        except N.CertError:
            refused = True
        check(f"update refused: {name}; old bundle kept", refused and open(ca.path, encoding="utf-8").read() == before)
    N._urlopen = cert_fail
    try:
        ca.update(); refused = None
    except N.CertError as ex:
        refused = str(ex)
    check("download not verified by the current bundle: refused, old kept, clock hint",
          refused and "clock" in refused and open(ca.path, encoding="utf-8").read() == before)
    for bad in ("", "hello", "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"):
        try:
            ca.install(bad, "upload"); ok = True
        except N.CertError:
            ok = False
        check(f"upload refused: {bad[:20]!r}", not ok and open(ca.path, encoding="utf-8").read() == before)
    N._urlopen = no_net
    st.update({"ntfy": {"verify": False, "insecure": True}})
    check("no setting can turn certificate checks off", "verify" not in json.dumps(st.get("ntfy"))
          and ca.context().verify_mode == ssl.CERT_REQUIRED and ca.context().check_hostname)

    # ---------------------------------------------------------------- topic never on MQTT
    pub = []
    class FakeClient:
        def __init__(self, *a, **k): self.connected, self.stop = True, False
        def publish(self, topic, payload, retain=False): pub.append((topic, json.dumps(payload, default=str) if not isinstance(payload, (bytes, str)) else str(payload)))
        def subscribe(self, t): pass
    X.MqttClient = FakeClient
    sd = tempfile.mkdtemp()
    st = F.Settings(sd, Log()); st.update({"ntfy": {"enabled": True}})
    secret = st.get("ntfy")["topic"]
    import threading
    mod = types.SimpleNamespace(hist=[], settings=st, lock=threading.RLock(), recent_events=[], vvb=None, ash=None, fan=None, scheduler=None, auto_reset=None, store=None, lux=None, tracker=None, optimiser=None, ignitions=None, warm=None,
                                weather=None, cfg_sync=None,
                                feed_snapshot=lambda: {"latest": {"epoch": time.time(), "fields": {"state": 4, "alarm": 0}},
                                                       "cfg": {}, "labels": {"alarms": {}}, "connected": True})
    args = types.SimpleNamespace(mqtt_prefix="burner", mqtt_discovery="homeassistant", allow_remote_start=False,
                                 mqtt_read_only=True, mqtt_flue="auto", data_dir=sd, mqtt="127.0.0.1", mqtt_pass=None,
                                 mqtt_user=None)
    br = X.MqttBridge(mod, args, Log())
    br._on_connect(); br.publish_state(); br.publish_stats()
    br.event({"type": "settings", "keys": ["ntfy"], "msg": "saved"})
    nn = N.Notifier(st, mod, sd, Log(), backend=FakeBackend(), start=False)
    nn.on_event({"type": "transition", "from": 0, "to": 2}); nn.status()
    br.c.stop = True
    check("topic is never published on MQTT", pub and not any(secret in t or secret in p for t, p in pub))

    # ---------------------------------------------------------------- QR code; without segno only the address
    s1 = nn.status()
    try:
        import segno  # noqa: F401
        check("QR code as SVG with segno", (s1["qr_svg"] or "").startswith("<svg") and secret in s1["subscribe_url"])
    except ImportError:
        print("note: segno is not installed here, QR code not checked")
    saved = sys.modules.get("segno")
    sys.modules["segno"] = None                  # as if segno were missing: import fails
    try:
        s2 = nn.status()
    finally:
        if saved is None:
            sys.modules.pop("segno", None)
        else:
            sys.modules["segno"] = saved
    check("without segno: no QR, the address is still there", s2["qr_svg"] is None and s2["subscribe_url"] == s1["subscribe_url"])
finally:
    N._urlopen = no_net
    shutil.rmtree(d, ignore_errors=True)
# ---- why the monitor (re)started: an update says so
td = tempfile.mkdtemp()
try:
    sm = N.Ntfy.start_message if hasattr(N, "Ntfy") else None
    cls = next(c for c in vars(N).values() if isinstance(c, type) and hasattr(c, "start_message"))
    sm = cls.start_message
    T0 = 1000000.0
    def upd(**d):
        with open(os.path.join(td, "update.json"), "w", encoding="utf-8") as fh:
            json.dump(dict({"checked": T0}, **d), fh)
    check("no update.json: plain 'monitor started'", sm(td, T0) == ("monitor_start", {}))
    upd(code="updated", current="v1.0.5", **{"from": "v1.0.2", "to": "v1.0.5"})
    k, pp = sm(td, T0 + 60)
    check("just updated: says from and to", k == "monitor_updated" and pp == {"frm": "v1.0.2", "to": "v1.0.5"})
    t, b, _, _ = N.message(k, "en", **pp)
    check("updated text", "now on v1.0.5 (was v1.0.2)" in b)
    check("an old update is not news", sm(td, T0 + 3600) == ("monitor_start", {}))
    upd(code="rolled_back", current="v1.0.5", **{"from": "v1.0.5", "to": "v1.0.6"})
    k, pp = sm(td, T0 + 60)
    check("rolled back: names the version that failed", k == "monitor_rolled_back" and pp == {"tried": "v1.0.6", "cur": "v1.0.5"})
    upd(code="went_back", current="v1.0.0", **{"from": "v1.0.1", "to": "v1.0.0"})
    check("went back on request", sm(td, T0 + 60) == ("monitor_went_back", {"cur": "v1.0.0"}))
    check("DA texts exist", all(N.message(k2, "da", frm="a", to="b", tried="c", cur="d")[0] for k2 in
                                ("monitor_updated", "monitor_rolled_back", "monitor_went_back")))
    check("no data folder: plain start", sm(None, T0) == ("monitor_start", {}))
    upd(code=None, phase="install", current="v1.0.2")
    check("while the updater tests the swap: busy", cls.update_busy(td) is True)
    upd(code="updated", phase="idle", current="v1.0.5", **{"from": "v1.0.2", "to": "v1.0.5"})
    check("after the test: not busy", cls.update_busy(td) is False)
    check("no data folder: not busy", cls.update_busy(None) is False)
finally:
    shutil.rmtree(td, ignore_errors=True)
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
