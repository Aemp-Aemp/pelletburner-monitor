"""Auto-generated discoveries for the classic page ("Discoveries" tab). Reads the local data files (read only) and
writes data/insights.json. Run any time:  py scada\\insights_gen.py
Each card: {"id", "title": {en,da}, "text": {en,da}, "chart": {...} or None}.
Chart types the page can draw: "bar" {labels, series:[{name:{en,da}, data, unit}]}, "stack" (same, stacked),
"xy" {points:[[x,y],...], xlabel:{en,da}, ylabel:{en,da}}.
New ideas are added as new functions in IDEAS at the bottom."""
import json, glob, os, statistics, datetime, collections, math, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import burner_common as C               # shared tables and file helpers
import burner_features as F             # the start records (time to flame): the same steps as the live program
DATA = os.path.join(HERE, "..", "data")
OUT = os.path.join(DATA, "insights.json")      # in the data folder, so the cards survive a restart / update
STATE = {k: (C.STATE_WORDS[k], C.STATE_WORDS_DA[k]) for k in C.STATE_WORDS}     # the shared state words (EN, DA)


def load(prefix):
    rows = []
    for p in sorted(glob.glob(os.path.join(DATA, f"{prefix}-*.jsonl"))):
        rows.extend(C.read_jsonl(p))
    rows.sort(key=lambda r: r.get("epoch", 0))
    return rows


def rows_best():
    """Full-rate rows where we have them, the 10-s history elsewhere."""
    raw, hist = load("raw"), load("hist")
    if not raw:
        return hist
    t0 = raw[0]["epoch"]
    return [r for r in hist if r["epoch"] < t0] + raw


def segments(rows, gap=120):
    """Consecutive (state, start, end) runs; a data gap > `gap` s ends a segment."""
    out, cur = [], None
    for r in rows:
        st, t = r.get("state"), r["epoch"]
        if cur and (st != cur[0] or t - cur[2] > gap):
            out.append(tuple(cur)); cur = None
        if not cur:
            cur = [st, t, t]
        cur[2] = t
    if cur:
        out.append(tuple(cur))
    return out


def state_hours(rows, gap=120):
    """(local day, state, hours) pieces. Each row holds its state until the next row; rows with the same time count
    once, a data gap > `gap` s counts nothing, and a piece past midnight is split, so no day gets more than 24 h."""
    pts = {}
    for r in rows:
        pts[r["epoch"]] = r.get("state")
    ts = sorted(pts)
    out = []
    for t, t2 in zip(ts, ts[1:]):
        if t2 - t > gap:
            continue
        st = pts[t]
        while t < t2:
            day = datetime.date.fromtimestamp(t)
            midnight = datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time()).timestamp()
            end = min(t2, midnight)
            out.append((day.isoformat(), st, (end - t) / 3600))
            t = end
    return out


def card(cid, en_t, da_t, en_x, da_x, chart=None, data=None):
    """data = the card's own numbers, for a page that draws them its own way (the home page's Plan tab)."""
    c = {"id": cid, "title": {"en": en_t, "da": da_t}, "text": {"en": en_x, "da": da_x}, "chart": chart}
    if data is not None:
        c["data"] = data
    return c


# ---------------------------------------------------------------- ideas
def idea_time_per_state(rows):
    days = collections.OrderedDict()
    for d, st, h in state_hours(rows):
        days.setdefault(d, collections.Counter())[st] += h
    if not days:
        return None
    order = [4, 3, 2, 1, 0, 6]
    series = [{"name": {"en": STATE[s][0], "da": STATE[s][1]}, "state": s, "unit": "h",
               "data": [round(days[d].get(s, 0), 2) for d in days]} for s in order]
    labels = [d[8:10] + "/" + d[5:7] for d in days]
    n = len(days)
    run = sum(days[d].get(4, 0) for d in days) / n; ign = sum(days[d].get(3, 0) for d in days) / n
    return card("time_per_state", "Where the hours go (per day)", "Hvor timerne går hen (pr. dag)",
                f"Hours in each state per day while the monitor was recording. On average running {run:.1f} h and ignition {ign:.1f} h a day ({n} {'day' if n == 1 else 'days'}). "
                "Ignition also covers the warm-up after the flame, where the boiler raises the power step by step "
                "(about 30 min from a cold boiler), so some ignition time is normal.",
                f"Timer i hver tilstand pr. dag mens monitoren optog. I snit drift {f"{run:.1f}".replace(".", ",")} t og tænding {f"{ign:.1f}".replace(".", ",")} t om dagen ({n} {'dag' if n == 1 else 'dage'}). "
                "Tænding dækker også opvarmningen efter flammen, hvor kedlen øger effekten trinvis "
                "(ca. 30 min fra kold kedel), så noget tændtid er normalt.",
                {"type": "stack", "labels": labels, "series": series})


