"""After a power cut .jsonl lines can start with NUL bytes, or hold a good record, NULs and a cut one glued together.
The shared reader (burner_common) and the pi-health readers keep every whole record. Made-up rows.
Run: py scada\\_nul_lines_test.py"""
import os, sys, json, time, tempfile, shutil, datetime
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import burner_common as C
import burner_ext as X

fails = 0
def check(name, cond, info=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {info}"))
    if not cond:
        fails += 1

Z = "\x00" * 37
cases = [
    (Z + '{"epoch": 1, "a": 1}', [{"epoch": 1, "a": 1}]),
    ('{"epoch": 1, "st' + Z + '{"epoch": 2}', [{"epoch": 2}]),
    ('{"epoch": 1}' + Z + '{"epoch": 2, "st', [{"epoch": 1}]),
    ('{"epoch": 1}' + Z + '{"epoch": 2}', [{"epoch": 1}, {"epoch": 2}]),
    (Z + " " + Z + '{"epoch": 3}' + Z, [{"epoch": 3}]),
    (Z, []),
    ('{"epoch": 4', []),
]
for line, want in cases:
    got = C.parse_jsonl_records(line)
    check(f"records of {line.replace(chr(0), '.')[:40]!r}", got == want, got)
    check(f"... and as bytes", C.parse_jsonl_records(line.encode()) == want)
    check(f"... last one by parse_jsonl_line", C.parse_jsonl_line(line) == (want[-1] if want else None))

d = tempfile.mkdtemp()
try:
    p = os.path.join(d, "hist-2000-01-01.jsonl")
    with open(p, "wb") as fh:
        fh.write(b'{"epoch": 1}\n' + b'{"epoch": 2}' + b"\x00" * 900 + b'{"epoch": 3, "st' + b"\n"
                 + b"\x00" * 50 + b'{"epoch": 4}\n')
    check("read_jsonl: the record before and after the NUL runs are kept", C.rows_of(p) ==
          [{"epoch": 1}, {"epoch": 2}, {"epoch": 4}], C.rows_of(p))
    now = time.time()
    day = datetime.date.fromtimestamp(now).isoformat()
    ph = os.path.join(d, f"pi-health-{day}.jsonl")
    bad = {"epoch": now - 120, "internet": {"ok": False, "dns_ok": False}, "net": {}}
    with open(ph, "wb") as fh:
        fh.write(b"\x00" * 300 + json.dumps(dict(bad, epoch=now - 180)).encode() + b"\n"
                 + json.dumps(bad).encode() + b"\x00" * 200 + b'{"epoch": 1, "cp\n'
                 + b"\x00" * 10 + json.dumps(dict(bad, epoch=now - 60)).encode() + b"\n")
    r = X.read_pi_health(d)
    check("read_pi_health: the newest record after a NUL run", r and abs(r["epoch"] - (now - 60)) < 1, r)
    t = X.net_trouble(d, now=now, cache_s=0)
    check("net_trouble: all three records count (none lost to the NULs)", t and t["down_min"] == 3, t)
    import replay as R
    check("replay copies the whole record before a NUL run", R._clean_line('{"epoch": 2}' + Z + '{"ep') == '{"epoch": 2}\n')
finally:
    shutil.rmtree(d, ignore_errors=True)
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
