import os
"""Offline test: /classic and /home routes, and the web start/stop gate (POST /api/run_mode)."""
import sys, json, urllib.request, urllib.error
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_responder as R

FAILED = 0


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name)
    FAILED += 0 if cond else 1


PORT = 8813
mod = R.Module(0x01)
sent = []
SRV = R.start_http(mod, PORT, lambda *a: None)
base = f"http://127.0.0.1:{PORT}/"


def get(p):
    try:
        with urllib.request.urlopen(base + p, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post(p, body, hdr=True):
    h = {"Content-Type": "application/json", "X-Burner": "1"} if hdr else {"Content-Type": "application/json"}
    req = urllib.request.Request(base + p, data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


s, b = get("classic")
check("/classic serves the classic page", s == 200 and b"<title>" in b)
sh, h = get("home")
check("/home answers (200 when home.html exists, else 404 with a message)", sh in (200, 404))
s, b2 = get("")
check("/ is the start page = the home page", s == sh and b2 == h and b2 != b)
s, g = get("guide")
check("/guide serves docs/guide.html", s == 200 and b"<title>" in g and g != h)
check("/guide.html is the same page", get("guide.html") == (200, g))
check("guide has no outside pictures (works offline)", b'src="http' not in g and b"images/" not in g)

check("run_mode refused when not armed", post("api/run_mode", {"cmd": "start"})[0] == 403)
mod.remote_armed = True
mod.run_mode_cb = lambda c, src: sent.append((c, src))
check("run_mode needs the X-Burner header", post("api/run_mode", {"cmd": "start"}, hdr=False)[0] == 403)
check("bad command refused", post("api/run_mode", {"cmd": "boom"})[0] == 400 and not sent)
s, j = post("api/run_mode", {"cmd": "stop"})
check("stop queued with source 'web'", s == 200 and sent == [("stop", "web")])

# in ALARM start/stop resets the alarm: refused unless the page sends reset_alarm: true (after its confirm)
import time as _t
sent.clear()
mod.latest = {"epoch": _t.time(), "fields": {"state": 6, "alarm": 4}}
for c in ("start", "stop"):
    s, j = post("api/run_mode", {"cmd": c})
    check(f"ALARM: {c} without reset_alarm refused with a plain message", s == 409 and "ALARM" in j.get("msg", ""))
    check(f"ALARM: {c} with reset_alarm 'yes' (not true) refused",
          post("api/run_mode", {"cmd": c, "reset_alarm": "yes"})[0] == 409)
check("ALARM: nothing queued without the flag", not sent)
s, j = post("api/run_mode", {"cmd": "start", "reset_alarm": True})
check("ALARM: start with reset_alarm accepted, source labelled", s == 200 and sent == [("start", "web (alarm reset)")])
ev = [e for e in mod.recent_events if e.get("type") == "alarm_reset_web"]
check("ALARM: the reset is in the events", ev and "owner confirmed" in ev[-1]["msg"] and ev[-1]["alarm"] == 4)
check("ALARM: MQTT still refused, even with the flag",
      mod.request_run_mode("stop", "mqtt", from_hub=True, reset_alarm=True)[0] is False)
check("ALARM: phone still refused", mod.request_run_mode("stop", "phone", from_hub=True)[0] is False)
sent.clear()
mod.latest = {"epoch": _t.time(), "fields": {"state": 4, "alarm": 0}}
s, j = post("api/run_mode", {"cmd": "stop"})
check("no alarm: plain stop still works", s == 200 and sent == [("stop", "web")])
s, j = post("api/run_mode", {"cmd": "start", "reset_alarm": True})
check("no alarm: the flag changes nothing", s == 200 and sent[-1] == ("start", "web"))
mod.latest = None

# version / update line (the Pi's updater writes data/update.json; the button leaves data/update-request)
import tempfile, types
s, j = get("api/update")
check("/api/update without a data folder: no version", s == 200 and json.loads(j) == {"current": None})
check("update check needs a data folder", post("api/update/check", {})[0] == 400)
tmpd = tempfile.mkdtemp()
mod.store = types.SimpleNamespace(dir=tmpd)
with open(os.path.join(tmpd, "update.json"), "w") as fh:
    json.dump({"current": "v1.0.0", "latest": "v1.1.0", "available": True, "auto": True}, fh)
s, j = get("api/update")
check("/api/update shows the updater's file", s == 200 and json.loads(j)["latest"] == "v1.1.0")
check("update check needs the X-Burner header", post("api/update/check", {}, hdr=False)[0] == 403
      and not os.path.exists(os.path.join(tmpd, "update-request")))
s, j = post("api/update/check", {})
check("update check leaves a request file", s == 200 and os.path.exists(os.path.join(tmpd, "update-request")))
mod.store = None

# which Host names may use the page (Raspberry Pi on the home network)
H = R.host_allowed
check("localhost always allowed", H("127.0.0.1:8787") and H("localhost") and H("[::1]:8787"))
check("other names refused by default", not H("10.55.0.1:8787") and not H("evil.example:8787"))
check("listed name allowed", H("pelletpi.local:8787", ["pelletpi.local"]))
check("'lan' allows private IPv4 numbers", H("10.55.0.1:8787", ["lan"]) and H("192.168.1.20", ["lan"]) and H("172.16.0.5:80", ["lan"]))
check("'lan' still refuses names and public addresses", not H("evil.example", ["lan"]) and not H("8.8.8.8", ["lan"]))

# a refused name gets a plain page that says what to do (the API stays JSON)
def get_as(p, host):
    req = urllib.request.Request(base + p, headers={"Host": host})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
s, b = get_as("home", "raspberrypi.local:8813")
check("refused name: plain page names BURNER_ALLOWED_HOSTS", s == 403 and b"BURNER_ALLOWED_HOSTS" in b)
check("refused name: no guide either", get_as("guide", "raspberrypi.local:8813")[0] == 403)
s, b = get_as("api/settings", "raspberrypi.local:8813")
check("refused name: API answers JSON", s == 403 and json.loads(b)["ok"] is False)

# a second server on a port in use must fail, not share the port (Windows SO_REUSEADDR gave random answers)
try:
    R.start_http(R.Module(0x01), PORT, lambda *a: None)
    busy = False
except OSError:
    busy = True
check("a port already in use gives an error (no silent sharing)", busy)

# a page closed or reloaded mid-answer: one short log line, no traceback, nothing raised
import io, contextlib
for err in (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
    class Gone:
        closed = False
        def write(self, b): raise err(10053, "closed by the other side")
        def flush(self): pass
    h = SRV.RequestHandlerClass.__new__(SRV.RequestHandlerClass)
    h.command, h.path, h.requestline, h.request_version = "GET", "/status.json?since=1", "GET /status.json", "HTTP/1.1"
    h.headers, h.wfile, h.close_connection = {}, Gone(), False
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            h._send(200, "x" * 2000, "text/plain")
        raised = False
    except Exception:
        raised = True
    lines = out.getvalue().splitlines()
    check(f"{err.__name__} in an answer: one short line, connection closed",
          not raised and len(lines) == 1 and "/status.json" in lines[0] and "since" not in lines[0]
          and h.close_connection)
print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