def start_records(rows):
    """The same start list as /api/ignitions (IgnitionLog.report): data/ignitions.jsonl from the last reset on,
    the newest 500. Without the file (a fresh install) the records are made from the rows by the same steps."""
    p = os.path.join(DATA, "ignitions.jsonl")
    recs = C.rows_of(p) if os.path.exists(p) else F.ignition_records(rows)
    since = ((_settings().get("ignitions") or {}).get("since") or 0) if os.path.exists(p) else 0
    return [r for r in recs if (r.get("epoch") or 0) >= since][-500:]


def flame_already_on(r):
    """Not a real lighting (F.real_lighting: a relight or a start that went on burning), or one that reached RUNNING
    with no lighting to time. Neither a hit nor a miss."""
    return not F.real_lighting(r) or (r.get("result") == "ok" and r.get("flame_s") is None)


def idea_ignition_times(rows):
    """Minutes to flame of each start: from the start of lighting (state 3) until the flame is strong enough
    (above 17). The same start records (`flame_s`) and the same "typical" as /api/ignitions."""
    recs = [r for r in start_records(rows) if not flame_already_on(r)]
    if not recs:
        return None
    labels = [datetime.datetime.fromtimestamp(r["epoch"]).strftime("%d/%m %H:%M") for r in recs]
    pts = [round(F.flame_min(r), 1) if F.flame_min(r) is not None else None for r in recs]
    lit = [p for p in pts if p is not None]
    # the boiler type changed: "typical" from the starts since then (older starts until 3 lit, and it says so)
    # the one "usual" (F.usual_flame: reset date, baseline, last 4 weeks), as /api/ignitions and Burner health
    typ, _, _, older = F.usual_flame(start_records(rows), _settings(), now=max(rows[-1]["epoch"] if rows else 0, recs[-1]["epoch"]))
    old_en = " (based on older starts — the boiler type changed)" if older and typ is not None else ""
    old_da = " (ud fra ældre starter — kedeltypen er ændret)" if older and typ is not None else ""
    txt_en = (f"{len(lit)} of {len(pts)} ignitions found a flame" + (f"; typical {typ:.1f} min{old_en}." if typ is not None else ".")
              if lit else "No ignition has found a flame yet.")
    txt_da = (f"{len(lit)} af {len(pts)} tændinger fik flamme" + (f"; typisk {typ:.1f}".replace(".", ",", 1) + f" min{old_da}." if typ is not None else ".")
              if lit else "Ingen tænding har fået flamme endnu.")
    # starts without a flame (stopped or failed) have no time: leave them out instead of drawing a 0-min bar
    shown = [(lb, p) for lb, p in zip(labels, pts) if p is not None][-20:]
    if not shown:
        return None
    return card("ign_minutes", "Minutes to flame, each start", "Minutter til flamme, hver start",
                txt_en + " Counted from when lighting begins until the flame is strong enough. Starts without a flame (stopped or failed) are left out of the chart; relights and starts where the flame was already on are not counted. Measured from the readings, not from the boiler's own log.",
                txt_da + " Regnet fra tændingen begynder til flammen er stærk nok. Starter uden flamme (stoppet eller mislykket) er udeladt af grafen; genoptændinger og starter hvor flammen allerede var der tælles ikke med. Målt ud fra målingerne, ikke kedlens egen log.",
                {"type": "bar", "labels": [lb for lb, _ in shown],
                 "series": [{"name": {"en": "minutes to flame", "da": "minutter til flamme"}, "unit": "min",
                             "data": [p for _, p in shown]}]})


