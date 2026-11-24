"""Odd input to the settings must give a clear "no" (HTTP 400) and leave settings.json strict JSON: never a closed
connection, a traceback or a value taken by accident. One check per case found by a fuzz run:
Infinity / NaN, numbers too big for a whole number, a period list that is an object, a day like "²", JSON nested too
deep, "0x10" / "1e3" typed on the page, and "false" / "4" given where a switch or a list belongs.

The HTTP part runs the real program on synthetic data (a replay: nothing is sent anywhere). The page part needs
Playwright; without it only that part is skipped."""
import http.client, json, math, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import _synth
import burner_common as C
import burner_features as F

FAILED = 0
PORT = 8818


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    FAILED += 0 if cond else 1


def strict_file(path):
    """settings.json is strict JSON (no Infinity / NaN)."""
    def bad(s):
        raise ValueError(s)
    try:
        with open(path, encoding="utf-8") as fh:
            json.load(fh, parse_constant=bad)
        return True
    except ValueError:
        return False


# ---- helpers in burner_common
check("to_int: Infinity is no whole number (no OverflowError)", C.to_int(float("inf")) is None and C.to_int(1e999, 7) == 7)
check("to_int: NaN is no whole number", C.to_int(float("nan")) is None)
check("to_float: Infinity / NaN / '1e999' give None", C.to_float(float("inf")) is None and C.to_float("nan") is None
      and C.to_float("1e999") is None and C.to_float("2.5") == 2.5)
for raw, why in ((b'{"a":Infinity}', "Infinity"), (b'{"a":NaN}', "NaN"), (b'{"a":-Infinity}', "-Infinity"),
                 (b'{"a":1e999}', "1e999"), (b"[" * 5000, "nested too deep"), (b"[" * 5000 + b"]" * 5000, "deep but closed")):
    try:
        C.strict_json_loads(raw)
        ok = False
    except ValueError:
        ok = True
    except Exception:
        ok = False
    check(f"strict JSON refuses {why} with ValueError", ok)
check("strict JSON still reads normal numbers", C.strict_json_loads(b'{"a":1.5,"b":-2,"c":[1e3]}') == {"a": 1.5, "b": -2, "c": [1000.0]})

# ---- Settings.update: each case is refused, nothing changes, the file stays strict JSON
d = tempfile.mkdtemp()
try:
    log = []
    st = F.Settings(d, log.append)
    path = os.path.join(d, "settings.json")
    before = json.dumps(st.all(), sort_keys=True)

    def refused(name, patch):
        try:
            ok, msg = st.update(patch)
        except Exception as e:
            check(f"{name}: refused (raised {e!r})", False)
            return
        check(f"{name}: refused with a message ({msg})", not ok and isinstance(msg, str) and msg)

    # 9 non-finite numbers
    refused("setup.store_kg Infinity", {"setup": {"store_kg": float("inf")}})
    refused("ash.bin_kg NaN", {"ash": {"bin_kg": float("nan")}})
    refused("weather.lat Infinity", {"weather": {"lat": float("inf"), "lon": 10}})
    st.update({"setup": {"at": float("inf")}})
    st.update({"setup": {"at": float("nan")}})
    check("setup.at Infinity / NaN is not kept", st.get("setup")["at"] is None or math.isfinite(st.get("setup")["at"]))
    # 10 non-finite numbers in whole-number settings (OverflowError before)
    for sect, key in (("schedule", "min_off_min"), ("auto_reset", "wait_min"), ("ntfy", "silo_low_kg"),
                      ("controller_info", "min_pct"), ("pellets", "reserve_kg"), ("warm", "stop_pct"), ("lux", "low_abs")):
        refused(f"{sect}.{key} = 1e999", {sect: {key: float("inf")}})
    ok, _ = st.update({"auto_reset": {"alarms": [4, float("inf")]}})
    check("auto_reset.alarms: an Infinity item is left out, no error", ok and st.get("auto_reset")["alarms"] == [4])
    good_period = {"start": "06:00", "end": "07:00", "days": [0], "kind": "start"}
    refused("period stop_pct = 1e999", {"warm": {"periods": [dict(good_period, stop_pct=float("inf"))]}})
    # 11 periods / windows / boosts must be lists
    refused("warm.periods as an object", {"warm": {"periods": {"a": 1}}})
    refused("warm.periods as a string", {"warm": {"periods": "abc"}})
    refused("warm.boosts as a string", {"warm": {"boosts": "06:00"}})
    refused("schedule.windows item not an object", {"schedule": {"windows": ["06:00"]}})
    refused("a section that is not an object", {"warm": [1, 2]})
    # 12 period days that are not numbers
    refused("period day '²' only", {"warm": {"periods": [dict(good_period, days=["²"])]}})
    ok, _ = st.update({"warm": {"periods": [dict(good_period, days=["²", "1", 2, True])]}})
    check("period days: '²' and true left out, '1' and 2 kept", ok and st.get("warm")["periods"][0]["days"] == [1, 2])
    refused("period days as a string", {"warm": {"periods": [dict(good_period, days="12")]}})
    refused("window days as a string", {"schedule": {"windows": [{"start": "06:00", "end": "07:00", "days": "12"}]}})
    st.update({"warm": {"periods": []}})
    # 15 real booleans and lists only
    refused("schedule.enabled 'false'", {"schedule": {"enabled": "false"}})
    check("... the schedule stays off", st.get("schedule")["enabled"] is False)
    refused("warm.enabled 1", {"warm": {"enabled": 1}})
    refused("vvb.enabled 'true'", {"vvb": {"enabled": "true"}})
    refused("ntfy event switch 'false'", {"ntfy": {"events": {k: "false" for k, _ in F.NTFY_EVENTS[:1]}}})
    refused("auto_reset.alarms '4'", {"auto_reset": {"alarms": "4"}})
    refused("auto_reset.alarms an object", {"auto_reset": {"alarms": {"4": 1}}})
    refused("ash.emptied 'false'", {"ash": {"emptied": "false"}})
    refused("period on 'no'", {"warm": {"periods": [dict(good_period, on="no")]}})
    ok, _ = st.update({"schedule": {"enabled": True}, "auto_reset": {"alarms": [4]}})
    check("real true and a real list are still taken", ok and st.get("schedule")["enabled"] is True and st.get("auto_reset")["alarms"] == [4])
    st.update({"schedule": {"enabled": False}})
    check("settings.json is strict JSON after all of these", strict_file(path))

    # a file that already holds Infinity (written by an older version): read without it, saved strict again
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    obj = json.loads(raw)
    obj["setup"]["at"] = float("inf")
    obj["ash"]["emptied_at"] = float("nan")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)                               # allow_nan: writes Infinity / NaN
    st2 = F.Settings(d, log.append)
    check("old file with Infinity: read without it", st2.get("setup")["at"] is None and st2.get("ash").get("emptied_at") is None)
    check("old file with Infinity: /api/settings answer is strict JSON", "Infinity" not in json.dumps(st2.public())
          and "NaN" not in json.dumps(st2.public()))
    st2.update({"schedule": {"enabled": False}})
    check("old file with Infinity: saved strict again", strict_file(path))
