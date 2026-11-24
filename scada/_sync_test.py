"""Offline test: the shared tables and helpers exist once.
 - the TABLES and SHARED blocks inside scada/home.html and scada/dashboard.html are what tools/sync_pages.py would
   write from burner_common.py and scada/shared.js (after changing a source: py tools\\sync_pages.py)
 - no page and no Python module keeps a hand-typed copy
 - the Dockerfile copies every Python module the program needs"""
import glob, io, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import burner_common as C
import burner_ext as X
import burner_features as F
import burner_notify as N
import burner_responder as R
import sync_pages

FAILED = 0


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name)
    FAILED += 0 if cond else 1


def read(rel):
    with io.open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n")


stale = sync_pages.sync(check=True)
check("both pages are up to date with burner_common.py and scada/shared.js" + (f" {stale}" if stale else ""), not stale)
shared = sync_pages.shared_js()
names = re.findall(r"^const (\w+)=", shared, re.M)
check("shared.js holds the helpers", names == ["LOC", "esc", "fmt", "median", "clock", "niceStep", "niceAxis", "axisDec", "api",
                                             "LH_TXT", "lhT", "LH_KW", "lhVal", "lhBad", "lhBoiler",   # + the lighting help card
                                             "UPD_TXT", "updT"])                                         # + the update card
check("one clock style (17:15) in both languages: times via clock(), not the browser's locale",
      all("toLocaleTimeString" not in read(p) for p in sync_pages.PAGES))
for page in sync_pages.PAGES:
    text = read(page)
    tb =sync_pages.block_in(text, "TABLES")
    got = json.loads(tb[tb.index("{"):tb.rindex("}") + 1]) if tb else None
    check(f"{page}: the TABLES block is burner_common's tables", got == json.loads(json.dumps(sync_pages.tables())))
    check(f"{page}: the SHARED block is scada/shared.js", sync_pages.block_in(text, "SHARED") == shared)
    rest = text.replace(tb or "", "").replace(sync_pages.block_in(text, "SHARED") or "", "")
    check(f"{page}: both blocks come before the page's own script code",
          text.index("==SHARED-END==") < text.index("const I18N"))
    typed = [w for w in ('"WAITING","STARTING"', '"AFVENTER","OPSTART"', '2:"BURNER DISCONNECTED', '2:"BRÆNDER FRAKOBLET', "[4,5,6,12]", "[1,2,3,7,8")
             if w in rest]
    check(f"{page}: no hand-typed copy of a table" + (f" {typed}" if typed else ""), not typed)
    again = [n for n in names + ["TABLES"] if re.search(r"^\s*(?:const|let|var|(?:async )?function) " + n + r"\b", rest, re.M)]
    check(f"{page}: no second definition of a shared helper" + (f" {again}" if again else ""), not again)
    check(f"{page}: the flame limit is the table's, not a typed 17",
          not re.search(r"(?:flame|lux|luxV|fl|v)\)?\s*>=?\s*17\b", rest))

# ---- Python: one copy, the other modules use it
check("state names: one table", X.STATE_LABELS is C.STATE_LABELS and F.STATE_LABELS is C.STATE_LABELS
      and R.STATE_LABELS is C.STATE_LABELS)
check("alarm names: one table", R.ALARM_LABELS is C.ALARM_LABELS and X.ALARM_TEXT is C.ALARM_TEXT
      and N.ALARM_EN == {k: v for k, v in C.ALARM_TEXT.items() if k} and N.ALARM_DA == C.ALARM_TEXT_DA)
check("sources: one table", X.SOURCES is C.SOURCES and R.SOURCES is C.SOURCES)
check("never reset automatically: one list", F.AUTO_RESET_NEVER == set(C.ALARM_NEVER_AUTO_RESET)
      and not set(C.ALARM_AUTO_RESET_ALLOWED) & set(C.ALARM_NEVER_AUTO_RESET))
check("every alarm has its names (boiler, English, Danish, page)",
      set(C.ALARM_LABELS) - {0} == set(C.ALARM_TEXT) - {0} == set(C.ALARM_TEXT_DA) == set(C.ALARM_PAGE["en"])
      == set(C.ALARM_PAGE["da"]))
check("Danish alarm words: the phone alerts say what the page says",
      all(C.ALARM_TEXT_DA[k].upper() == C.ALARM_PAGE["da"][k].upper() for k in C.ALARM_TEXT_DA))
check("every state has its names", set(C.STATE_LABELS) == set(C.STATE_WORDS) == set(range(8))
      and all(len(v) == 8 for v in C.STATE_PAGE.values()))
check("/status.json labels", C.labels() == {"states": C.STATE_LABELS, "alarms": C.ALARM_LABELS, "sources": C.SOURCES})
dup = []
for p in sorted(glob.glob(os.path.join(ROOT, "burner_*.py"))):
    if os.path.basename(p) != "burner_common.py":
        src = read(os.path.basename(p))
        dup += [f"{os.path.basename(p)}: {n}" for n in ("STATE_LABELS", "ALARM_LABELS", "STATE_WORDS", "ALARM_TEXT", "SOURCES")
                if re.search(r"^" + n + r"\s*=\s*\{", src, re.M)]
check("no module types a table again" + (f" {dup}" if dup else ""), not dup)

# ---- Dockerfile: every module of the program is copied into the image
docker = read("Dockerfile")
copied = set(re.search(r"^COPY ((?:\S+\.py ?)+)\./", docker, re.M).group(1).split())
need = {os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "burner_*.py"))}
check("Dockerfile copies every burner_*.py" + (f" (missing: {sorted(need - copied)})" if need - copied else ""), need <= copied)

print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