def idea_minimum_mode(rows):
    """WAITING (minimum mode) pauses: how long, and boiler temperature when it paused."""
    out = []
    for st, a, b in segments(rows):
        if st == 1 and b - a > 60:
            r = next((x for x in rows if x["epoch"] >= a), {})
            out.append((a, (b - a) / 60, r.get("boiler_c")))
    if not out:
        return None
    pts = [[round(m, 1), bt] for a, m, bt in out if bt is not None]
    return card("min_mode", "Minimum-mode pauses", "Pauser i minimumdrift",
                f"{len(out)} {'pause' if len(out) == 1 else 'pauses'} recorded. Each dot: how long the burner waited (minutes) and the boiler temperature "
                "when it paused. Many short pauses at a high temperature mean the burner gives more heat than the house "
                "needs: a lower 'Stop at' or 'Max power' in the boiler menu can give longer, calmer runs.",
                f"{len(out)} {'pause' if len(out) == 1 else 'pauser'} registreret. Hver prik: hvor længe brænderen ventede (minutter) og kedeltemperaturen "
                "da den holdt pause. Mange korte pauser ved høj temperatur betyder at brænderen giver mere varme end huset "
                "bruger: en lavere 'Stop ved' eller 'Max ydelse' i kedlens menu kan give længere, roligere perioder.",
                {"type": "xy", "points": pts, "xlabel": {"en": "minutes waited", "da": "minutter ventet"},
                 "ylabel": {"en": "boiler °C", "da": "kedel °C"}})


SUB = [  # derived sub-steps (NOT sent by the controller): (key, en, da)
    ("ash", "Ash plate turn", "Askeplade drejer"),
    ("purge", "Air purge", "Udluftning"),
    ("ready", "Getting ready", "Gør klar"),
    ("fuel", "Start fuel (feeding)", "Tændbrændsel (fødning)"),
    ("heat", "Heating pellets, no flame yet", "Varmer piller, ingen flamme endnu"),
    ("flame", "Flame seen, igniter on", "Flamme set, tænding til"),
    ("settle", "Flame settling (fan 50 %)", "Flammen stabiliseres (røgsuger 50 %)"),
    ("step", "Stepping up power", "Effekt øges trinvis"),
    ("run", "Running", "Drift"),
    ("cool", "Cooling down (fan 25 %)", "Køler ned (røgsuger 25 %)"),
    ("wait", "Waiting", "Venter"),
    ("stop", "Stopped", "Stoppet"),
    ("alarm", "Alarm", "Alarm"),
]


def substep(r):
    st, fl = r.get("state"), (r.get("flame") or 0)
    fan, pw = r.get("fan_pct") or 0, r.get("power_pct") or 0
    if st == 6:
        return "alarm"
    if st == 2:
        return "ash" if r.get("din2") == 1 else ("purge" if fan >= 90 else "ready")
    if st == 3:
        if fl > C.FLAME_ON:
            if r.get("igniter_on"):
                return "flame"
            return "step" if pw > 0 else "settle"
        return "fuel" if r.get("auger_on") else "heat"
    if st == 4:
        return "run"
    if st == 1:
        return "cool" if 15 <= fan <= 35 else "wait"
    return "stop" if st == 0 else "wait"


