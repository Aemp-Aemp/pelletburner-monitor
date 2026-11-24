#!/usr/bin/env python3
"""Today's graph as a PNG for the answer to the phone command "status" (opt-in: Settings -> Phone alerts).

The same chart as "Today and tomorrow morning" on the home page, in its dark colours: boiler °C, power %, burning
bars, the heating plan (also tomorrow's), outside temperature (forecast dashed), the "now" line, hour labels and a
title with the date, the state and the boiler temperature. Drawn with Pillow at 2x and scaled down (smooth lines),
DejaVu Sans as the font (fonts/ in the repo; Pillow's own font if missing). Without Pillow nothing is drawn: the caller
(burner_notify) then sends the text alone.

    png = today_png(mod, "en")                                    # bytes; raises on errors
    png = render(rows, plan, wx, now, t0, t1, head, lang)         # the drawing itself (tests use this)
"""
import datetime, io, os, time

import burner_common as C

W, H = 1000, 500                  # the picture
S = 2                             # drawn at S x, then scaled down (anti-aliasing)
X0, X1, Y0, Y1 = 60, 936, 104, 382      # the plot area (picture pixels)
BURNING = (3, 4, 7)
GAP_S = 900                       # rows further apart than this: the line is broken

# the home page's dark theme tokens (scada/home.html :root dark)
COL = {"card": "#28211D", "ink": "#F2E8DC", "ink2": "#B5A898", "ink3": "#8A7D70", "grid": "#342B25",
       "axis": "#4A3E36", "ember": "#E8692C", "fanLine": "#9AA7AF", "planKeep": "#4A321F", "planStart": "#352C45",
       "planText": "#F0B97A", "outLine": "#4E8FD0"}
WORDS = {"en": {"today": "Today", "now": "Now", "tomorrow": "tomorrow", "next": "Next start {t}", "warmby": "warm by {t}",
                "next_tmw": "Next start tomorrow {t}", "keep": "Keep warm", "start": "Start", "boiler": "Boiler",
                "power": "Power %", "burning": "Burning", "out": "Outside", "nodata": "No data yet today",
                "months": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()},
         "da": {"today": "I dag", "now": "Nu", "tomorrow": "i morgen", "next": "Næste start {t}", "warmby": "varm kl. {t}",
                "next_tmw": "Næste start i morgen {t}", "keep": "Hold varm", "start": "Start", "boiler": "Kedel",
                "power": "Effekt %", "burning": "Brænder", "out": "Ude", "nodata": "Ingen data i dag endnu",
                "months": "jan feb mar apr maj jun jul aug sep okt nov dec".split()}}
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")     # shipped in the repo
FONTS = {"regular": "DejaVuSans.ttf", "bold": "DejaVuSans-Bold.ttf"}
_font_cache = {}


def font(size, bold=False):
    """DejaVu Sans from fonts/ in the repo, at S x; Pillow's own font when the file is missing."""
    key = (size, bold)
    if key not in _font_cache:
        from PIL import ImageFont
        try:
            f = ImageFont.truetype(os.path.join(FONT_DIR, FONTS["bold" if bold else "regular"]), size * S)
        except OSError:
            f = ImageFont.load_default(size * S)
        _font_cache[key] = f
    return _font_cache[key]


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _runs(rows, key, ok=lambda r: True):
    """Lists of (epoch, value) without gaps > GAP_S and with `ok(row)` true."""
    out, cur, last = [], [], None
    for r in rows:
        e, v = _num(r.get("epoch")), _num(r.get(key))
        if e is None or v is None or not ok(r):
            if cur:
                out.append(cur)
            cur, last = [], None
            continue
        if last is not None and e - last > GAP_S and cur:
            out.append(cur)
            cur = []
        cur.append((e, v))
        last = e
    if cur:
        out.append(cur)
    return out


