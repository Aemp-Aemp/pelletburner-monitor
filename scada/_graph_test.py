"""Today's graph as a picture on the phone "status" answer: the PNG is valid, small and has the chart's colours;
the ntfy send is a PUT with Filename when the setting is on, plain text when off, when the picture fails or when
Pillow is missing. No network: _urlopen is faked. Synthetic data only. Run: py scada\\_graph_test.py"""
import os, sys, json, tempfile, shutil, datetime, time, io, urllib.error
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F
import burner_notify as N
import burner_graph as G
from PIL import Image

fails = 0
def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails += 1

class Log:
    def __init__(self): self.lines = []
    def __call__(self, *a): self.lines.append(" ".join(str(x) for x in a))

def png_img(b):
    """The PNG checked by Pillow (verify) and opened again for reading."""
    assert b[:8] == b"\x89PNG\r\n\x1a\n", "signature"
    Image.open(io.BytesIO(b)).verify()
    return Image.open(io.BytesIO(b)).convert("RGB")

def near(img, hexcol, tol=24):
    """How many pixels are close to this colour."""
    c = tuple(int(hexcol[i:i + 2], 16) for i in (1, 3, 5))
    return sum(n for n, p in img.getcolors(1 << 20) if sum(abs(a - b) for a, b in zip(p, c)) <= tol)

def synth_rows(d0, now):
    """A made-up day (10 s rows): burns 06-09 and 16-19; power climbs from 30 % and eases off near the target."""
    rows, b = [], 30.0
    for s in range(0, int(now - d0), 10):
        h = s / 3600
        start = 6 if h < 12 else 16
        burn = 6 <= h < 9 or 16 <= h < 19
        p = round(max(30.0, min(100.0, 30 + 140 * (h - start), (72 - b) * 6))) if burn else 0
        b = b + p / 100 * 0.012 - (b - 18) * 1.2e-4
        rows.append({"epoch": d0 + s, "state": 4 if burn else 0, "boiler_c": round(b, 1), "power_pct": p,
                     "setpoint": 70})
    return rows

# ---------------------------------------------------------------- the renderer
d0 = datetime.datetime(2000, 1, 3).timestamp()
now = d0 + 19.5 * 3600
rows = synth_rows(d0, now)
plan = [{"a": d0 + 6 * 3600, "b": d0 + 8 * 3600, "kind": "keep", "warmby": True, "lead": 60, "target": "07:00"},
        {"a": d0 + 16 * 3600, "b": d0 + 20 * 3600, "kind": "keep", "name": "Evening"},
        {"a": d0 + 29 * 3600, "b": d0 + 29.5 * 3600, "kind": "start"}]
wx = [[d0 + h * 3600, 2.0 + (h % 24) / 6.0] for h in range(-2, 34)]
t0, t1 = G.window(now)
pr = G.page_rows([{"epoch": now - 30 * 3600}] + rows, now)
check("rows as the page gets them: last 24 h, at most ~2000", 1000 <= len(pr) <= 4000
      and all(r["epoch"] >= now - 86400 for r in pr) and pr[0]["epoch"] == rows[0]["epoch"])
check("window: today and tomorrow morning", t0 == d0 and t1 == d0 + 32 * 3600)
check("window just after midnight keeps the last 6 h", G.window(d0 + 1800)[0] == d0 - 6 * 3600)
check("window at noon also reaches tomorrow morning", G.window(d0 + 12 * 3600) == (d0, d0 + 32 * 3600))
png = G.render(rows, plan, wx, now, t0, t1, ("Today · 3 Jan", "Running · 60 °C"), "en",
               next_start=d0 + 29 * 3600, out_c=3.5, setpoint=70)
img = png_img(png)
check("valid PNG, 1000x500", img.size == (1000, 500))
check(f"small ({len(png)} bytes < 120 kB)", 5000 < len(png) < 120_000)
check("dark card background", img.getpixel((5, 5)) == (0x28, 0x21, 0x1D))
check("the boiler line is drawn (ember)", near(img, G.COL["ember"]) > 1500)
check("the outside temperature is drawn (blue)", near(img, G.COL["outLine"]) > 300)
check("the keep-warm band is drawn", near(img, G.COL["planKeep"], 6) > 5000)
check("the power line is drawn (grey)", near(img, G.COL["fanLine"], 40) > 150)
png_da = G.render(rows, plan, [], now, t0, t1, ("I dag · 3. jan", "Optænding · 60 °C"), "da")
check("Danish (æøå), no weather: still valid", png_img(png_da).size == (1000, 500))
check("no rows at all: still a picture", png_img(G.render([], [], [], now, t0, t1, ("X", ""), "en")).size == (1000, 500))
check("broken rows are skipped", png_img(G.render([{"epoch": "x"}, {}, {"epoch": now, "boiler_c": None}] + rows[:50],
                                                  [{"a": None}, "x"], [[1], None], now, t0, t1, ("X", "")))
      .size == (1000, 500))