def idea_start_substeps(rows):
    """Timeline of the most recent start, split into sub-steps derived from the 1-s data."""
    starts = [i for i in range(1, len(rows)) if rows[i].get("state") == 2 and rows[i - 1].get("state") != 2]
    if not starts:
        return None
    i0 = starts[-1]
    t0 = rows[i0]["epoch"] - 30
    seg, keys, cut = [], [k for k, _, _ in SUB], False
    for r in rows:
        if r["epoch"] < t0:
            continue
        k = keys.index(substep(r))
        # stop the timeline 10 min into a rest/stop that follows the start (or after 1 h)
        if seg and SUB[seg[-1][2]][0] in ("cool", "wait", "stop") and r["epoch"] - seg[-1][0] > 600 \
                and r["epoch"] - rows[i0]["epoch"] > 120:
            break
        if seg and seg[-1][2] == k and r["epoch"] - seg[-1][1] <= 120:
            seg[-1][1] = r["epoch"]
        else:
            if seg:
                seg[-1][1] = max(seg[-1][1], r["epoch"])
            seg.append([r["epoch"], r["epoch"], k])
        if r["epoch"] - t0 > 3600:
            cut = True
            break
    # one step list for the chart and the text: steps of 5 s or more, in the order they first came (top row first)
    seg = [s for s in seg if s[1] - s[0] >= 5] or seg
    used, mins = [], collections.Counter()
    for a, b, k in seg:
        if k not in used:
            used.append(k)
        mins[k] += (b - a) / 60
    when = datetime.datetime.fromtimestamp(rows[i0]["epoch"]).strftime("%d/%m %H:%M")
    summ = ", ".join(f"{SUB[k][1]} {mins[k]:.1f} min" for k in used)
    summ_da = ", ".join(f"{SUB[k][2]} {mins[k]:.1f} min".replace(".", ",") for k in used)
    note_en = ("These sub-steps are worked out from the readings (feed, igniter, fan, flame, power, ash-plate switch). "
               "The boiler does NOT send them: it only sends its main state, so its own screen texts can differ.")
    note_da = ("Disse delskridt er regnet ud fra målingerne (fødning, tænding, røgsuger, flamme, effekt, askepladekontakt). "
               "Kedlen sender dem IKKE: den sender kun sin hovedtilstand, så teksterne på dens egen skærm kan være anderledes.")
    if cut:
        summ += " (the chart shows the first hour only; the burner kept going)"
        summ_da += " (grafen viser kun den første time; brænderen fortsatte)"
    # time to flame: the one definition (burner_features: lighting start -> flame), the same number History shows.
    # The steps above also hold the purge and the moments before the start, so they add up to more.
    # the start record History uses (data/ignitions.jsonl; made from the rows when there is no file)
    rec = min((r for r in start_records(rows) if abs(r["epoch"] - rows[i0]["epoch"]) <= 60),
              key=lambda r: abs(r["epoch"] - rows[i0]["epoch"]), default=None)
    fm = F.flame_min(rec) if rec else None
    if fm is not None:
        summ += f". Time to flame {fm:.1f} min, counted from the start of lighting (as on the History page)"
        summ_da += f". Tid til flamme {fm:.1f} min, regnet fra tændingen begynder (som på Historik-siden)".replace(f"{fm:.1f}", f"{fm:.1f}".replace(".", ","))
    return card("substeps", f"The last start, step by step ({when})", f"Sidste start, trin for trin ({when})",
                f"{summ}. {note_en}", f"{summ_da}. {note_da}",
                {"type": "timeline", "t0": seg[0][0], "t1": seg[-1][1],
                 "rows": [{"en": SUB[k][1], "da": SUB[k][2]} for k in used],
                 "segments": [[round(a, 1), round(b, 1), used.index(k)] for a, b, k in seg]})


def idea_burn_vs_start(rows):
    """Per start: minutes spent getting a flame vs minutes of real burning before it stopped/paused again."""
    out = []
    starts = [i for i in range(1, len(rows)) if rows[i].get("state") == 2 and rows[i - 1].get("state") != 2]
    for n, i0 in enumerate(starts):
        t0 = rows[i0]["epoch"]
        end_i = starts[n + 1] if n + 1 < len(starts) else len(rows)
        flame = end = None
        for r in rows[i0:end_i]:
            if flame is None and (r.get("flame") or 0) > C.FLAME_ON and r.get("state") in (3, 4):
                flame = r["epoch"]
            if flame and r.get("state") in (0, 1, 5, 6):
                end = r["epoch"]
                break
        if flame is None or end is None or end - t0 > 6 * 3600:
            continue
        out.append((t0, round((flame - t0) / 60, 1), round((end - flame) / 60, 1)))
    if len(out) < 2:
        return None
    out = out[-14:]
    burn = [b for _, _, b in out]
    short = sum(1 for b in burn if b < 30)
    return card("burn_vs_start", "Each start: getting a flame vs real burning",
                "Hver start: at få flamme mod rigtig fyring",
                f"{short} of the last {len(out)} burns were shorter than 30 min after the flame (typical "
                f"{statistics.median(burn):.0f} min). Every start costs an ignition and about 6 minutes of flame settling, "
                "so short burns mean many starts for little heat. In mild weather a lower stop % in the heating plan (Settings on the new page, or a "
                "lower target temperature) usually gives longer, calmer burns.",
                f"{short} af de sidste {len(out)} fyringer var kortere end 30 min efter flammen (typisk "
                f"{statistics.median(burn):.0f} min). Hver start koster en tænding og ca. 6 minutters stabilisering, så korte "
                "fyringer betyder mange starter for lidt varme. I mildt vejr giver en lavere stop-% i varmeplanen (Indstillinger på den nye side, eller "
                "en lavere kedeltemperatur) som regel længere, roligere fyringer.",
                {"type": "stack", "labels": [datetime.datetime.fromtimestamp(t).strftime("%d/%m %H:%M") for t, _, _ in out],
                 "series": [{"name": {"en": "getting a flame", "da": "til flamme"}, "state": 3, "unit": "min",
                             "data": [a for _, a, _ in out]},
                            {"name": {"en": "burning", "da": "fyring"}, "state": 4, "unit": "min",
                             "data": [b for _, _, b in out]}]})


