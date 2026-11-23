"""Offline test: pellets at home (stock minus burned)."""
import sys, os, json, time, tempfile, datetime
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F
import burner_ext as X

FAILED = 0


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name)
    FAILED += 0 if cond else 1


d = tempfile.mkdtemp(prefix="stock_test_")
s = F.Settings(d, lambda m: None)
store = X.Store(d, 365, lambda m: None)


class M:
    latest_cfg = None


opt = F.Optimiser(s, store, M(), lambda m: None)
check("no stock until entered", opt.pellet_stock() is None)
check("negative refused", s.set_stock(-1)[0] is False)
check("too much refused", s.set_stock(10 ** 6)[0] is False)
check("bad reserve refused, stock untouched", s.set_stock(500, reserve_kg=-3)[0] is False and s.get("pellets")["stock"] is None)

# 5 past days, each 6 h at 50 % power with 2200 g/h -> 6.6 kg/day estimated
today = datetime.date.today()
for k in range(5, 0, -1):
    day = today - datetime.timedelta(days=k)
    t0 = datetime.datetime.combine(day, datetime.time(6)).timestamp()
    rows = [{"epoch": t0 + i * 60, "state": 4, "power_pct": 50, "fuel_gph": 2200} for i in range(361)]
    with open(os.path.join(d, f"hist-{day.isoformat()}.jsonl"), "w") as fh:
        fh.write("\n".join(json.dumps(r) for r in rows) + "\n")

ok, _ = s.set_stock(500)
p = s.get("pellets")
p["stock"]["at"] = datetime.datetime.combine(today - datetime.timedelta(days=3), datetime.time(9)).timestamp()
s.set_internal("pellets", "stock", p["stock"])       # entered 3 days ago at 09:00 (mid-burn that day)
st = opt.pellet_stock()
# burned: half of day -3 (09:00-12:00 of 06:00-12:00) = 3.3 kg + days -2, -1 = 13.2 kg -> 16.5
check("burned since entry (partial first day)", abs(st["burned"] - 16.5) < 0.2)
check("left = entered - burned", abs(st["left"] - (500 - st["burned"])) < 0.01)
check("per day from finished burning days", st["per_day"] == 6.6)
check("days left", st["days_left"] == round(st["left"] / 6.6))
check("buy-by keeps the reserve", abs((st["buy_by"] - time.time()) / 86400 - (st["left"] - 150) / 6.6) < 0.01)
s2 = F.Settings(d, lambda m: None)
check("stock survives a restart", s2.get("pellets")["stock"]["kg"] == 500)
print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
