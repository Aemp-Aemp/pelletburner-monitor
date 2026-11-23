"""Full disk (simulated: a write stops half-way with 'No space left on device'): no record is glued to a half line,
'write failed' is logged once per outage with a count and 'writes work again' when space is back, status.json has
"disk" only while writes fail, /home has the notice (EN + DA), and the replay tool keeps running."""
import errno, json, os, re, shutil, sys, tempfile, time, types
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import burner_common as C
import burner_ext as E

fails = 0
def check(name, ok, info=""):
    global fails
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": {info}"))


FULL = [False]
_open = open


class Half:
    """A file on a full disk: about half of each write lands, then ENOSPC."""
    def __init__(self, fh): self._fh = fh
    def write(self, s):
        if not FULL[0]:
            return self._fh.write(s)
        self._fh.write(s[: len(s) // 2])
        self._fh.flush()
        raise OSError(errno.ENOSPC, "No space left on device")
    def __enter__(self): return self
    def __exit__(self, *a):
        try:
            self._fh.close()
        except OSError:
            pass
    def __getattr__(self, k): return getattr(self._fh, k)


def fopen(file, mode="r", *a, **k):
    fh = _open(file, mode, *a, **k)
    return Half(fh) if any(c in mode for c in "wax+") else fh


C.open = fopen                                   # burner_common's open() only (append_jsonl, Store, atomic writes)
d = tempfile.mkdtemp()
try:
    logs = []
    C.set_write_log(logs.append)
    now = time.time()

    # ---- append_jsonl: the half lines of the full period never glue the next records on
    p = os.path.join(d, "events-2000-01-01.jsonl")
    C.append_jsonl(p, {"epoch": 1, "v": 1})
    FULL[0] = True
    res = [C.append_jsonl(p, {"epoch": 2 + i, "v": "x" * 40}) for i in range(5)]
    check("full: append_jsonl says not written", res == [False] * 5, res)
    t = C.disk_trouble()
    check("full: disk_trouble counts the failed writes", t and t["n"] == 5 and t["min"] == 0, t)
    FULL[0] = False
    ok = [C.append_jsonl(p, {"epoch": 10 + i, "v": i}) for i in range(3)]
    rows = C.rows_of(p)
    check("space back: first record and every later one read (none glued to a half line)",
          ok == [True] * 3 and rows == [{"epoch": 1, "v": 1}] + [{"epoch": 10 + i, "v": i} for i in range(3)], rows)
    check("space back: disk_trouble is None", C.disk_trouble() is None, C.disk_trouble())
    fl = [x for x in logs if "write failed" in x]
    back = [x for x in logs if "writes work again" in x]
    check("one 'write failed' line for the outage, one 'writes work again' with the count",
          len(fl) == 1 and "No space" in fl[0] and len(back) == 1 and "5 failed writes" in back[0], logs)

    # ---- a cut line from DURING this run (not only from before the start) is ended before the next write
    q = os.path.join(d, "fan_samples.jsonl")
    C.append_jsonl(q, {"a": 1})
    C.append_jsonl(q, {"a": 2})                  # now in the 'checked' set
    FULL[0] = True
    C.append_jsonl(q, {"a": 3, "pad": "y" * 30})
    FULL[0] = False
    C.append_jsonl(q, {"a": 4})
    check("cut line made during the run: the next record still read", C.rows_of(q) == [{"a": 1}, {"a": 2}, {"a": 4}],
          C.rows_of(q))

    # ---- Store (hist / events / raw day files): same, and its log gets the lines once
    logs.clear()
    C.set_write_log(None)                        # Store's own log is used when the monitor has not set one
    slog = []
    s = E.Store(d, 30, slog.append)
    s.add_event({"epoch": now, "type": "a"})
    FULL[0] = True
    for i in range(50):
        s.add_event({"epoch": now + i, "type": "full", "pad": "z" * 20})
        s.add_row({"epoch": now + i, "v": i})
    FULL[0] = False
    s.add_event({"epoch": now + 60, "type": "b"})
    s.add_row({"epoch": now + 60, "v": 60})
    day = s._day(now)
    evs = C.rows_of(os.path.join(d, f"events-{day}.jsonl"))
    hist = C.rows_of(os.path.join(d, f"hist-{day}.jsonl"))
    check("Store: the event before and the first one after the full period are kept",
          [e["type"] for e in evs] == ["a", "b"], evs)
    check("Store: hist row after the full period read", hist == [{"epoch": now + 60, "v": 60}], hist)
    check("Store: 100 failed writes = one 'write failed' + one 'writes work again (100 ...)' line",
          len(slog) == 2 and "write failed" in slog[0] and "100 failed writes" in slog[1], slog)

    # ---- atomic JSON writes count too (settings, state files)
    FULL[0] = True
    try:
        C.write_json_atomic(os.path.join(d, "state.json"), {"a": 1})
        raised = False
    except OSError:
        raised = True
    check("atomic write on a full disk raises and counts", raised and C.disk_trouble() and C.disk_trouble()["n"] == 1,
          C.disk_trouble())
    FULL[0] = False
    C.write_json_atomic(os.path.join(d, "state.json"), {"a": 2})
    check("atomic write works again: outage over", C.disk_trouble() is None and C.read_json(os.path.join(d, "state.json")) == {"a": 2})

    # ---- status.json: "disk" only while writes fail
    import burner_responder as R
    rm = R.Module(0x01)
    check("status.json: no 'disk' when writes work", "disk" not in rm.feed_snapshot(since=0))
    FULL[0] = True
    C.append_jsonl(p, {"epoch": 99})
    snap = rm.feed_snapshot(since=0)
    check("status.json: 'disk' while writes fail (also in a since-reply)", snap.get("disk", {}).get("n") == 1, snap.get("disk"))
    FULL[0] = False
    C.append_jsonl(p, {"epoch": 100})
    check("status.json: 'disk' gone when writes work again", "disk" not in rm.feed_snapshot(since=0))
    del C.open

    # ---- /home: the notice, EN + DA, drawn from S.disk only
    with _open(os.path.join(ROOT, "scada", "home.html"), encoding="utf-8") as fh:
        html = fh.read()
    check("/home: two disk_fail texts (EN + DA) and the notice drawn from S.disk",
          len(re.findall(r"\bdisk_fail:\"", html)) == 2 and "SD" in html and 'id="diskLine"' in html
          and re.search(r"S&&S\.disk\?", html) and "renderDiskLine(document.getElementById(\"diskLine\"))" in html)

    # ---- replay tool: a full disk while copying pi-health lines does not stop the fake serial port
    import replay as RP
    work = os.path.join(d, "work")
    os.makedirs(work)
    sf = RP.SideFiles(os.path.join(d, "nosrc"), work, 0)
    sf.items = [(1.0, "pi-health-2000-01-01.jsonl", '{"epoch": 1}\n'), (2.0, "pi-health-2000-01-01.jsonl", '{"epoch": 2}\n')]
    import builtins
    real = builtins.open
    def full_open(f, mode="r", *a, **k):
        if "a" in mode:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(f, mode, *a, **k)
    builtins.open = full_open
    try:
        sf.pump(10.0)
        err = None
    except OSError as e:
        err = e
    finally:
        builtins.open = real
    check("replay SideFiles.pump on a full disk: no error, goes on", err is None and sf.i == 2, (err, sf.i))
    ph = os.path.join(work, "pi-health-2000-01-01.jsonl")
    with _open(ph, "w", encoding="utf-8") as fh:
        fh.write('{"epoch": 0}\n{"epo')              # the half line of the failed write
    sf.items.append((3.0, "pi-health-2000-01-01.jsonl", '{"epoch": 3}\n'))
    sf.last = 0
    sf.pump(20.0)
    check("replay SideFiles: the line after the full disk starts on a new line", C.rows_of(ph) == [{"epoch": 0}, {"epoch": 3}],
          C.rows_of(ph))
finally:
    C.__dict__.pop("open", None)
    C.set_write_log(None)
    shutil.rmtree(d, ignore_errors=True)

print("all ok" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