def idea_cooling(rows):
    """After the burner stops: how fast the boiler cools (helps planning a 'warm by' start)."""
    segs = segments(rows, gap=300)
    out = []
    for i in range(1, len(segs)):
        st, a, b = segs[i]
        if st not in (0, 1) or segs[i - 1][0] != 4:
            continue
        j = i
        while j + 1 < len(segs) and segs[j + 1][0] in (0, 1):     # a pause followed by a stop = one rest
            j += 1
        b = segs[j][2]
        if b - a < 20 * 60:
            continue
        pts = [r for r in rows if a <= r["epoch"] <= b and r.get("boiler_c") is not None]
        if len(pts) < 10:
            continue
        t0, c0 = pts[0]["epoch"], pts[0]["boiler_c"]
        curve = []
        for m in (0, 15, 30, 45, 60, 90, 120, 180, 240, 300, 360):
            r = next((p for p in pts if p["epoch"] >= t0 + m * 60), None)
            if r:
                curve.append([m, r["boiler_c"]])
        out.append((t0, c0, curve))
    if not out:
        return None
    t0, c0, curve = out[-1]
    last_m, last_c = curve[-1]
    first_h = next((c for m, c in curve if m == 60), None)
    when = datetime.datetime.fromtimestamp(t0).strftime("%d/%m %H:%M")
    en = (f"After the stop at {when} the boiler went from {c0:.0f} °C to {last_c:.0f} °C in {last_m} min"
          + (f" ({c0 - first_h:.0f} °C in the first hour)" if first_h is not None else "") +
          ". It cools fastest just after the stop and slower as it gets near room temperature. This is what a "
          "'warm by' start needs: the colder the boiler, the earlier it has to start.")
    da = (f"Efter stoppet {when} gik kedlen fra {c0:.0f} °C til {last_c:.0f} °C på {last_m} min"
          + (f" ({c0 - first_h:.0f} °C den første time)" if first_h is not None else "") +
          ". Den køler hurtigst lige efter stoppet og langsommere når den nærmer sig rumtemperatur. Det er det en "
          "'varm kl.'-start skal bruge: jo koldere kedlen er, jo tidligere skal den starte.")
    return card("cooling", "How fast the boiler cools after a stop", "Hvor hurtigt kedlen køler efter et stop", en, da,
                {"type": "xy", "points": curve, "xlabel": {"en": "minutes after the stop", "da": "minutter efter stop"},
                 "ylabel": {"en": "boiler °C", "da": "kedel °C"}})


def idea_warm_by(rows, warm_c=60.0):
    """Did each 'warm by HH:MM' start make it? Minutes between reaching warm_c and the asked time."""
    out, seen = [], set()
    for e in sorted(load("events"), key=lambda e: e.get("epoch") or 0):
        why = str(e.get("why", ""))
        if e.get("type") != "warm" or e.get("what") != "start" or not why.startswith("warm by "):
            continue
        try:
            hh, mm = int(why[8:10]), int(why[11:13])
        except ValueError:
            continue
        t0 = e["epoch"]
        day = datetime.datetime.fromtimestamp(t0)
        goal = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if goal < day - datetime.timedelta(hours=12):     # started the evening before; a retry just after the
            goal += datetime.timedelta(days=1)            # asked time still belongs to that same morning
        g = goal.timestamp()
        if g in seen:
            continue                                      # a second start (retry) for the same asked time: count it once
        seen.add(g)
        hit =next((r["epoch"] for r in rows if t0 <= r["epoch"] <= g + 3 * 3600
                    and (r.get("boiler_c") or 0) >= warm_c), None)
        if hit is None:
            if rows and rows[-1]["epoch"] < g + 3 * 3600:
                continue                                  # still under way
            out.append((day, None, None, g))
        else:
            out.append((day, round((g - hit) / 60), round((hit - t0) / 60), g))
    if not out:
        return None
    out = out[-14:]
    made = [o for o in out if o[1] is not None and o[1] >= 0]
    d, early, took, _ = out[-1]
    if early is None:
        en_last, da_last = "the last one never got there", "den sidste nåede det aldrig"
    elif early >= 0:
        en_last = f"the last one was warm {early} min before the asked time (start to warm took {took} min)"
        da_last = f"den sidste var varm {early} min før det ønskede tidspunkt (fra start til varm tog {took} min)"
    else:
        en_last = f"the last one was {-early} min late (start to warm took {took} min)"
        da_last = f"den sidste var {-early} min for sent (fra start til varm tog {took} min)"
    en = (f"{len(made)} of {len(out)} 'warm by' starts had the boiler at {warm_c:.0f} °C in time; {en_last}. "
          "Above 0 = warm early, below 0 = late. The start time is worked out from how fast the boiler cooled "
          "overnight, so it gets better as it learns.")
    da = (f"{len(made)} af {len(out)} 'varm kl.'-starter havde kedlen på {warm_c:.0f} °C i tide; {da_last}. "
          "Over 0 = varm før tid, under 0 = for sent. Starttiden regnes ud fra hvor hurtigt kedlen kølede "
          "om natten, så den bliver bedre efterhånden som den lærer.")
    return card("warm_by", "Was it warm in time?", "Var den varm i tide?", en, da,
                {"type": "bar", "labels": [o[0].strftime("%d/%m") for o in out],
                 "series": [{"name": {"en": "minutes early", "da": "minutter før tid"}, "unit": "min",
                             "data": [o[1] if o[1] is not None else 0 for o in out]}]},
                # the last 7: goal = the asked time, early = minutes warm before it (below 0 = late, None = never
                # got warm), ok = warm in time; made = how many of them were ok
                data={"warm_c": warm_c, "made": len([o for o in out[-7:] if o in made]),
                      "starts": [{"goal": round(o[3]), "early": o[1], "took": o[2], "ok": o in made} for o in out[-7:]]})


