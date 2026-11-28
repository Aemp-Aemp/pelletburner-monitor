"""Home Assistant "Last start lighting time" is the time to flame of a real lighting (burner_features.lit), and a
start that went on burning without lighting (resumed) or a relight is not "the last start" - for HA and for the
phone's status answer alike. Made-up start records, no MQTT broker. Run: py scada\\_ha_lighting_test.py"""
import os, sys, time, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F
import burner_ext as X
import burner_phone as P

fails = 0
def check(name, cond, info=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {info}"))
    if not cond:
        fails += 1

now = time.time()
real = {"epoch": now - 7200, "result": "ok", "minutes": 9.5, "ignition_min": 4, "flame_s": 222, "source": "panel"}
resumed = {"epoch": now - 600, "result": "ok", "minutes": 1.0, "ignition_min": 0, "flame_s": 0, "source": "mqtt"}
relight = {"epoch": now - 300, "result": "ok", "minutes": 3, "relight": True, "flame_s": 30}
check("made-up records: real lit, resumed is resumed", F.lit(real) and F.resumed(resumed) and not F.lit(resumed))


def stats(records):
    mod = types.SimpleNamespace(ignitions=types.SimpleNamespace(records=records), optimiser=None, warm=None)
    br = types.SimpleNamespace(mod=mod, log=lambda *a: None)
    return X.MqttBridge.compute_stats(br)


s = stats([real, resumed, relight])
check("HA: lighting time = time to flame of the last real lighting (min)", s["last_start_min"] == F.flame_min(real) == 3.7,
      s["last_start_min"])
check("HA: the resumed start and the relight are not the last start", s["last_start_source"] == "panel"
      and s["last_start_at"] == X._iso_tz(real["epoch"]), s)
failed = {"epoch": now - 100, "result": "failed", "minutes": None, "flame_s": None, "source": "panel"}
s = stats([real, failed])
check("HA: a failed last start has no lighting time", s["last_start_min"] is None and s["last_start_result"] == "failed")
s = stats([resumed])
check("HA: only a resumed start: no last start at all", s["last_start_min"] is None and s["last_start_at"] is None)

# the phone's "status" answer uses the same filter
nt = types.SimpleNamespace(lang=lambda: "en")
mod = types.SimpleNamespace(latest={}, ignitions=types.SimpleNamespace(records=[real, resumed, relight]), optimiser=None)
pc = types.SimpleNamespace(nt=nt, mod=mod, clock=time.time)
txt = P.PhoneCommands.status_text(pc, now)
want = time.strftime("%H:%M", time.localtime(real["epoch"]))
check("phone status: last start is the real lighting, not the resumed one", want in txt
      and time.strftime("%H:%M", time.localtime(resumed["epoch"])) not in txt, txt)
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
