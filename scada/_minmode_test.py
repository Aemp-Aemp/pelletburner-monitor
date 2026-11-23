"""The boiler's own minimum mode (MINIMUMDRIFT): seen from behaviour (DRIFT -> WAITING by itself), shown in
status (minmode.seen), cleared after 3 days of normal burn ends or by "I turned it off", re-armed by a new burn-out.
Made-up data only."""
import sys, os, tempfile, datetime
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_ext as X
import burner_features as F

res = []
def check(name, cond): res.append(("PASS" if cond else "FAIL", name))
def ep(s): return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()
H = 3600

def run(tr, rows):
    for t, st in rows:
        r = {"epoch": ep(t) if isinstance(t, str) else t, "state": st, "alarm": 0}
        tr.on_row(r, [r])

d = tempfile.mkdtemp()
s = F.Settings(d, lambda m: None)
tr = X.Tracker(None)
mm = F.MinModeWatch(s, tr)
D = F.MinModeWatch.detect

# 1) nothing seen yet
check("no burn-out: not seen", mm.feed(ep("2000-01-01 06:00"))["seen"] is False)

# 2) a burn that ends DRIFT -> PAUSESTOP -> STOPPET (minimum mode off): not seen
run(tr, [("2000-01-01 06:00", 0), ("2000-01-01 06:01", 2), ("2000-01-01 06:02", 3), ("2000-01-01 06:30", 4),
         ("2000-01-01 08:00", 5), ("2000-01-01 08:20", 0)])
check("DRIFT -> PAUSESTOP: not seen", mm.feed(ep("2000-01-01 09:00"))["seen"] is False)

# 3) the plan stops it at low power (DRIFT -> STOPPET with the plan as cause): not seen
tr.cause_fn = lambda frm, to, now: "warm mode (low power)" if to == 0 else None
run(tr, [("2000-01-01 10:00", 2), ("2000-01-01 10:01", 3), ("2000-01-01 10:30", 4), ("2000-01-01 11:00", 0)])
check("web stop (DRIFT -> STOPPET): not seen", mm.feed(ep("2000-01-01 12:00"))["seen"] is False)

# 4) DRIFT -> WAITING with one of our own commands as cause: not the boiler, not seen
tr.cause_fn = lambda frm, to, now: "warm mode (low power)" if (frm, to) == (4, 1) else None
run(tr, [("2000-01-01 12:00", 0), ("2000-01-01 12:01", 2), ("2000-01-01 12:02", 3), ("2000-01-01 12:30", 4),
         ("2000-01-01 13:00", 1)])
check("DRIFT -> WAITING caused by us: not seen", mm.feed(ep("2000-01-01 13:10"))["seen"] is False)

# 5) the boiler burns out by itself: DRIFT -> WAITING, no cause -> seen, with its time
tr.cause_fn = None
run(tr, [("2000-01-01 14:00", 0), ("2000-01-01 14:01", 2), ("2000-01-01 14:02", 3), ("2000-01-01 14:30", 4),
         ("2000-01-01 15:37", 1)])
f = mm.feed(ep("2000-01-01 16:00"))
check("burn-out to WAITING by the boiler: seen", f["seen"] is True and f["last"] == ep("2000-01-01 15:37"))
check("kept in settings (survives the log rolling over)", (s.get("minmode") or {}).get("last") == round(ep("2000-01-01 15:37")))
check("cause 'boiler' also counts", D([{"type": "transition", "epoch": ep("2000-01-01 15:37"), "from": 4, "to": 1,
                                         "cause": "boiler"}], now=ep("2000-01-01 16:00"))["seen"] is True)

# 6) still seen within 3 days, even after normal burns
run(tr, [("2000-01-02 05:00", 2), ("2000-01-02 05:01", 3), ("2000-01-02 05:30", 4), ("2000-01-02 07:00", 5),
         ("2000-01-02 07:20", 0)])
check("seen within 3 days", mm.feed(ep("2000-01-03 15:00"))["seen"] is True)

# 7) 3 days pass with burns that ended without a burn-out: cleared
check("cleared after 3 days with normal burn ends", mm.feed(ep("2000-01-04 15:38"))["seen"] is False)

# 8) 3 days pass with NO burn at all since: still seen (no proof it is off)
only = [{"type": "transition", "epoch": ep("2000-01-01 15:37"), "from": 4, "to": 1}]
check("no burns since: still seen after 3 days", D(only, now=ep("2000-01-05 12:00"))["seen"] is True)

# 9) setting from the log alone (transitions gone) still counts
check("last from settings alone", D([], last=ep("2000-01-04 10:00"), now=ep("2000-01-04 12:00"))["seen"] is True)

# 10) "I turned it off": hidden; a new burn-out after that shows it again
run(tr, [("2000-01-05 05:00", 2), ("2000-01-05 05:01", 3), ("2000-01-05 05:30", 4), ("2000-01-05 06:10", 1)])
check("new burn-out: seen again", mm.feed(ep("2000-01-05 06:20"))["seen"] is True)
mm.turned_off(ep("2000-01-05 06:30"))
f = mm.feed(ep("2000-01-05 06:31"))
check("'I turned it off' hides it", f["seen"] is False and f["off_at"] == round(ep("2000-01-05 06:30")))
run(tr, [("2000-01-05 09:00", 2), ("2000-01-05 09:01", 3), ("2000-01-05 09:30", 4), ("2000-01-05 10:00", 1)])
f = mm.feed(ep("2000-01-05 10:05"))
check("burn-out after 'turned off': re-armed", f["seen"] is True and f["last"] == ep("2000-01-05 10:00"))

# 11) settings survive a restart; the web cannot set them through /api/settings
s2 = F.Settings(d, lambda m: None)
check("minmode kept over a restart", s2.get("minmode")["last"] == round(ep("2000-01-05 10:00"))
      and s2.get("minmode")["off_at"] == round(ep("2000-01-05 06:30")))
ok, _ = s2.update({"minmode": {"last": 1, "off_at": 2}}, save=False)
check("not editable through the settings API", s2.get("minmode")["last"] == round(ep("2000-01-05 10:00")))

# 12) the transition at program start (alarm seen at start) never counts
check("at_start transition ignored", D([{"type": "transition", "epoch": ep("2000-01-01 15:37"), "from": 4, "to": 1,
                                          "at_start": True}], now=ep("2000-01-01 16:00"))["seen"] is False)

for r in res:
    print(*r)
fails = [r for r in res if r[0] != "PASS"]
print(f"{len(res) - len(fails)}/{len(res)} passed")
sys.exit(1 if fails else 0)