finally:
    shutil.rmtree(d, ignore_errors=True)


# ---- the running program: every bad body gets a JSON answer (400), never a closed connection
def post(port, body, path="/api/settings"):
    """(status, answer dict or None); status None = no answer (connection closed)."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        c.request("POST", path, body=body, headers={"X-Burner": "1", "Content-Type": "application/json",
                                                     "Host": f"127.0.0.1:{port}"})
        r = c.getresponse()
        data = r.read()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, None
    except (OSError, http.client.HTTPException):
        return None, None
    finally:
        c.close()


BAD = [
    ("9 setup.at Infinity", b'{"setup":{"at":Infinity}}'),
    ("9 setup.at NaN", b'{"setup":{"at":NaN}}'),
    ("10 schedule.min_off_min 1e999", b'{"schedule":{"min_off_min":1e999}}'),
    ("10 auto_reset.wait_min 1e999", b'{"auto_reset":{"wait_min":1e999}}'),
    ("10 pellets.reserve_kg -1e999", b'{"pellets":{"reserve_kg":-1e999}}'),
    ("11 warm.periods object", b'{"warm":{"periods":{"a":1}}}'),
    ("12 period day superscript two", json.dumps({"warm": {"periods": [{"start": "06:00", "end": "07:00", "days": ["²"], "kind": "start"}]}}).encode()),
    ("13 JSON nested 5000 deep", b"[" * 5000),
    ("13 JSON nested 5000 deep, closed", b"[" * 5000 + b"]" * 5000),
    ("15 schedule.enabled 'false'", b'{"schedule":{"enabled":"false"}}'),
    ("15 auto_reset.alarms '4'", b'{"auto_reset":{"alarms":"4"}}'),
]
with _synth.Replay(PORT) as rp:
    s0 = rp.get("/api/settings")["settings"]
    for name, body in BAD:
        code, ans = post(rp.port, body)
        check(f"HTTP {name}: answer 400 with a message", code == 400 and isinstance(ans, dict) and ans.get("ok") is False
              and ans.get("msg"))
    code, ans = post(rp.port, b"[" * 5000, "/api/pellets/stock")
    check("HTTP deep JSON to another call: answer 400", code == 400)
    import urllib.request
    with urllib.request.urlopen(rp.base + "/api/settings", timeout=20) as r:
        s = C.strict_json_loads(r.read())["settings"]
    check("HTTP: after all of these /api/settings is strict JSON and unchanged", s == s0)
    code, ans = post(rp.port, b'{"pellets":{"reserve_kg":150}}')
    check("HTTP: a good value is still saved", code == 200 and ans["settings"]["pellets"]["reserve_kg"] == 150)

    # ---- 14 the page: a typed number is plain digits only
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: page part (Playwright is not installed)")
        print("FAILED:", FAILED)
        sys.exit(1 if FAILED else 0)
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        pg = br.new_page()
        pg.goto(rp.base + "/home", wait_until="domcontentloaded")
        pg.wait_for_function("typeof fCheck==='function' && typeof numTyped==='function'")
        for typed, want in (("0x10", None), ("1e3", None), ("+5", None), ("Infinity", None), ("5 ", 5), ("12", 12),
                            ("007", 7), ("", None)):
            r = pg.evaluate("s=>fCheck('warm.pause_h',s)", typed)
            got = r.get("v") if not r.get("err") else None
            check(f"14 page: pause_h typed {typed!r} -> {want}", got == want and (want is not None or r.get("err")))
        r = pg.evaluate("s=>fCheck('ash.bin_kg',s)", "1,5")
        check("14 page: bin_kg '1,5' (Danish comma) -> 1.5", r.get("v") == 1.5)
        r = pg.evaluate("s=>fCheck('ash.bin_kg',s)", "0x10")
        check("14 page: bin_kg '0x10' refused", bool(r.get("err")))
        r = pg.evaluate("ctlCheck(ctlRead({querySelector:s=>({value:{'[data-ci=\"sw\"]':'','[data-ci=\"min_pct\"]':'1e1','[data-ci=\"fan100_rpm\"]':''}[s]||''})}))")
        check("14 page: service numbers '1e1' refused", r == "min_pct")
        # 17 /classic: same strict rule, and a bad field is not sent
        pg.goto(rp.base + "/classic", wait_until="domcontentloaded")
        pg.wait_for_function("typeof numTyped==='function' && typeof saveAutoReset==='function' && typeof switchTab==='function'")
        pg.evaluate("switchTab('sch')")
        pg.wait_for_function("typeof AR!=='undefined' && !!AR")
        got = pg.evaluate("['0x10','1e3','+5','Infinity','5 ','1,5','007'].map(numTyped)")
        check("17 classic: numTyped like /home",
              all(v is None or v != v for v in got[:4]) and got[4:] == [5, 1.5, 7])
        pg.evaluate("()=>{ document.getElementById('arWait').value='1e3'; return saveAutoReset(); }")
        msg = pg.evaluate("document.getElementById('arMsg').textContent")
        check("17 classic: '1e3' in an auto-reset field is refused on the page", msg == pg.evaluate("t('num_bad')"))
        with urllib.request.urlopen(rp.base + "/api/settings", timeout=20) as r:
            s2 = C.strict_json_loads(r.read())["settings"]
        check("17 classic: nothing saved", s2["auto_reset"] == s["auto_reset"])
        # 22 no data: blame this device only when it is offline; no reply at all says both; an error answer = monitor
        js = "[noAnswerKey(new TypeError('x')), noAnswerKey(new DOMException('t','AbortError')), noAnswerKey(0), noAnswerKey(new SyntaxError('j'))]"
        for page in ("/classic", "/home"):
            if page != "/classic":
                pg.goto(rp.base + page, wait_until="domcontentloaded")
            pg.wait_for_function("typeof noAnswerKey==='function'")
            check(f"22 {page}: online reasons", pg.evaluate(js) == ["nodata_noreply", "nodata_noreply", "nodata_resp", "nodata_resp"])
            pg.context.set_offline(True)
            check(f"22 {page}: offline -> this device has no network",
                  pg.evaluate("[noAnswerKey(new TypeError('x')), t('nodata_net')]")[0] == "nodata_net"
                  and "network" in pg.evaluate("t('nodata_net')"))
            pg.context.set_offline(False)
        # 23 slow Wi-Fi: one status poll at a time (pg is on /home now)
        n = pg.evaluate("""async()=>{ const f0=window.fetch; let n=0;
            window.fetch=(u,o)=>{ if(String(u).startsWith('status.json')){ n++; return new Promise(r=>setTimeout(()=>r(f0(u,o)),600)); } return f0(u,o); };
            await new Promise(r=>setTimeout(r,700)); n=0;
            const a=pollStatus(true); pollStatus(true); pollStatus(true); await a; window.fetch=f0; return n; }""")
        check("23 /home: timer polls skipped while one is waiting", n == 1)
        n = pg.evaluate("""async()=>{ const f0=window.fetch; let n=0;
            window.fetch=(u,o)=>{ if(String(u).startsWith('status.json')){ n++; return new Promise(r=>setTimeout(()=>r(f0(u,o)),300)); } return f0(u,o); };
            pollStatus(true); await pollStatus(); window.fetch=f0; return n; }""")
        check("23 /home: a call after a save waits, then asks once more", n == 2)
        # 32 no "-0.0°": round first, then a zero has no minus (/home nf and fmt; /classic uses the same fmt)
        got = pg.evaluate("[nf(-0.04,1), nf(-0.0,1), nf(-0.4,0), nf(-0.06,1), fmt(-0.04,1)].map(s=>s.replace(',','.'))")
        check("32 /home: -0.04 -> 0.0, -0.06 -> -0.1", got == ["0.0", "0.0", "0", "-0.1", "0.0"])
        br.close()

print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
