"""Offline test of scada/insights_gen.py on synthetic data (scada/_synth.py): every card is built without an
exception and has an English and a Danish title and text, with one full day recorded and with three.
Each idea is called directly, because main() hides a broken idea (it only prints)."""
import os, sys, json, shutil, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _synth
import insights_gen as G

FAILED = 0
# Known defects, not fixed in this step (no behaviour change): "idea name/days". Remove the entry with the fix.
KNOWN_BUGS = {}
CHARTS = ("bar", "stack", "xy", "timeline")


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name)
    FAILED += 0 if cond else 1


def good(c):
    """A card the page can show: both languages filled in, a chart type the page knows, plain JSON."""
    json.dumps(c)
    return (bool(c["id"]) and all(isinstance(c[k][lang], str) and c[k][lang].strip() for k in ("title", "text") for lang in ("en", "da"))
            and (c["chart"] is None or c["chart"]["type"] in CHARTS))


TMP = tempfile.mkdtemp(prefix="insights_test_")
for days in (1, 3):
    G.DATA = _synth.make_data(os.path.join(TMP, f"d{days}"), days=days)
    G.OUT = os.path.join(TMP, f"insights{days}.json")
    rows = G.rows_best()
    check(f"[{days} d] rows loaded", len(rows) == days * 8640)
    cards = {}
    for fn in G.IDEAS:
        key = f"{fn.__name__}/{days}"
        try:
            c = fn(rows)
            ok, why = c is not None and good(c), "" if c is not None else " (no card)"
            if ok:
                cards[c["id"]] = c
        except Exception as e:
            ok, why = False, f" ({e!r})"
        if key in KNOWN_BUGS:
            if ok:
                check(f"[{days} d] {fn.__name__}: works now - remove it from KNOWN_BUGS", False)
            else:
                print(f"XFAIL [{days} d] {fn.__name__}: known bug, {KNOWN_BUGS[key]}{why}")
        else:
            check(f"[{days} d] {fn.__name__}: card in en + da{why}", ok)
    # time to flame = from the start of IGNITION (state 3) until flame > 17 (owner decision)
    want = [round(_synth.flame_delay(d, b) / 60, 1) for d in range(days) for b in (0, 1)]
    got = (cards.get("ign_minutes") or {"chart": {"series": [{"data": None}]}})["chart"]["series"][0]["data"]
    check(f"[{days} d] minutes to flame counted from ignition start to flame > 17", got == want)
    G.main()
    out = json.load(open(G.OUT, encoding="utf-8"))
    check(f"[{days} d] main() writes insights.json with the same cards",
          sorted(c["id"] for c in out["cards"]) == sorted(cards) and out["rows"] == len(rows))

    # the two cards the home page draws its own way (Plan tab) carry their numbers as "data"
    wb, pp = (cards.get("warm_by") or {}).get("data"), (cards.get("pellets_period") or {}).get("data")
    check(f"[{days} d] warm in time: one entry per 'warm by' start (at most 7) with the asked time",
          bool(wb) and len(wb["starts"]) == min(days, 7) and wb["made"] == sum(1 for x in wb["starts"] if x["ok"])
          and all(set(x) == {"goal", "early", "took", "ok"} and x["ok"] == (x["early"] is not None and x["early"] >= 0) for x in wb["starts"])
          and [x["goal"] for x in wb["starts"]] == [(_synth.DAY0.replace(hour=7) + __import__("datetime").timedelta(days=k)).timestamp() for k in range(days)])
    if pp is not None:
        ch = cards["pellets_period"]["chart"]
        check(f"[{days} d] pellets per period: kg a day per period, the same periods as the chart",
              pp["days"] == len(ch["labels"]) and [p["name"] or "Boiler on its own" for p in pp["periods"]] == [s["name"]["en"] for s in ch["series"]]
              and all(abs(p["kg"] - sum(s["data"]) / pp["days"]) < 0.11 for p, s in zip(pp["periods"], ch["series"]))
              and 98 <= sum(p["share"] for p in pp["periods"]) <= 102)
    # the last start, step by step: chart rows and text list the same steps, in time order (first step on top)
    sc = cards.get("substeps")
    if sc:
        names = [r["en"] for r in sc["chart"]["rows"]]
        firsts = []
        for a, b, i in sorted(sc["chart"]["segments"]):
            if i not in firsts:
                firsts.append(i)
        check(f"[{days} d] step by step: chart rows in time order", firsts == list(range(len(names))))
        import re
        listed = re.findall(r"(?:^|, )(.+?) \d+\.\d min", sc["text"]["en"].split(" (the chart")[0].split(". Time to flame")[0].split(". These")[0])
        check(f"[{days} d] step by step: text lists the chart's steps in the same order", listed == names)
        # time to flame in the text = the start record's (the one definition History uses)
        tf = re.search(r"Time to flame (\d+\.\d) min", sc["text"]["en"])
        recs = [r for r in G.F.ignition_records(G.rows_best()) if G.F.flame_min(r) is not None]
        check(f"[{days} d] step by step: time to flame ({tf and tf.group(1)} min) as in the start records",
              tf is not None and bool(recs) and float(tf.group(1)) == round(G.F.flame_min(recs[-1]), 1))