# ---------------------------------------------------------------- the fonts (fonts/ in the repo, copied in the Dockerfile)
check("fonts/LICENSE is there", os.path.isfile(os.path.join(G.FONT_DIR, "LICENSE")))
check("DejaVu fonts are there", all(os.path.isfile(os.path.join(G.FONT_DIR, f)) for f in G.FONTS.values()))
with open(os.path.join(G.FONT_DIR, "..", "Dockerfile"), encoding="utf-8") as fh:
    check("Dockerfile copies fonts/", "COPY fonts/ ./fonts/" in fh.read())
for bold in (False, True):
    G._font_cache.clear()
    fnt = G.font(15, bold)
    box = bytes(fnt.getmask("▯"))
    bad = [ch for w in ("Næste start", "Brænder", "Optænding", "Redder ilden", "ÆØÅæøå")
           for ch in w if ch != " " and bytes(fnt.getmask(ch)) == box]
    check(f"{'bold' if bold else 'regular'}: every letter has its own glyph (no missing-glyph box) {bad}", not bad)
G._font_cache.clear()
check("Danish picture words: Redder ilden, not Redder bål", G.WORDS["da"]["burning"] == "Brænder"
      and N.burner_common.STATE_WORDS_DA[7] == "Redder ilden")

# today_png from a data folder (as on the Pi)
d = tempfile.mkdtemp()
try:
    real_now = time.time()
    day = datetime.date.fromtimestamp(real_now)
    rd0 = datetime.datetime.combine(day, datetime.time()).timestamp()
    with open(os.path.join(d, f"hist-{day.isoformat()}.jsonl"), "w") as fh:
        for r in synth_rows(rd0, real_now):
            fh.write(json.dumps(r) + "\n")

    class Store: dir = d
    class Mod:
        store, warm, weather, optimiser, tracker = Store(), None, None, None, None
        latest = {"epoch": real_now, "fields": {"state": 4, "boiler_c": 61.4, "alarm": 0}}
    mod = Mod()
    p2 = G.today_png(mod, "da", real_now)
    check("today_png from the data folder: valid PNG", png_img(p2).size == (1000, 500) and len(p2) < 120_000)
    hd = G.head_for(mod, "en", real_now)
    check("title: date; subtitle: state and °C", hd[0] == f"Today · {day.day} {G.WORDS['en']['months'][day.month - 1]}"
          and hd[1] == "Running · 61 °C")
    check("Danish subtitle", G.head_for(mod, "da", real_now)[1] == "Drift · 61 °C")

    # ---------------------------------------------------------------- sending (network faked)
    sent = []
    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    refuse_put = {"on": False}
    def fake_urlopen(req, timeout=None, **kw):
        sent.append(req)
        if refuse_put["on"] and req.get_method() == "PUT":
            raise urllib.error.HTTPError(req.full_url, 413, "too large", {}, None)
        return Resp(b'{"id":"abc"}')
    N._urlopen = fake_urlopen

    log = Log()
    st = F.Settings(d, log)
    check("setting on by default", st.get("ntfy")["graph"] is True)
    ok, _ = st.update({"ntfy": {"enabled": True, "lang": "en", "server": "http://ntfy.example.test",
                                "events": {"daily": True}, "graph": False}})
    check("setting can be turned off", ok and st.get("ntfy")["graph"] is False)
    assert ok
    n = N.Notifier(st, mod, d, log, sleep=lambda s: None, start=False)
    noon = datetime.datetime.combine(day, datetime.time(12, 0)).timestamp()
    n.graph_clock = lambda: noon

    def dl(item, **kw):
        """deliver with the hourly limit reset (the checks below are about sending, not the limit)"""
        n._graph_t = 0.0
        return n.deliver(item, **kw)

    def daily_item():
        t, b, pr, tg = N.message("daily", "en", h=3, starts=2, kg=4.5, al=0)
        return {"key": "daily", "title": t, "body": b, "priority": pr, "tags": tg, "graph": True}

    sent.clear(); dl(daily_item(), retries=())
    check("off: plain text POST", len(sent) == 1 and sent[0].get_method() == "POST"
          and sent[0].get_header("Filename") is None and b"Burned 3 h" in sent[0].data)

    ok, _ = st.update({"ntfy": {"graph": True}})
    check("setting can be turned on", ok and st.get("ntfy")["graph"] is True)
    sent.clear(); dl(daily_item(), retries=())
    r = sent[0] if sent else None
    check("on: one PUT with Filename: today.png and the PNG as body",
          len(sent) == 1 and r.get_method() == "PUT" and r.get_header("Filename") == "today.png"
          and r.data[:8] == b"\x89PNG\r\n\x1a\n")
    check("on: the text is in the Message header, title kept",
          r.get_header("Message") == "Burned 3 h, 2 start(s), about 4.5 kg pellets, 0 alarm(s)." and r.get_header("Title") == "Last 24 h at the boiler")
    check("topic not in the log", not any(st.get("ntfy")["topic"] in x for x in log.lines))

    sent.clear(); dl({"key": "alarm", "title": "A", "body": "B", "graph": False}, retries=())
    check("other messages: never a picture", sent and sent[0].get_method() == "POST")

    # a render error: the text alone, one log line
    orig = G.today_png
    G.today_png = lambda *a, **k: 1 / 0
    log.lines.clear(); sent.clear(); dl(daily_item(), retries=())
    G.today_png = orig
    check("render error: plain text sent", len(sent) == 1 and sent[0].get_method() == "POST")
    check("render error: one log line", sum("graph not made" in x for x in log.lines) == 1)

    # no Pillow on this machine: the text alone
    saved = {k: v for k, v in sys.modules.items() if k == "PIL" or k.startswith("PIL.")}
    for k in saved:
        sys.modules[k] = None
    try:
        log.lines.clear(); sent.clear(); dl(daily_item(), retries=())
    finally:
        sys.modules.update(saved)
    check("without Pillow: plain text and one log line", len(sent) == 1 and sent[0].get_method() == "POST"
          and sum("graph not made (ModuleNotFoundError)" in x for x in log.lines) == 1)

    # the server refuses attachments: the text alone
    refuse_put["on"] = True
    log.lines.clear(); sent.clear(); ok = dl(daily_item(), retries=())
    refuse_put["on"] = False
    check("picture refused: falls back to text", ok and [x.get_method() for x in sent] == ["PUT", "POST"]
          and sum("picture not sent" in x for x in log.lines) == 1)

    # multi-line / Danish text goes in an encoded Message header
    sent.clear(); dl({"key": "phone", "title": "Kedel-status", "body": "Tilstand: Drift\nKedel: 61 °C",
                             "graph": True}, retries=())
    mh = sent[0].get_header("Message") if sent else ""
    import base64
    check("multi-line Message header is RFC 2047", mh.startswith("=?UTF-8?B?")
          and base64.b64decode(mh[10:-2]).decode() == "Tilstand: Drift\nKedel: 61 °C")

    # only 07:00-22:00, at most one an hour
    def made_at(hh, mm, reset=True):
        n.graph_clock = lambda: datetime.datetime.combine(day, datetime.time(hh, mm)).timestamp()
        if reset:
            n._graph_t = 0.0
        return n._graph(daily_item()) is not None
    check("picture at 07:00 and 21:59", made_at(7, 0) and made_at(21, 59))
    check("no picture at night (06:59, 22:00, 03:00)", not made_at(6, 59) and not made_at(22, 0) and not made_at(3, 0))
    check("one an hour: 12:00 yes, 12:30 no, 13:00 yes",
          made_at(12, 0) and not made_at(12, 30, False) and made_at(13, 0, False))
    n.graph_clock = lambda: noon
    n._graph_t = 0.0
    sent.clear(); n.deliver(daily_item(), retries=()); n.deliver(daily_item(), retries=())
    check("two status answers in a minute: picture on the first only",
          [x.get_method() for x in sent] == ["PUT", "POST"])

    # the phone "status" answer asks for the picture, other answers do not
    import burner_phone as P
    st.update({"ntfy": {"cmd_pin": "4821", "cmd_topic": F.new_ntfy_cmd_topic()}})
    pc = P.PhoneCommands(n, mod, log, start=False)
    while not n.q.empty():
        n.q.get()
    pc.reply("status", text="x"); pc.reply("help")
    items = [n.q.get() for _ in range(2)]
    check("phone: status answer carries the graph flag, help does not", items[0]["graph"] and not items[1]["graph"])
    # the daily summary never carries the picture (only the "status" answer does)
    while not n.q.empty():
        n.q.get()
    n.notify("daily", h=1, starts=1, kg=1)
    check("daily summary: no graph flag", not n.q.get().get("graph"))
finally:
    shutil.rmtree(d, ignore_errors=True)

print(f"\n{'ALL OK' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
