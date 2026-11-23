"""settings.json is only written when its content changed (start-up, the same value again), so the SD card is
spared and settings.json.bak keeps the real previous version."""
import os, sys, tempfile, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F

FAILED = 0


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name)
    FAILED += 0 if cond else 1


d = tempfile.mkdtemp()
p = os.path.join(d, "settings.json")
log = lambda m: None
s = F.Settings(d, log)                                  # first start: makes the topics and writes the file
s.update({"schedule": {"enabled": True}})
s.update({"schedule": {"enabled": False}})              # now .bak = the file with the schedule on
old = os.path.getmtime(p)
os.utime(p, (old - 100, old - 100))                     # so a rewrite is easy to see
os.utime(p + ".bak", (old - 100, old - 100))
mt, bak = os.path.getmtime(p), open(p + ".bak").read()

s2 = F.Settings(d, log)                                 # a restart with nothing changed
check("start-up: settings.json not rewritten", os.path.getmtime(p) == mt)
check("start-up: .bak keeps the real previous version", open(p + ".bak").read() == bak
      and json.loads(bak)["schedule"]["enabled"] is True)
s2.update({"schedule": {"enabled": False}})             # the same value again
s2.set_internal("schedule", "enabled", False)
check("same value saved again: no write", os.path.getmtime(p) == mt)
s2.update({"schedule": {"enabled": True}})
check("a real change is written", os.path.getmtime(p) != mt and json.load(open(p))["schedule"]["enabled"] is True)
check("and .bak is the version before it", json.load(open(p + ".bak"))["schedule"]["enabled"] is False)

# a save that cannot be written (read-only file / folder, full SD card): an error, never "saved"; the old values stay
import stat, socket, urllib.request, urllib.error
import burner_responder as R

d2 = tempfile.mkdtemp()
p2 = os.path.join(d2, "settings.json")
st = F.Settings(d2, log)
st.update({"warm": {"stop_pct": 40}})
os.chmod(p2, stat.S_IREAD)                              # Windows: a read-only file can't be replaced
if os.name != "nt":
    os.chmod(d2, 0o555)                                 # Linux: only a read-only folder stops the replace
try:
    ro = not os.access(p2, os.W_OK) or os.name == "nt"  # root may write anyway: then skip the read-only part
    if not ro:
        print("SKIP read-only checks (running as root)")
    else:
        ok, msg = st.update({"warm": {"stop_pct": 55}})
        check("read-only: update says it could not save", ok is False and msg == F.Settings.SAVE_FAILED)
        check("read-only: the old value stays in use", st.get("warm")["stop_pct"] == 40)
        check("read-only: the file still has the old value", json.load(open(p2))["warm"]["stop_pct"] == 40)
        check("read-only: no temp file left", not os.path.exists(p2 + ".tmp"))
        check("read-only: pellet count refused too", st.set_stock(500)[0] is False and not (st.get("pellets") or {}).get("stock"))
        check("read-only: new batch refused too", st.add_batch("x")[0] is False)

        s_ = socket.socket(); s_.bind(("127.0.0.1", 0)); port = s_.getsockname()[1]; s_.close()
        mod = R.Module(0x01)
        mod.settings = st
        R.start_http(mod, port, lambda *a: None)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/settings", data=b'{"warm":{"stop_pct":55}}',
                                     method="POST", headers={"X-Burner": "1", "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5).read(); code, j = 200, {}
        except urllib.error.HTTPError as e:
            code, j = e.code, json.loads(e.read())
        check("read-only: /api/settings answers 507 with a plain message", code == 507 and j.get("ok") is False
              and j.get("disk") is True and "full or read-only" in j.get("msg", ""))
        check("read-only: the answer shows the old value", j.get("settings", {}).get("warm", {}).get("stop_pct") == 40)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/settings", data=b'{"warm":{"stop_pct":5}}',
                                     method="POST", headers={"X-Burner": "1", "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5).read(); code = 200
        except urllib.error.HTTPError as e:
            code, j = e.code, json.loads(e.read())
        check("a bad value is still a plain 400 (not the storage message)", code == 400 and "disk" not in j)
finally:
    if os.name != "nt":
        os.chmod(d2, 0o755)
    os.chmod(p2, stat.S_IREAD | stat.S_IWRITE)
ok, _ = st.update({"warm": {"stop_pct": 55}})
check("writable again: saved", ok and json.load(open(p2))["warm"]["stop_pct"] == 55)

print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