# "Minutes to flame" uses the server's start list (ignitions.jsonl `flame_s`, from the reset date on), not the rows
d = os.path.join(TMP, "d1")
G.DATA = d
rows = G.rows_best()
e = rows[-1]["epoch"] - 3 * 86400
recs = [{"epoch": e - 100, "result": "ok", "flame_s": 60},                       # before the reset: not counted
        {"epoch": e + 100, "result": "ok", "flame_s": 240},
        {"epoch": e + 200, "result": "ok", "flame_s": 300},
        {"epoch": e + 300, "result": "failed", "flame_s": None},                # a real miss
        {"epoch": e + 400, "result": "ok", "flame_s": None, "peak_lux": 140},   # flame already on: not a miss
        {"epoch": e + 500, "result": "stopped", "relight": True, "flame_s": 0},  # relight, flame still on
        {"epoch": e + 600, "result": "ok", "flame_s": 360}]
with open(os.path.join(d, "ignitions.jsonl"), "w", encoding="utf-8") as fh:
    fh.write("".join(json.dumps(r) + "\n" for r in recs))
st = json.load(open(os.path.join(d, "settings.json"), encoding="utf-8"))
st["ignitions"] = {"since": e}
json.dump(st, open(os.path.join(d, "settings.json"), "w", encoding="utf-8"))
c = G.idea_ignition_times(rows)
check("minutes to flame: server start list, flame-on starts not counted as misses",
      c["text"]["en"].startswith("3 of 4 ignitions found a flame; typical 5.0 min.")
      and c["chart"]["series"][0]["data"] == [4.0, 5.0, 6.0])

# "Where the hours go": duplicate / overlapping rows (monitor restarts) and runs past midnight never give a day > 24 h
import datetime
t0 = datetime.datetime(2000, 1, 1).timestamp()
rows = [{"epoch": t0 + s, "state": 4} for s in range(0, 2 * 86400, 10)]            # two full days of RUNNING
rows += [{"epoch": t0 + s, "state": 4} for s in range(20 * 3600, 30 * 3600, 10)]   # a restart wrote the same 10 h again
rows += [{"epoch": t0 + s + 5, "state": 0} for s in range(22 * 3600, 26 * 3600, 10)]  # and an overlapping copy, 5 s apart
rows = [r for r in rows if not 40 * 3600 <= r["epoch"] - t0 < 42 * 3600]          # a 2-hour gap: not counted
rows.sort(key=lambda r: r["epoch"])
c = G.idea_time_per_state(rows)
tot = [round(sum(s["data"][i] for s in c["chart"]["series"]), 2) for i in range(len(c["chart"]["labels"]))]
check(f"hours per day: duplicates/overlaps counted once, split at midnight, gap left out ({tot})",
      c["chart"]["labels"] == ["01/01", "02/01"] and abs(tot[0] - 24) < 0.01 and abs(tot[1] - 22) < 0.02)

shutil.rmtree(TMP, ignore_errors=True)
print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