def page_rows(rows, now, hours=24, keep=2000):
    """The rows the page draws: the last `hours` h, every n-th so at most `keep` (as /history.json?h=24)."""
    rows = [r for r in rows if isinstance(r, dict) and (_num(r.get("epoch")) or 0) >= now - hours * 3600]
    return rows[::max(1, len(rows) // keep)]


def window(now):
    """The home page's "Today and tomorrow morning" span: today (from 6 h back just after midnight) up to
    tomorrow 08:00, so the night's plan and tomorrow's planned starts are in it."""
    d0 = datetime.datetime.combine(datetime.date.fromtimestamp(now), datetime.time()).timestamp()
    t0 = min(d0, (now - 6 * 3600) // 3600 * 3600)
    return t0, d0 + 32 * 3600


def _hhmm(e):
    return datetime.datetime.fromtimestamp(e).strftime("%H:%M")


class _Pen:
    """ImageDraw in picture pixels (multiplied by S here)."""

    def __init__(self, d):
        self.d = d

    def line(self, pts, col, w=1.0, dash=None):
        pts = [(x * S, y * S) for x, y in pts]
        if len(pts) < 2:
            return
        width = max(1, round(w * S))
        if not dash:
            self.d.line(pts, fill=col, width=width, joint="curve")
            return
        on, off = dash[0] * S, dash[1] * S           # dashes along the whole polyline
        pos, drawing, seg = 0.0, True, [pts[0]]
        for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
            dx, dy = xb - xa, yb - ya
            n = (dx * dx + dy * dy) ** 0.5
            t = 0.0
            while n > 0 and t < n:
                step = min((on if drawing else off) - pos, n - t)
                t += step
                pos += step
                p = (xa + dx * t / n, ya + dy * t / n)
                if drawing:
                    seg.append(p)
                if pos >= (on if drawing else off) - 1e-9:
                    if drawing and len(seg) > 1:
                        self.d.line(seg, fill=col, width=width, joint="curve")
                    drawing, pos, seg = not drawing, 0.0, [p]
        if drawing and len(seg) > 1:
            self.d.line(seg, fill=col, width=width, joint="curve")

    def rect(self, x0, y0, x1, y1, col, r=0):
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        box = [x0 * S, y0 * S, x1 * S, y1 * S]
        if r:
            self.d.rounded_rectangle(box, radius=min(r * S, (x1 - x0) * S / 2, (y1 - y0) * S / 2), fill=col)
        else:
            self.d.rectangle(box, fill=col)

    def dot(self, x, y, r, col):
        self.d.ellipse([(x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S], fill=col)

    def text(self, x, y, s, col, size=15, bold=False, anchor="ls", halo=None):
        kw = {"stroke_width": 3 * S, "stroke_fill": halo} if halo else {}
        self.d.text((x * S, y * S), s, fill=col, font=font(size, bold), anchor=anchor, **kw)

    def width(self, s, size=15, bold=False):
        return self.d.textlength(s, font=font(size, bold)) / S


def render(rows, plan, wx, now, t0, t1, head, lang="en", next_start=None, out_c=None, setpoint=None):
    """-> PNG bytes. rows: [{epoch, state, boiler_c, power_pct}], plan: [{a, b, kind, name, warmby, lead, target}],
    wx: [[epoch, °C], ...] hourly outside temperature (may be empty), head: (bold title, lighter subtitle)."""
    from PIL import Image, ImageDraw
    w = WORDS.get(lang, WORDS["en"])
    img = Image.new("RGB", (W * S, H * S), COL["card"])
    pen = _Pen(ImageDraw.Draw(img))
    X = lambda e: X0 + (X1 - X0) * (e - t0) / (t1 - t0)
    cx = lambda e: X(min(max(e, t0), t1))
    rows = [r for r in rows if isinstance(r, dict) and t0 <= (_num(r.get("epoch")) or 0) <= now]
    bv = [v for v in (_num(r.get("boiler_c")) for r in rows) if v is not None]
    wx = [(e, c) for e, c in ((_num(h[0]), _num(h[1])) for h in (wx or [])
                              if isinstance(h, (list, tuple)) and len(h) >= 2)
          if e is not None and c is not None and t0 - 3600 <= e <= t1 + 3600]
    lo = min([20.0] + bv + [c for _, c in wx]) // 10 * 10
    hi = -(-max([70.0 if setpoint is None else setpoint + 5] + bv) // 10) * 10
    Y = lambda v: Y1 - (Y1 - Y0) * (v - lo) / (hi - lo)
    YP = lambda p: Y1 - (Y1 - Y0) * max(0.0, min(100.0, p)) / 100
    top = Y0 - 16                                  # plan bands reach a bit above the plot

    # title: bold "Today · 22 Nov", lighter "Running · 65 °C", the next start on the right
    title, sub = head
    pen.text(24, 46, title, COL["ink"], 32, True)
    if sub:
        pen.text(24 + pen.width(title, 32, True) + 12, 46, "·  " + sub, COL["ink2"], 24)
    if next_start and now < next_start:
        d1 = datetime.date.fromtimestamp(now) + datetime.timedelta(days=1)
        key = "next_tmw" if datetime.date.fromtimestamp(next_start) >= d1 else "next"
        pen.text(W - 24, 46, w[key].format(t=_hhmm(next_start)), COL["ink3"], 19, anchor="rs")

    # plan bands (faded once past), labels drawn last
    labels, ends = [], [-1e9, -1e9]

    def label(x, txt, fade):
        tw_ = pen.width(txt, 15, True) + 8
        right = x + tw_ > X1
        a = x - tw_ if right else x
        row = next((i for i, e in enumerate(ends) if a >= e + 4), None)
        if row is None:
            return
        ends[row] = a + tw_
        labels.append((x - 5 if right else x + 5, top + 16 + row * 18, txt, "rs" if right else "ls", fade))
    for b in sorted((p for p in plan or [] if isinstance(p, dict)), key=lambda p: _num(p.get("a")) or 0):
        a_, z_ = _num(b.get("a")), _num(b.get("b"))
        if a_ is None or z_ is None or z_ <= t0 or a_ >= t1:
            continue
        if b.get("warmby"):
            tg = a_ + (_num(b.get("lead")) or 0) * 60
            if tg > t0:
                fade = tg < now
                pen.rect(cx(a_), top, max(cx(a_) + 3, cx(tg)), Y1 + 4, "#2E2630" if fade else COL["planStart"], 6)
                pen.line([(cx(tg), top), (cx(tg), Y1 + 4)], COL["planText"], 1.6)
                label(cx(tg), w["warmby"].format(t=b.get("target") or _hhmm(tg)), fade)
        elif b.get("kind") == "keep":
            fade = z_ < now
            pen.rect(cx(a_), top, max(cx(a_) + 3, cx(z_)), Y1 + 4, "#382A20" if fade else COL["planKeep"], 6)
            pen.line([(cx(a_), top), (cx(a_), Y1 + 4)], COL["planText"], 1.0)
            if cx(z_) - cx(a_) > 34:
                label(cx(a_), b.get("name") or w["keep"], fade)
        elif a_ >= t0:
            pen.line([(X(a_), top), (X(a_), Y1 + 4)], COL["planText"], 1.4, dash=(4, 3))
            label(X(a_), b.get("name") or w["start"], a_ < now)

    # soft grid, °C on the left, % on the right
    g = lo
    while g <= hi:
        pen.line([(X0, Y(g)), (X1, Y(g))], COL["grid"], 1)
        pen.text(X0 - 8, Y(g) + 5, f"{g:.0f}°", COL["ink3"], 17, anchor="rs")
        g += 10
    for p in (0, 50, 100):
        pen.text(X1 + 6, YP(p) + 5, f"{p}%", COL["fanLine"], 17)
    if setpoint is not None:
        pen.line([(X0, Y(setpoint)), (X1, Y(setpoint))], COL["ink3"], 1, dash=(3, 4))
    d0 = datetime.datetime.combine(datetime.date.fromtimestamp(now), datetime.time()).timestamp()
    e = d0 - 86400                                 # every 3 hours of local time
    while e <= t1:
        if e >= t0:
            pen.text(X(e), Y1 + 46, datetime.datetime.fromtimestamp(e).strftime("%H"), COL["ink3"], 17, anchor="ms")
        e += 10800
    if t0 < d0:                                    # just after midnight: where today begins
        pen.line([(X(d0), top), (X(d0), Y1 + 20)], COL["axis"], 2)
    tm = d0 + 86400
    if t0 < tm < t1:
        pen.line([(X(tm), top), (X(tm), Y1 + 20)], COL["axis"], 2)
        pen.text(X(tm) + 6, Y1 + 30, w["tomorrow"], COL["ink3"], 15)

    # outside temperature: measured solid, forecast dashed
    if len(wx) > 1:
        past = [p for p in wx if p[0] <= now + 1800]
        fut = [p for p in wx if p[0] >= now - 3600]
        if len(past) > 1:
            pen.line([(cx(e), Y(c)) for e, c in past], COL["outLine"], 2.4)
        if len(fut) > 1:
            pen.line([(cx(e), Y(c)) for e, c in fut], "#3F73A8", 2.4, dash=(5, 4))
    # power % only while burning (states 3, 4, 7), then the boiler temperature on top - as the page draws them
    for rn in _runs(rows, "power_pct", lambda r: _num(r.get("state")) in BURNING):
        if len(rn) > 1:
            pen.line([(X(e), YP(v)) for e, v in rn], COL["fanLine"], 2)
    for rn in _runs(rows, "boiler_c"):
        if len(rn) > 1:
            pen.line([(X(e), Y(v)) for e, v in rn], COL["ember"], 3.6)
    if out_c is not None and wx:
        pen.dot(cx(now), Y(out_c), 4, COL["outLine"])
        pen.text(cx(now) + 7, Y(out_c) - 7, f"{out_c:.1f}°".replace(".", "," if lang == "da" else "."),
                 COL["outLine"], 16, True, halo=COL["card"])
    # burning bars under the plot
    seg = None
    for r in rows + [{"epoch": now + GAP_S + 1, "state": None}]:
        e, on = _num(r.get("epoch")), _num(r.get("state")) in BURNING
        if seg and (not on or e - seg[1] > GAP_S):
            pen.rect(X(seg[0]), Y1 + 10, max(X(seg[0]) + 4, X(seg[1])), Y1 + 18, COL["ember"], 4)
            seg = None
        if on:
            seg = [seg[0], e] if seg else [e, e]
    # now line, next-start marker, labels on top (with a halo in the card colour)
    xn = cx(now)
    pen.line([(xn, Y0 - 4), (xn, Y1 + 20)], COL["ink"], 1.5)
    if next_start and t0 <= next_start <= t1:
        xs = X(next_start)
        pen.d.polygon([((xs - 6) * S, (Y1 + 22) * S), ((xs + 6) * S, (Y1 + 22) * S), (xs * S, (Y1 + 13) * S)],
                      fill=COL["planText"])
    for x, y, txt, anc, fade in labels:
        pen.text(x, y, txt, "#A88259" if fade else COL["planText"], 15, True, anchor=anc, halo=COL["card"])
    pen.text(xn, top - 6, w["now"], COL["ink"], 17, True, anchor="ms", halo=COL["card"])
    if not bv:
        pen.text((X0 + X1) / 2, (Y0 + Y1) / 2, w["nodata"], COL["ink3"], 16, anchor="ms", halo=COL["card"])

    # legend: rounded swatches
    lx, ly = 24, H - 22
    items = [(w["keep"], "box", COL["planKeep"]), (w["start"], "box", COL["planStart"]),
             (w["boiler"], "line", COL["ember"]), (w["power"], "line", COL["fanLine"]),
             (w["burning"], "bar", COL["ember"])]
    if len(wx) > 1:
        items.append((w["out"], "line", COL["outLine"]))
    for txt, kind, col in items:
        if kind == "box":
            pen.rect(lx, ly - 13, lx + 14, ly + 1, col, 4)
        elif kind == "bar":
            pen.rect(lx, ly - 9, lx + 14, ly - 3, col, 3)
        else:
            pen.rect(lx, ly - 7.5, lx + 16, ly - 4.5, col, 1.5)
        pen.text(lx + 22, ly, txt, COL["ink2"], 18)
        lx += 22 + pen.width(txt, 18) + 24

    out = img.resize((W, H), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    if buf.tell() > 120_000:                       # keep it small for the phone: fewer colours
        buf = io.BytesIO()
        out.quantize(colors=128, method=Image.MEDIANCUT).save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _day_rows(store_dir, t0, now):
    if not store_dir:
        return []
    rows, d = [], datetime.date.fromtimestamp(t0)
    while d <= datetime.date.fromtimestamp(now):
        rows += [r for r in C.rows_of(os.path.join(store_dir, f"hist-{d.isoformat()}.jsonl")) if isinstance(r, dict)]
        d += datetime.timedelta(days=1)
    return rows


def head_for(mod, lang, now):
    """("Today · 22 Nov", "Running · 65 °C") - the state only while the data is fresh."""
    w = WORDS.get(lang, WORDS["en"])
    dt = datetime.datetime.fromtimestamp(now)
    title = f"{w['today']} · {dt.day}{'.' if lang == 'da' else ''} {w['months'][dt.month - 1]}"
    lat = getattr(mod, "latest", None) or {}
    f = lat.get("fields") or {}
    st, parts = C.to_int(f.get("state")), []
    if st is not None and 0 <= st < 8 and now - (lat.get("epoch") or 0) <= 300:
        parts.append((C.STATE_WORDS_DA if lang == "da" else C.STATE_WORDS).get(st, str(st)))
        bc = C.to_float(f.get("boiler_c"))
        if bc is not None:
            parts.append(f"{bc:.0f} °C")
    return title, " · ".join(parts)


def today_png(mod, lang="en", now=None):
    """Today's graph from the monitor's own history (data folder), plan and weather. Raises on errors."""
    now = now or time.time()
    t0, t1 = window(now)
    store = getattr(mod, "store", None)
    rows = page_rows(_day_rows(getattr(store, "dir", None), min(t0, now - 86400), now), now)
    plan, wx, nxt, out_c = [], [], None, None
    warm = getattr(mod, "warm", None)
    if warm:
        try:
            fw = warm.feed()
            if fw.get("enabled"):
                plan, nxt = fw.get("plan") or [], fw.get("next_start")
        except Exception:
            pass
    weather = getattr(mod, "weather", None)
    if weather:
        try:
            fx = weather.feed()
            wx, out_c = fx.get("hourly") or [], _num(fx.get("c"))
        except Exception:
            pass
    sp = None
    for r in reversed(rows[-50:]):
        sp = _num(r.get("setpoint"))
        if sp is not None:
            break
    return render(rows, plan, wx, now, t0, t1, head_for(mod, lang, now), lang, next_start=nxt, out_c=out_c,
                  setpoint=sp)