RUN_ST = (2, 3, 4, 7)


def _settings():
    return C.read_json(os.path.join(DATA, "settings.json"), {})


_hm = C.hhmm_to_min


def idea_pellets_per_period(rows, days=7):
    """kg of pellets per heating-plan period, per day. Each burn (start -> stop) goes to the period that started it;
    burns outside every period are the boiler relighting on its own. Uses the boiler's pellet counter where it moved
    that day, otherwise power x feed rate (same rule as the pellet stock)."""
    periods = [p for p in ((_settings().get("warm") or {}).get("periods") or []) if p.get("on", True)]
    periods.sort(key=lambda p: _hm(p["start"]))
    OWN = ("Boiler on its own", "Kedlen selv")

    def which(t):
        d = datetime.datetime.fromtimestamp(t)
        m = d.hour * 60 + d.minute
        for p in periods:
            if d.weekday() not in (p.get("days") or range(7)):
                continue
            a, b = _hm(p["start"]), _hm(p.get("end") or p["start"])
            lo = a - (240 if p.get("kind") == "warmby" else 30)
            if lo <= m <= b + 30:
                return p["name"]
        return None

    silo, est = collections.defaultdict(lambda: collections.defaultdict(float)), collections.defaultdict(lambda: collections.defaultdict(float))
    cover = collections.defaultdict(float)
    cur, prev = None, None
    for r in rows:
        st = r.get("state")
        if prev is not None:
            cover[datetime.date.fromtimestamp(prev["epoch"]).isoformat()] += min(r["epoch"] - prev["epoch"], 60.0)
        if st in RUN_ST and (prev is None or prev.get("state") not in RUN_ST):
            cur = which(r["epoch"])                    # a new burn: who started it
        if prev is not None and prev.get("state") in RUN_ST:
            day = datetime.date.fromtimestamp(prev["epoch"]).isoformat()
            dt = min(r["epoch"] - prev["epoch"], 60.0)
            silo[day][cur] += F.silo_step(prev, r)            # the same per-row fuel as the day summaries
            est[day][cur] += F.row_fuel_kg(prev, dt)
        prev = r
    today = datetime.date.today().isoformat()
    # whole days only (recorded F.WHOLE_DAY_H hours or more, as "per day"), the counter's 1-kg steps spread by the
    # power-based shares, and the owner's correction from counting the pellets (as the pellet stock)
    dlist = sorted(d for d in set(silo) | set(est) if d != today and cover[d] >= F.WHOLE_DAY_H * 3600)[-days:]
    if len(dlist) < 1:
        return None
    use, corr = {}, F.correction_of(_settings())
    for d in dlist:
        e, kg = est[d], sum(silo[d].values())
        es = sum(e.values())
        use[d] = {n: v / es * kg * corr for n, v in e.items()} if kg > 0 and es > 0 else {n: v * corr for n, v in e.items()}
    names = [p["name"] for p in periods] + [None]
    tot = {n: sum(use[d].get(n, 0) for d in dlist) for n in names}
    names = [n for n in names if tot[n] > 0.05]
    allkg = sum(tot.values()) or 1
    lab = lambda n, i: n if n else OWN[i]
    parts_en = ", ".join(f"{lab(n, 0)} {tot[n] / len(dlist):.1f} kg" for n in names)
    parts_da = ", ".join(f"{lab(n, 1)} {tot[n] / len(dlist):.1f} kg".replace(".", ",") for n in names)
    top = max(names, key=lambda n: tot[n])
    share = round(100 * tot[top] / allkg)
    nd = "day" if len(dlist) == 1 else f"{len(dlist)} days"
    nd_da = "dag" if len(dlist) == 1 else f"{len(dlist)} dage"
    lead_en = f"On {datetime.date.fromisoformat(dlist[0]).strftime('%d/%m')}, the only full day recorded so far" if len(dlist) == 1 else f"Average per day over the last {nd} recorded in full"
    lead_da = f"Den {datetime.date.fromisoformat(dlist[0]).strftime('%d/%m')}, det eneste hele døgn optaget indtil videre" if len(dlist) == 1 else f"Gennemsnit pr. dag de sidste {nd_da} der er optaget hele døgnet"
    en = (f"{lead_en}: {parts_en}. {lab(top, 0)} uses the most ({share} %). "
          "Each burn counts for the plan period that started it" + ("; 'Boiler on its own' is when the boiler relit "
          "by itself to keep its water warm" if None in names else "") + ". Sorted with today's heating plan.")
    da = (f"{lead_da}: {parts_da}. {lab(top, 1)} bruger mest ({share} %). "
          "Hver fyring tæller for den periode i varmeplanen der startede den" + ("; 'Kedlen selv' er når kedlen "
          "selv tændte for at holde vandet varmt" if None in names else "") + ". Sorteret efter varmeplanen som den er i dag.")
    return card("pellets_period", "Pellets per heating-plan period", "Piller pr. periode i varmeplanen", en, da,
                {"type": "stack", "labels": [datetime.date.fromisoformat(d).strftime("%d/%m") for d in dlist],
                 "series": [{"name": {"en": lab(n, 0), "da": lab(n, 1)}, "unit": "kg",
                             "data": [round(use[d].get(n, 0), 1) for d in dlist]} for n in names]},
                # kg a day per period, in the plan's order; name None = the boiler relit on its own
                data={"days": len(dlist), "periods": [{"name": n, "kg": round(tot[n] / len(dlist), 1),
                                                       "share": round(100 * tot[n] / allkg)} for n in names]})


