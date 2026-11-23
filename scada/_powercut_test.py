"""Power cut: data files with NUL bytes and a half line are read without error and without losing the good lines;
the next record written after a cut starts on a new line."""
import json, os, shutil, sys, tempfile, time
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import burner_common as C
import burner_ext as E
import replay as R

fails = 0
def check(name, ok, info=""):
    global fails
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": {info}"))

d = tempfile.mkdtemp()
now = time.time()
good = [{"epoch": now - 300 + i, "v": i} for i in range(5)]
# good, good, half line + NUL run glued to the next good line, empty line, NUL-only line, good, good, half last line
broken = (json.dumps(good[0]) + "\n" + json.dumps(good[1]) + "\n"
          + '{"epoch": 1, "v"' + "\x00" * 312 + json.dumps(good[2]) + "\n"
          + "\n" + "\x00" * 40 + "\n"
          + json.dumps(good[3]) + "\n" + json.dumps(good[4]) + "\n"
          + '{"epoch": 9, "v": "\xe4')
try:
    p = os.path.join(d, "hist-2000-01-01.jsonl")
    with open(p, "wb") as fh:
        fh.write(broken.encode("utf-8")[:-1])          # cut inside a multi-byte character too
    try:
        rows = C.rows_of(p)
        check("read_jsonl: every good line read, no junk", rows == good, rows)
    except Exception as e:
        check("read_jsonl: no exception", False, repr(e))

    # append after the cut: the new record is on a line of its own
    C.append_jsonl(p, {"epoch": now, "v": 99})
    rows = C.rows_of(p)
    check("append_jsonl after a half line: new record read", rows[-1] == {"epoch": now, "v": 99}, rows[-2:])
    with open(p, encoding="utf-8", errors="replace") as fh:          # text mode: \r\n on Windows reads as \n
        check("append_jsonl: file ends with the new record on its own line",
              fh.read().endswith("\n" + json.dumps({"epoch": now, "v": 99}) + "\n"))

    # the monitor's own day files (Store._append)
    s = E.Store(d, 30, lambda *a: None)
    s.dir = d
    day = s._day(now)
    hp = os.path.join(d, f"hist-{day}.jsonl")
    with open(hp, "wb") as fh:
        fh.write(json.dumps(good[0]).encode() + b"\n" + b'{"epoch": 2, "st' + b"\x00" * 30)
    s.add_row({"epoch": now, "v": 7})
    check("Store append after a NUL run: both records read", C.rows_of(hp) == [good[0], {"epoch": now, "v": 7}],
          C.rows_of(hp))

    # open_append on an empty and a missing file: no extra newline
    ep = os.path.join(d, "empty.jsonl")
    open(ep, "w").close()
    C.append_jsonl(ep, {"a": 1})
    C.append_jsonl(os.path.join(d, "new.jsonl"), {"a": 1})
    check("append to empty / new file: no blank first line",
          open(ep).read() == '{"a": 1}\n' and open(os.path.join(d, "new.jsonl")).read() == '{"a": 1}\n')

    # pi-health: newest good line even when it is glued to a NUL run, and with a broken last line
    ph = os.path.join(d, f"pi-health-{day}.jsonl")
    with open(ph, "wb") as fh:
        fh.write(b'{"epoch": 5, "cp' + b"\x00" * 641 + json.dumps({"epoch": now, "cpu_c": 50}).encode() + b"\n"
                 + b'{"epoch": ' + str(now).encode() + b', "cpu')
    r = E.read_pi_health(d)
    check("read_pi_health: good line after a NUL run and before a cut line", r == {"epoch": now, "cpu_c": 50}, r)

    # replay: the copy is clean, the raw rows are read
    src, work = os.path.join(d, "src"), os.path.join(d, "work")
    os.makedirs(src)
    rp = os.path.join(src, "raw-2000-01-01.jsonl")
    with open(rp, "wb") as fh:
        fh.write(broken.encode("utf-8")[:-1])
    os.makedirs(work)
    R._cut_jsonl(rp, os.path.join(work, "raw-2000-01-01.jsonl"), now + 10)
    with open(os.path.join(work, "raw-2000-01-01.jsonl"), encoding="utf-8") as fh:
        txt = fh.read()
    check("replay copy: no NUL, only the good lines", "\x00" not in txt and [json.loads(x) for x in txt.splitlines()] == good,
          txt[:200])
    got = list(R.iter_raw_rows(src, 0, now + 10))
    check("replay raw rows: every good line", got == good, got)

    # JSON state files: written with fsync, previous good file kept as .bak, a broken file falls back to it
    import burner_features as F
    sd = os.path.join(d, "settings")
    os.makedirs(sd)
    synced, real_fsync = [], os.fsync
    os.fsync = lambda fd: synced.append(fd) or real_fsync(fd)
    try:
        st = F.Settings(sd, lambda m: None)
        ok, msg = st.update({"ash": {"ash_pct": 0.9}})
        check("settings saved with fsync", ok and synced, (ok, msg, synced))
    finally:
        os.fsync = real_fsync
    sp = os.path.join(sd, "settings.json")
    st.update({"ash": {"ash_pct": 1.1}})
    check("settings.json.bak = the previous good file", C.read_json(sp + ".bak", {}).get("ash", {}).get("ash_pct") == 0.9)
    for name, junk in (("NUL bytes", b"\x00" * 300), ("empty", b""), ("cut short", b'{"ash": {"capa')):
        with open(sp, "wb") as fh:
            fh.write(junk)
        logs = []
        st2 = F.Settings(sd, logs.append)
        check(f"settings.json {name}: user settings from .bak, one log line",
              st2.get("ash").get("ash_pct") == 0.9 and any(".bak" in x for x in logs), (st2.get("ash"), logs))
        st2.save()                                         # the broken file is not kept as the backup
        check(f"settings.json {name}: good .bak not overwritten by the broken file",
              C.read_json(sp + ".bak", {}).get("ash", {}).get("ash_pct") == 0.9)
    check("read_json: missing file and no .bak = default", C.read_json(os.path.join(d, "none.json"), 5) == 5)
finally:
    shutil.rmtree(d, ignore_errors=True)

print("all ok" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
