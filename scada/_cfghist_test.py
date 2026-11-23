"""Boiler settings change history (data/cfg_changes.jsonl): one row per changed field when a settings read differs
from the one before; the first read after a restart is compared with the last stored values (last_cfg.json), so a
restart neither hides a change nor makes one up; cut lines and NUL bytes in the files are tolerated; status.json
carries the last changes and /classic shows them in plain words (EN + DA). Made-up values only."""
import json, os, re, shutil, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import burner_common as C
import burner_ext as E
import burner_responder as R

fails = 0


def check(name, ok, info=""):
    global fails
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": {info}"))


BASE = {f"{i:03d}": 10 + i for i in range(42)}
BASE.update({"011": 0, "014": 60, "015": 83, "017": 600, "018": 1, "033": 0, "036": 0.005})
d = tempfile.mkdtemp(prefix="cfghist_")
path, last = os.path.join(d, "cfg_changes.jsonl"), os.path.join(d, "last_cfg.json")
logs = []
try:
    h = E.CfgHistory(d, logs.append)
    check("first read ever: kept as the start, nothing logged", h.on_cfg(dict(BASE), 1000) == [] and not os.path.exists(path)
          and os.path.exists(last))
    check("same read again: nothing logged", h.on_cfg(dict(BASE), 2000) == [] and not os.path.exists(path))
    b = dict(BASE, **{"011": 1})
    check("field 011 (flips during ignition) is not a setting", h.on_cfg(b, 2500) == [])
    mt = os.path.getmtime(last)
    b = dict(BASE, **{"014": 40, "015": 80, "017": 800, "018": 20, "033": 1})
    rows = h.on_cfg(b, 3000)
    check("type change: one row per changed field", [(r["field"], r["old"], r["new"]) for r in rows]
          == [("014", 60, 40), ("015", 83, 80), ("017", 600, 800), ("018", 1, 20), ("033", 0, 1)], rows)
    check("rows have time, field, old, new", all(set(r) == {"time", "field", "old", "new"} and r["time"] == 3000 for r in rows))
    check("rows are on disk", [r["field"] for r in C.rows_of(path)] == ["014", "015", "017", "018", "033"])
    check("log line names the changes", any("017 600 -> 800" in m for m in logs), logs)
    check("a read with fewer fields: the missing ones are not 'changed'", h.on_cfg({"000": 10}, 3500) == []
          and h.last["017"] == 800)

    # ---- restart: the stored values are the start, a change made while off is seen once
    h2 = E.CfgHistory(d, logs.append)
    check("restart: last changes reloaded", [r["field"] for r in h2.feed()] == ["014", "015", "017", "018", "033"])
    check("restart, same values: no fake change", h2.on_cfg(dict(b), 4000) == [] and len(C.rows_of(path)) == 5)
    h3 = E.CfgHistory(d, logs.append)
    rows = h3.on_cfg(dict(b, **{"000": 65}), 5000)
    check("restart, value changed while off: seen on the first read", [(r["field"], r["old"], r["new"]) for r in rows] == [("000", 10, 65)])
    h4 = E.CfgHistory(d, logs.append)
    check("and not again after the next restart", h4.on_cfg(dict(b, **{"000": 65}), 6000) == [] and len(C.rows_of(path)) == 6)

    # ---- power cut: a cut last line + NUL bytes in the history, a broken last_cfg.json (the .bak is used)
    with open(path, "ab") as fh:
        fh.write(b'{"time": 7000, "field": "00' + b"\x00" * 40)
    with open(last, "wb") as fh:
        fh.write(b"\x00" * 30)
    logs.clear()
    h5 = E.CfgHistory(d, logs.append)
    check("cut line / NUL bytes skipped on load", len(h5.feed()) == 6 and h5.feed()[-1]["field"] == "000")
    check("broken last_cfg.json: the backup is used, no fake change", h5.last and h5.on_cfg(dict(b, **{"000": 65}), 7000) == [],
          logs)
    rows = h5.on_cfg(dict(b, **{"000": 65, "019": 5}), 7100)
    check("next change after the cut line goes on a line of its own",
          [(r["field"], r["new"]) for r in rows] == [("019", 5)] and C.rows_of(path)[-1] == rows[0], C.rows_of(path)[-2:])
    os.remove(last); os.remove(last + ".bak")
    h6 = E.CfgHistory(d, logs.append)
    check("no stored values at all: the first read is only the start", h6.on_cfg(dict(BASE), 8000) == [])
    for i in range(15):
        h6.on_cfg(dict(BASE, **{"002": 100 + i}), 9000 + i)
    check("pages get the last 10 changes", len(h6.feed()) == 10 and h6.feed()[-1]["new"] == 114)

    # ---- through the monitor: a cfg read reaches the history and status.json
    mod = R.Module(0x01)
    mod.cfg_hist = E.CfgHistory(d, lambda m: None)
    mod.ingest_json('{"cfg_data_1":...}', {"cfg_data_1": dict(BASE, **{"002": 99})})
    j = mod.feed_snapshot()
    check("status.json has cfg_changes", j.get("cfg_changes") and j["cfg_changes"][-1]["field"] == "002"
          and j["cfg_changes"][-1]["new"] == 99, j.get("cfg_changes"))
    check("cfg_changes is a slow part (only sent when it changed)", "cfg_changes" in R.SLOW_PARTS)
    mod2 = R.Module(0x01)
    check("no data folder: no history, nothing breaks", (mod2.ingest_json("cfg_data", {"cfg_data_1": dict(BASE)}) or True)
          and "cfg_changes" not in mod2.feed_snapshot())
finally:
    shutil.rmtree(d, ignore_errors=True)

# ---- /classic Analysis: a card with the changes in plain words, EN and DA
src = open(os.path.join(HERE, "dashboard.html"), encoding="utf-8").read()
for lang in ("en", "da"):
    block = src.split(f"\n  {lang}: {{", 1)[1][:60000]
    check(f"/classic {lang}: words for the changes card", all(re.search(r"\b%s:" % k, block) for k in
                                                              ("cc_title", "cc_none", "cc_row", "cc_note", "cc_fan")))
check("/classic: the card and its render", 'id="ccList"' in src and "function renderCfgChanges" in src
      and "renderCfgChanges();" in src.split("function setLang", 1)[1][:600])
check("/classic: field names in EN and DA", '"017":"Ignition timeout (s)"' in src and '"017":"Optænding timeout (s)"' in src)

print("all ok" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
