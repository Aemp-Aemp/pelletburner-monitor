"""Offline test: an open page must not flood the Wi-Fi (seen: ~100 kB/s from one background tab).
status.json?since= sends only new history rows, JSON and pages are gzipped, pages answer 304 on reload,
and the pages' polling stops while they are hidden."""
import os, sys, re, json, gzip, time, socket, urllib.request, urllib.error
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import burner_responder as R

FAILED = 0


def check(name, cond, extra=""):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra else ""))
    FAILED += 0 if cond else 1


s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
mod = R.Module(0x01)
now = time.time()
for i in range(720):                                     # a full live history (~2 h of rows)
    mod.hist.append({"epoch": now - 7200 + i * 10, "state": 4, "boiler_c": 70.1, "power_pct": 55, "fan_pct": 40,
                     "fan_rpm": 1500, "flame": 120, "silo_kg": 80.5, "setpoint": 72, "fuel_gph": 2100})
R.start_http(mod, PORT, lambda *a: None)
base = f"http://127.0.0.1:{PORT}/"


def get(p, hdr=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(base + p, headers=hdr or {}), timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


_, _, full = get("status.json")
check("status.json without since: all history rows (old clients)", len(json.loads(full)["history"]) == 720)
last = now - 7200 + 715 * 10
_, _, part = get(f"status.json?since={last}")
j = json.loads(part)
check("status.json?since=: only the newer rows", [r["epoch"] for r in j["history"]] == [now - 7200 + i * 10 for i in range(716, 720)]
      and j.get("history_since") == last)
check("a poll is small (< 8 kB unzipped here)", len(part) < 8000, f"{len(part)} B, full {len(full)} B")
c, h, z = get(f"status.json?since={last}", {"Accept-Encoding": "gzip"})
check("JSON gzipped when the browser accepts it", h.get("Content-Encoding") == "gzip" and json.loads(gzip.decompress(z))["history"] == j["history"],
      f"{len(z)} B")
mod.recent_events.append({"type": "warm", "epoch": now - 60, "what": "start"})
_, _, b = get(f"status.json?since={last}")
j1 = json.loads(b)
check("since without tv: all slow parts and their versions", "events" in j1 and "labels" in j1 and j1.get("tv"))
_, _, b = get(f"status.json?since={last}&tv={j1['tv']}")
j2 = json.loads(b)
check("same tv: slow parts left out, the rest sent", not any(k in j2 for k in R.SLOW_PARTS) and j2["tv"] == j1["tv"]
      and len(j2["history"]) == 4 and "latest" in j2, f"{len(b)} B")
mod.recent_events.append({"type": "warm", "epoch": now - 30, "what": "stop"})
_, _, b = get(f"status.json?since={last}&tv={j1['tv']}")
j3 = json.loads(b)
check("events changed: only events sent again", "events" in j3 and len(j3["events"]) == 2 and "labels" not in j3
      and j3["tv"] != j1["tv"])
check("without since: everything, no tv (old clients)", "events" in json.loads(get(f"status.json?tv={j1['tv']}")[2]))
# hot keys: a changing status line / data rate sends only those keys, not the whole part with its lists
import types
WARM = {"enabled": True, "status": {"code": "low", "text": "power low", "t": now, "p": {"left": 300}},
        "plan": [{"a": now + i * 3600, "b": now + i * 3600 + 1800, "name": "p%d" % i} for i in range(40)], "rest_since": None}
ASH = {"turn_hist": [[now - i * 600, 6.0] for i in range(300)], "edges": [], "rate_s": 1.0, "out_s": 0, "turns_24h": 5}
WX = {"enabled": True, "hourly": [[now + i * 3600, 4.5] for i in range(48)], "age_min": 1}
mod.warm, mod.ash, mod.weather = (types.SimpleNamespace(feed=lambda d=d: json.loads(json.dumps(d))) for d in (WARM, ASH, WX))
mod.latest = {"epoch": now, "t": "x", "fields": {"state": 4, "boiler_c": 70.1}, "raw": {"000": 4, "001": 70.1}}
_, _, b = get(f"status.json?since={now}")
k1 = json.loads(b)
check("hot parts: tv has the parts' versions and then one per hot part",
      len(k1["tv"].split("-")) == len(R.SLOW_PARTS) + len(R.HOT_KEYS) and "plan" in k1["warm"] and "hot" not in k1)
check("since: latest without the raw copy (pages read latest.fields)", "raw" not in k1["latest"] and k1["latest"]["fields"])
_, _, b = get(f"status.json?since={now}&tv={k1['tv']}")
k2 = json.loads(b)
check("nothing changed: no slow part and no hot keys", "warm" not in k2 and "hot" not in k2 and k2["tv"] == k1["tv"])
WARM["status"] = {"code": "low", "text": "power low", "t": now + 10, "p": {"left": 290}}
ASH["rate_s"] = 1.1
_, _, b = get(f"status.json?since={now}&tv={k1['tv']}", {"Accept-Encoding": "gzip"})
k3 = json.loads(gzip.decompress(b))
check("status / rate changed: only the hot keys sent, the lists stay with the page",
      "warm" not in k3 and "ash_plate" not in k3 and "weather" not in k3
      and k3["hot"] == {"warm": {"status": WARM["status"], "rest_since": None},
                        "ash_plate": {"rate_s": 1.1, "out_s": 0, "turns_24h": 5}}, k3.get("hot"))
check("such a poll stays small (< 1.5 kB gzip)", len(b) < 1500, f"{len(b)} B")
WARM["plan"] = WARM["plan"][1:]
_, _, b = get(f"status.json?since={now}&tv={k3['tv']}")
k4 = json.loads(b)
check("the plan changed: the whole warm part again, no hot copy for it", "plan" in k4.get("warm", {})
      and "warm" not in k4.get("hot", {}))
for name, var in (("home.html", "SLOW"), ("dashboard.html", "liveSlow")):
    src = open(os.path.join(HERE, name), encoding="utf-8").read()
    check(f"{name}: merges the hot keys into its copy of the part",
          f"if(j.hot&&j.hot[k]) {var}[k]=Object.assign({{}},{var}[k],j.hot[k])" in src)
mod.warm = mod.ash = mod.weather = None

c, h, b = get("history.json?h=1&since=" + str(now))
check("history.json?since= answers (no rows newer than now)", c == 200 and json.loads(b)["rows"] == [])

c, h, b = get("classic", {"Accept-Encoding": "gzip"})
et = h.get("ETag")
check("page gzipped with an ETag", c == 200 and h.get("Content-Encoding") == "gzip" and et and len(b) < 100000,
      f"{len(b)} B")
c, h, b = get("classic", {"If-None-Match": et, "Accept-Encoding": "gzip"})
check("reload with the same ETag: 304, no body", c == 304 and b == b"")
c, h, b = get("classic")
check("plain request still gets the plain page", c == 200 and b"<title>" in b and "Content-Encoding" not in h)

# the pages: every repeating timer skips its work while the page is hidden; status polls ask with since=
for name in ("home.html", "dashboard.html"):
    src = open(os.path.join(HERE, name), encoding="utf-8").read()
    ivs = re.findall(r"setInterval\((.*?),\s*[\w*]+\);", src)
    bad = [x for x in ivs if "document.hidden" not in x and x.strip() not in ("poll",)]
    check(f"{name}: every setInterval pauses while hidden", ivs and not bad, bad)
    check(f"{name}: a visibilitychange catches up on return", 'addEventListener("visibilitychange"' in src)
    js = "[" + ",".join(f'"{k}"' for k in R.SLOW_PARTS) + "].forEach"
    check(f"{name}: keeps the same slow parts as the server (SLOW_PARTS)", js in src)
    fetches = re.findall(r'fetch\("status\.json"([^,]*)', src)
    check(f"{name}: status.json is always asked with since=", fetches and all("since" in f for f in fetches), fetches)
dsrc = open(os.path.join(HERE, "dashboard.html"), encoding="utf-8").read()
check("dashboard.html: poll() returns while hidden", "demoTick(); return; }\n  if(document.hidden) return;" in dsrc)

print("ALL PASS" if not FAILED else f"{FAILED} FAILED")
sys.exit(1 if FAILED else 0)