def idea_power_levels(rows, days=7):
    """Hours of burning at each power level (last `days` days)."""
    bands = [(0, 40, "under 40 %", "under 40 %"), (40, 60, "40-59 %", "40-59 %"), (60, 80, "60-79 %", "60-79 %"),
             (80, 100, "80-99 %", "80-99 %"), (100, 101, "100 %", "100 %")]
    t_from = rows[-1]["epoch"] - days * 86400 if rows else 0
    secs = [0.0] * len(bands)
    prev = None
    for r in rows:
        if prev is not None and prev.get("state") == 4 and prev["epoch"] >= t_from and prev.get("power_pct") is not None:
            dt = min(r["epoch"] - prev["epoch"], 60.0)
            p = prev["power_pct"]
            for i, (a, b, _, _) in enumerate(bands):
                if a <= p < b:
                    secs[i] += dt
                    break
        prev = r
    tot = sum(secs)
    if tot < 3600:
        return None
    hrs = [round(s / 3600, 1) for s in secs]
    full = round(100 * secs[-1] / tot)
    low = round(100 * (secs[0] + secs[1]) / tot)
    en = (f"Of {tot / 3600:.1f} hours of steady burning: {full} % at full power and {low} % below 60 %. "
          "The burner turns itself down as the boiler nears its target. Lots of low power means the house needs "
          "less than the burner can give: a lower 'stop at' in the heating plan ends those quiet tails sooner.")
    da = (f"Af {tot / 3600:.1f} timers rolig fyring: {full} % ved fuld effekt og {low} % under 60 %. "
          "Brænderen skruer selv ned når kedlen nærmer sig målet. Meget lav effekt betyder at huset skal bruge "
          "mindre end brænderen kan give: en lavere 'stop ved' i varmeplanen afslutter de stille haler før.")
    da = da.replace(f"{tot / 3600:.1f}", f"{tot / 3600:.1f}".replace(".", ","))
    return card("power_levels", "Time at each power level", "Tid ved hver effekt", en, da,
                {"type": "bar", "labels": [b[2] for b in bands],
                 "series": [{"name": {"en": "hours", "da": "timer"}, "unit": "h", "data": hrs}]})


