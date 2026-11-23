"""Regression tests for earlier code-review findings (no hardware, a few seconds)."""
import sys, os, json, time, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_responder as R
import burner_ext as X
from burner_sniff import build_frame, Splitter

res = []
def check(name, cond): res.append(("PASS" if cond else "FAIL", name))

def frames(payload_chunks):
    sp = Splitter()
    out = []
    for p in payload_chunks:
        out += list(sp.feed(build_frame(0x01, 3, 0x20, 4, p)))
    return out

ST = '{"status_info":{"000":0,"001":20.5,"002":15,"003":0.0,"004":0,"005":0,"006":0,"007":0,"008":300,"009":1000,"010":1,"011":0,"012":1,"013":0,"014":0,"015":1,"016":1,"017":0,"018":0}}'

# 1 lost middle frame: partial A, (B lost), C with EOT -> dropped, next message parses again
m = R.Module(0x01); m.fw_mode = True
a, c = ST[:60].encode(), ST[120:].encode() + b"\x04"
for f in frames([a, c]):
    m.reply(f)
check("1 broken message dropped (buffer empty)", len(m.json_in) == 0)
for f in frames([ST.encode() + b"\x04"]):
    m.reply(f)
check("1 next message parses after a lost frame", m.latest is not None)
# oversize cap
for f in frames([b"x" * 200] * 50):
    m.reply(f)
check("1 buffer never exceeds the cap", len(m.json_in) <= 8192)

# 2 non-dict JSON must not raise
m = R.Module(0x01); m.fw_mode = True
ok = True
try:
    for p in (b"[1,2]\x04", b'{"devicedrive":"x"}\x04', b'"str"\x04', b"123\x04"):
        m.json_in.clear()
        for f in frames([p]):
            m.reply(f)
except Exception as e:
    ok = False
    print("raised", repr(e))
check("2 non-dict JSON handled without exception", ok)

# 3 command expiry + scheduler start re-check at delivery
m = R.Module(0x01)
m.queue_command("start", "mqtt"); m.svc_once_meta["t"] -= 120
check("3 a command older than 90 s is dropped", m.take_command() is None)
m.latest = {"epoch": time.time(), "fields": {"state": 6, "alarm": 4}}
m.queue_command("start", "schedule")
check("3 scheduler start into ALARM is dropped at delivery", m.take_command() is None)
m.queue_command("start", "mqtt")
check("3 manual (MQTT) start in ALARM is still delivered (clears alarm like the button)", m.take_command() == {"run_mode": "start"})
m.latest = {"epoch": time.time() - 120, "fields": {"state": 0, "alarm": 0}}
m.queue_command("start", "schedule")
check("3 scheduler start without fresh status is dropped", m.take_command() is None)
m.latest = {"epoch": time.time(), "fields": {"state": 0, "alarm": 0}}
m.queue_command("start", "schedule")
check("3 scheduler start when idle is delivered", m.take_command() == {"run_mode": "start"})
check("3 nothing left after delivery", m.take_command() is None)

# 10/11/16 CfgSync
d = tempfile.mkdtemp()
p = os.path.join(d, "desired.json")
json.dump({"cfg_data_1": {"000": 65, "001": 2200, "002": 45, "003": 0, "004": 2000, "005": 40, "006": 6,
                          "007": 300, "008": 1020, "009": 1700, "010": 1900, "014": 1600, "017": 6000,
                          "035": 5.0, "036": 0.005, "033": 1}, "check_only": ["033"]}, open(p, "w"))
logs = []
cs = X.CfgSync(p, logs.append, every_s=0)
check("10 valid file loaded", len(cs.desired) == 16)
ok, msg = cs.set_values({"000": 1e9})
check("10 out-of-range value refused", not ok)
ok, msg = cs.set_values({"000": "65"})
check("10 string value refused", not ok)
ok, msg = cs.set_values({"018": 0})
check("10 MAX DOSER 0 refused (the boiler would do ~255 top-ups)", not ok)
ok, msg = cs.set_values({"016": 0})
check("10 OPVARMNING 0 refused (~18 h wait)", not ok)
ok, msg = cs.set_values({"033": 2})
check("10 check-only key refused", not ok)
ok, msg = cs.set_values({"000": 66})
check("10 good value saved atomically", ok and json.load(open(p))["cfg_data_1"]["000"] == 66 and not os.path.exists(p + ".tmp"))
board = {k: 1 for k in cs.desired}
cs.on_cfg(board)
sizes = [len(json.dumps({"response": "OK", "cfg_data_1": b}, separators=(",", ":"))) + 1 for b in cs.queue]
check("11 every batch <= 100 bytes", all(s <= 100 for s in sizes) and sum(len(b) for b in cs.queue) == 15)
# 16: a key the board refuses is parked after 3 rounds, not rewritten forever
cs2 = X.CfgSync(p, logs.append, every_s=0)
board = {k: v for k, v in cs2.desired.items()}; board["005"] = 99            # board refuses 005
for _ in range(5):
    cs2.queue = []
    cs2.on_cfg(board)
check("16 refused key parked (mismatch, no more writes)", cs2.status == "mismatch" and not cs2.queue and "005" in cs2.stuck)
# 7: broken file -> previous values kept, error reported once
json.dump({"cfg_data_1": {"000": 65}, "check_only": 33}, open(p, "w"))
os.utime(p, (time.time() + 5, time.time() + 5))
n0 = len(logs)
cs2.tick(); cs2.tick(); cs2.tick()
check("7 bad check_only -> no crash, previous values kept, logged once",
      len(cs2.desired) == 16 and len([l for l in logs[n0:] if "cannot use" in l]) == 1)
json.dump({"cfg_data_1": {}}, open(p, "w"))
os.utime(p, (time.time() + 10, time.time() + 10))
cs2.tick()
check("10 empty file is an error, not 'in sync'", "error" in cs2.status and len(cs2.desired) == 16)

for st, name in res:
    print(st, name)
nfail = sum(1 for st, _ in res if st == "FAIL")
print("failed:", nfail)
sys.exit(1 if nfail else 0)                  # run_tests.py only sees the exit code
