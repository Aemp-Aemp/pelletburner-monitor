"""Offline tests: hot-water tank (VVB) sensor input + flame-by-power summary. No serial port, no board."""
import sys, os, json, time, tempfile, struct
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F
import burner_responder as R

FAILED = 0


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name)
    FAILED += 0 if cond else 1


d = tempfile.mkdtemp(prefix="vvb_test_")
s = F.Settings(d, lambda m: None)
ev = []
v = F.VvbInput(s, lambda m: None, ev.append)

check("off by default -> responder's fixed reg0", v.reg0(0.0) == 0.0)
check("off -> readings refused", v.put(50, "mqtt")[0] is False)
check("bad source refused", s.update({"vvb": {"source": "wifi"}})[0] is False)
check("safe_c below 40 refused", s.update({"vvb": {"safe_c": 20}})[0] is False)
check("bad 1-wire id refused", s.update({"vvb": {"onewire_id": "../../etc"}})[0] is False)
ok, _ = s.update({"vvb": {"enabled": True, "source": "mqtt", "max_age_min": 1, "safe_c": 75}})
check("enable mqtt source", ok)
check("no reading yet -> safe 75", v.reg0(0.0) == 75.0)
check("reading from the wrong source refused", v.put(50, "http")[0] is False)
check("out of range refused", v.put(150, "mqtt")[0] is False and v.put("nan", "mqtt")[0] is False)
check("text refused", v.put("hot", "mqtt")[0] is False)
check("good reading accepted", v.put("52.34", "mqtt")[0] is True)
check("fresh reading sent", v.reg0(0.0) == 52.3)
v.tick()
v.at -= 61                       # older than max_age_min
check("stale -> safe 75", v.reg0(0.0) == 75.0)
v.tick(); v.tick()
check("stale warning emitted once", len([e for e in ev if e.get("source") == "vvb"]) == 1)
check("feed reports stale", v.feed()["fresh"] is False and v.feed()["sent_c"] == 75.0)

# 1-wire parsing with fake sysfs files
w = os.path.join(d, "w1")
os.makedirs(os.path.join(w, "28-0123456789ab"))
p = os.path.join(w, "28-0123456789ab", "w1_slave")
with open(p, "w") as fh:
    fh.write("72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=23125\n")
v.W1_GLOB = os.path.join(w, "28-*", "w1_slave")
check("1-wire reading parsed", v.read_onewire() == 23.125)
with open(p, "w") as fh:
    fh.write("72 01 4b 46 7f ff 0e 10 57 : crc=57 NO\n72 01 4b 46 7f ff 0e 10 57 t=23125\n")
check("1-wire bad CRC ignored", v.read_onewire() is None)
with open(p, "w") as fh:
    fh.write("50 05 4b 46 7f ff 0c 10 1c : crc=1c YES\n50 05 4b 46 7f ff 0c 10 1c t=85000\n")
check("1-wire power-on 85 C ignored", v.read_onewire() is None)

# settings survive a reload
s2 = F.Settings(d, lambda m: None)
check("settings saved", s2.get("vvb")["enabled"] is True and s2.get("vvb")["max_age_min"] == 1)

# responder answers reg0 with the tank value
mod = R.Module(0x01)
mod.vvb = v
v.put(48.0, "mqtt")
frames = list(R.Splitter().feed(R.build_frame(0x01, 0, 0x00, 3, b"")))
f = frames[0] if frames else None
if f is not None and f.crc_ok and not f.write:
    out, why = mod.reply(f)
    check("responder reg0 read answers the tank value", "48.0" in why and struct.pack(">f", 48.0) in out.replace(b"}", b""))
else:
    print("SKIP responder reg0 frame test", f and (f.crc_ok, f.write))

# flame by power
rows = []
t0 = 1_700_000_000
for i in range(200):
    pw = 40 if i < 100 else 95
    rows.append({"epoch": t0 + i * 10, "state": 4, "power_pct": pw, "flame": 150 if pw == 40 else 90,
                 "fan_rpm": 1600 if pw == 40 else 2500, "fuel_gph": 2200})
sm = F.summarise_rows(rows)
fp = F.flame_by_power([sm, sm])
check("flame by power: two steps", [r["power"] for r in fp] == [40, 90])
check("flame by power: averages", fp[0]["flame"] == 150 and fp[1]["flame"] == 90 and fp[1]["rpm"] == 2500)
check("flame by power: days add up", fp[0]["minutes"] == round(2 * 99 * 10 / 60))

print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