def idea_cooling_vs_outside(rows, room_c=18.0):
    """Each rest of an hour or more: how fast the boiler lost its warmth, against the outside temperature."""
    hourly = (C.read_json(os.path.join(DATA, "weather_cache.json")) or {}).get("hourly") or []
    if not hourly:
        return None
    segs = segments(rows, gap=300)
    pts = []
    for i in range(1, len(segs)):
        st, a, b = segs[i]
        if st not in (0, 1) or segs[i - 1][0] not in (4, 5):
            continue
        j = i
        while j + 1 < len(segs) and segs[j + 1][0] in (0, 1) and segs[j + 1][1] - segs[j][2] < 300:
            j += 1                                     # stopped + waiting in a row = one rest
        b = segs[j][2]
        if b - a < 60 * 60:
            continue
        # the first 3 hours only: later the boiler is near the boiler-room temperature, which we do not measure
        rr = [r for r in rows if a <= r["epoch"] <= min(b, a + 3 * 3600) and r.get("boiler_c") is not None]
        if len(rr) < 10:
            continue
        c0, c1 = rr[0]["boiler_c"], rr[-1]["boiler_c"]
        if c0 - room_c < 15 or c1 - room_c < 2 or c1 >= c0:
            continue
        tau = (rr[-1]["epoch"] - rr[0]["epoch"]) / 3600 / math.log((c0 - room_c) / (c1 - room_c))
        half = tau * math.log(2)
        out = [c for t, c in hourly if a - 1800 <= t <= b + 1800 and c is not None]
        if not out or not (0.3 < half < 24):
            continue
        pts.append([round(statistics.mean(out), 1), round(half, 1), a])
    if not pts:
        return None
    pts = pts[-30:]
    last = pts[-1]
    when = datetime.datetime.fromtimestamp(last[2]).strftime("%d/%m %H:%M")
    en = (f"{len(pts)} {'rest' if len(pts) == 1 else 'rests'} of an hour or more. After the stop at {when} (outside {last[0]:.1f} °C) the boiler lost "
          f"half its warmth above room temperature in {last[1]:.1f} hours. Each dot is one rest: further left = colder "
          "outside. Lower dots = faster cooling, so a 'warm by' start has to begin earlier. The trend gets clearer as "
          "winter brings colder nights.")
    da = (f"{len(pts)} hvil på en time eller mere. Efter stoppet {when} (ude {last[0]:.1f} °C) havde kedlen mistet "
          f"halvdelen af sin varme over rumtemperatur på {last[1]:.1f} timer. Hver prik er et hvil: længere til venstre = "
          "koldere ude. Lavere prikker = hurtigere afkøling, så en 'varm kl.'-start skal begynde tidligere. Mønstret "
          "bliver tydeligere når vinteren giver koldere nætter.")
    da = da.replace(f"{last[0]:.1f}", f"{last[0]:.1f}".replace(".", ",")).replace(f"{last[1]:.1f}", f"{last[1]:.1f}".replace(".", ","))
    return card("cool_outside", "Cooling speed and outside temperature", "Afkøling og udetemperatur", en, da,
                {"type": "xy", "points": [[p[0], p[1]] for p in pts],
                 "xlabel": {"en": "outside °C", "da": "ude °C"},
                 "ylabel": {"en": "hours to lose half the warmth", "da": "timer til halv varme"}})


IDEAS = [idea_start_substeps, idea_warm_by, idea_pellets_per_period, idea_power_levels, idea_cooling_vs_outside, idea_burn_vs_start, idea_cooling, idea_time_per_state, idea_ignition_times, idea_minimum_mode]


def main():
    rows = rows_best()
    cards = []
    for fn in IDEAS:
        try:
            c = fn(rows)
            if c:
                cards.append(c)
        except Exception as e:                       # one broken idea must not hide the others
            print(f"idea {fn.__name__} failed: {e!r}")
    out = {"generated": datetime.datetime.now().isoformat(timespec="seconds"), "rows": len(rows), "cards": cards}
    C.write_json_atomic(OUT, out, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(cards)} cards from {len(rows)} rows -> {OUT}")


if __name__ == "__main__":
    main()
