"""Outside temperature: settings check, offline safety, parsing. No internet needed. Run: py scada\\_weather_test.py"""
import os, sys, json, time, tempfile, shutil
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F

log = lambda *a: None
fails = 0
def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails += 1

d = tempfile.mkdtemp()
try:
    st = F.Settings(d, log)
    check("off by default", st.get("weather")["enabled"] is False)
    ok, _ = st.update({"weather": {"enabled": True}})
    check("can't turn on without a place", not ok)
    ok, _ = st.update({"weather": {"enabled": True, "name": "Testby", "lat": 55.123456, "lon": 9.876543}})
    check("place saved", ok and st.get("weather")["lat"] == 55.12 and st.get("weather")["lon"] == 9.88)
    check("bad latitude refused", not st.update({"weather": {"lat": 123}})[0])
    check("country DE accepted", st.update({"weather": {"cc": "de"}})[0] and st.get("weather")["cc"] == "DE")
    check("other country refused", not st.update({"weather": {"cc": "SE"}})[0])
    orig = F.Weather._get
    F.Weather._get = staticmethod(lambda u, p, timeout=15: {"results": [
        {"name": "Ribe", "admin1": "X", "country": "Brasil", "country_code": "BR", "latitude": 1, "longitude": 2},
        {"name": "Ribe", "admin1": "Y", "country": "Deutschland", "country_code": "DE", "latitude": 50, "longitude": 9},
        {"name": "Ribe", "admin1": "Z", "country": "Danmark", "country_code": "DK", "latitude": 55.3, "longitude": 8.8}]})
    found = F.Weather.find("Ribe")
    check("search: only DK/DE, Denmark first", [x["cc"] for x in found] == ["DK", "DE"])
    F.Weather._get = orig
    check("place survives a restart", F.Settings(d, log).get("weather")["name"] == "Testby")

    w = F.Weather(st, d, log)
    now = int(time.time())
    fake = {"current": {"temperature_2m": 4.3},
            "hourly": {"time": [now - 3600 * i for i in range(3, 0, -1)] + [now + 3600 * i for i in range(1, 6)],
                       "temperature_2m": [6, 5, 4.5, 3, 2, -1.5, 0, 1]}}
    calls = []
    F.Weather._get = staticmethod(lambda url, params, timeout=15: calls.append(params) or fake)
    w.fetch(55.12, 9.88)
    f = w.feed()
    check("current temperature", f["c"] == 4.3)
    check("low / high next 24 h", f["low_24h"] == -1.5 and f["high_24h"] == 3)
    check("only rounded coordinates sent", calls[0]["latitude"] == 55.12 and "name" not in calls[0])
    check("reading saved to weather.jsonl", os.path.exists(os.path.join(d, "weather.jsonl")))
    check("cache survives a restart", F.Weather(st, d, log).feed()["c"] == 4.3)

    import datetime as _dt
    y0 = _dt.datetime.combine(_dt.date.today() - _dt.timedelta(days=1), _dt.time(0, 30)).timestamp()
    fake2 = {"current": {"temperature_2m": 5.0},
             "hourly": {"time": [int(y0 + 3600 * i) for i in range(24)], "temperature_2m": [2.0] * 12 + [6.0] * 12}}
    F.Weather._get = staticmethod(lambda url, params, timeout=15: fake2)
    w.fetch(55.12, 9.88)
    yday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    check("daily average kept (yesterday 4.0)", w.feed()["days"].get(yday) == 4.0)
    check("daily averages survive a restart", F.Weather(st, d, log).days().get(yday) == 4.0)

    def boom(*a, **k):
        raise OSError("no internet")
    F.Weather._get = staticmethod(boom)
    w2 = F.Weather(st, d, log)
    w2._fetch_safe(55.12, 9.88)
    f2 = w2.feed()
    check("offline: keeps the last reading", f2["c"] == 5.0 and f2["error"])
    said = []
    w3 = F.Weather(st, d, said.append)
    for _ in range(12):                                   # an hour of failed tries (every 5 min)
        w3._fetch_safe(55.12, 9.88)
    check("repeated failures logged once an hour", len(said) == 1 and "no internet" in said[0])
    w3.err_logged -= 3600
    w3._fetch_safe(55.12, 9.88)
    check("next failure line after an hour, with the count", len(said) == 2 and "13 failed tries" in said[1])
    F.Weather._get = staticmethod(lambda url, params, timeout=15: fake2)
    w3._fetch_safe(55.12, 9.88)
    check("recovery logged once", len(said) == 3 and "works again" in said[2] and w3.fails == 0)

    import burner_common as C
    t0 = _dt.datetime(2026, 11, 22, 5, 7, 9).timestamp()
    check("log line gets local date and time", C.log_stamp("# weather: x", t0) == "2026-11-22 05:07:09  # weather: x")
    check("frame lines keep their own time", C.log_stamp("05:07:09.123  IN  x", t0) == "05:07:09.123  IN  x")
    check("leading blank line stays in front", C.log_stamp("\n===== SUMMARY", t0) == "\n2026-11-22 05:07:09  ===== SUMMARY")
    check("empty line stays empty", C.log_stamp("", t0) == "")
    st.update({"weather": {"enabled": False}})
    check("off: feed says off", w2.feed() == {"enabled": False})
finally:
    shutil.rmtree(d, ignore_errors=True)
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
