"""Software update from the page: the server side, the texts, the card defaults and the Pi's updater script.

1. burner_ext.update_info: every state the pages can show, from made-up data/update.json files.
2. The routes: only the words check / retry / back reach the request file, never anything from the browser.
3. The texts (scada/shared.js UPD_TXT): English and Danish have the same keys, every state has words.
4. New SD cards: pelletpi/settings.txt points at the public repository; make_card.py --repo-url changes it.
5. pi/pelletpi-update.sh itself, run by bash against a made-up release repository, a stand-in `docker` and a
   stand-in monitor (no Docker, no Pi, no boiler, no internet): what it decides and how each failure ends.
   Needs bash and git (Git for Windows has both); without them this part is left out with a note.
"""
import http.server, json, os, re, shutil, stat, subprocess, sys, tempfile, threading, time, types
import urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import burner_ext as X
import burner_responder as R

FAILED = 0


def check(name, cond, extra=""):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra and not cond else ""), flush=True)
    FAILED += 0 if cond else 1


def rd(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


TMP = tempfile.mkdtemp(prefix="pb_upd_")

# ---------------------------------------------------------------- 1) the states the pages show
d1 = os.path.join(TMP, "d1")
os.makedirs(d1)
NOW = time.time()


def info(obj, age=0, req_age=None, now=None):
    p = os.path.join(d1, "update.json")
    if obj is None:
        if os.path.exists(p):
            os.remove(p)
    else:
        with open(p, "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))
        os.utime(p, (NOW - age, NOW - age))
    rq = os.path.join(d1, "update-request")
    if os.path.exists(rq):
        os.remove(rq)
    if req_age is not None:
        with open(rq, "w") as f:
            f.write("check\n")
        os.utime(rq, (NOW - req_age, NOW - req_age))
    return X.update_info(d1, now or NOW)


def U(**kw):
    d = {"v": 2, "current": "v1.0.0", "latest": "v1.0.0", "available": False, "auto": True, "phase": "idle",
         "code": "uptodate", "why": None, "from": None, "to": None, "result": "", "checked": int(NOW), "previous": None}
    d.update(kw)
    return d


check("no update.json: no updater here", info(None) == {"current": None} and X.update_info(None) == {"current": None})
check("broken update.json: no updater here", info("{not json") == {"current": None} and info("[1]") == {"current": None})
v = info(U())["view"]
check("up to date: one button, nothing busy", v["key"] == "uptodate" and v["btn"] == "check" and not v["busy"] and v["back"] is None)
v = info(U(code="offline"))["view"]
check("no internet", v["key"] == "offline" and v["btn"] == "check")
for ph in X.UPDATE_PHASES:
    v = info(U(phase=ph, code=None, to="v1.1.0", latest="v1.1.0", available=True))["view"]
    check(f"at work ({ph}): busy, no buttons", v["key"] == "ph_" + ph and v["busy"] and v["btn"] is None and v["back"] is None
          and v["to"] == "v1.1.0")
v = info(U(phase="install", code=None, to="v1.1.0"), age=1000)["view"]
check("a step that stopped (power cut): interrupted, the button is back", v["key"] == "interrupted" and not v["busy"] and v["btn"] == "check")
check("the long download step gets an hour", info(U(phase="download", code=None), age=1000)["view"]["busy"]
      and info(U(phase="download", code=None), age=4000)["view"]["key"] == "interrupted")
PREV = {"version": "v1.0.0", "until": NOW + 86400}
v = info(U(current="v1.1.0", latest="v1.1.0", code="updated", to="v1.1.0", previous=PREV, **{"from": "v1.0.0"}))["view"]
check("updated: 'now on X' and Go back to the old one", v["key"] == "updated" and v["current"] == "v1.1.0" and v["back"] == "v1.0.0")
v = info(U(current="v1.1.0", code="updated", previous={"version": "v1.0.0", "until": NOW - 5}))["view"]
check("Go back is gone when the kept version has run out", v["back"] is None)
v = info(U(latest="v1.1.0", available=True, code="rolled_back", why="boiler", to="v1.1.0", **{"from": "v1.0.0"}))["view"]
check("rolled back: says which version and why", v["key"] == "rolled_back" and v["to"] == "v1.1.0" and v["current"] == "v1.0.0"
      and v["why"] == "boiler" and v["btn"] == "check")
check("an unknown reason is left out", info(U(code="rolled_back", why="<b>x"))["view"]["why"] is None)
v = info(U(latest="v1.1.0", available=True, code="available", auto=False))["view"]
check("found while auto-update is off: the button installs it", v["key"] == "available" and v["btn"] == "install" and v["latest"] == "v1.1.0")
check("found at night while the boiler works: the button installs it", info(U(latest="v1.1.0", code="later"))["view"]["btn"] == "install")
v = info(U(latest="v1.1.0", available=True, code="bad_skipped"))["view"]
check("failed here before: the button tries it again", v["key"] == "bad_skipped" and v["btn"] == "retry")
for code in X.UPDATE_CODES:
    check(f"result '{code}' is passed on", info(U(code=code))["view"]["key"] == code)
check("an unknown result reads as an error", info(U(code="something else"))["view"]["key"] == "error")
v = info(U(), req_age=5)["view"]
check("just pressed: checking, busy", v["key"] == "asked" and v["busy"] and v["btn"] is None)
check("a left-over request does not hide the step the updater is at",
      info(U(phase="waiting", code=None, to="v1.1.0"), req_age=300)["view"]["key"] == "ph_waiting")
v = info(U(), req_age=300)["view"]
check("nobody took the request: says so, the button is back", v["key"] == "no_updater" and not v["busy"] and v["btn"] == "check")
old = {"current": "v1.0.0", "latest": "v1.1.0", "available": True, "auto": True, "result": "x", "checked": 1}
i = info(old)
check("file from the first updater: still shown, with the button", i["view"]["key"] == "old" and i["view"]["btn"] == "check"
      and i["latest"] == "v1.1.0" and i["view"]["back"] is None)
i = info(U(current="v1<script>", latest="x" * 90, to="a b\"c"))
check("version names are cut down to plain short text", i["current"] == "v1script" and len(i["latest"]) == 40 and i["view"]["to"] == "abc")

# ---------------------------------------------------------------- 2) the routes
PORT = 8841
mod = R.Module(0x01)
SRV = R.start_http(mod, PORT, lambda *a: None)
base = f"http://127.0.0.1:{PORT}/"


def get(p):
    with urllib.request.urlopen(base + p, timeout=5) as r:
        return r.status, json.loads(r.read())


def post(p, body, hdr=True, ctype="application/json"):
    h = {"Content-Type": ctype}
    if hdr:
        h["X-Burner"] = "1"
    req = urllib.request.Request(base + p, data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


d2 = os.path.join(TMP, "d2")
os.makedirs(d2)
rq = os.path.join(d2, "update-request")
check("no data folder: nothing to ask", post("api/update/check", {})[0] == 400)
mod.store = types.SimpleNamespace(dir=d2)
with open(os.path.join(d2, "update.json"), "w") as f:
    json.dump(U(), f)
s, j = get("api/update")
check("GET /api/update: the file plus the view", s == 200 and j["current"] == "v1.0.0" and j["view"]["key"] == "uptodate")
for what in ("check", "retry", "back"):
    check(f"{what}: refused without the X-Burner header", post("api/update/" + what, {}, hdr=False)[0] == 403 and not os.path.exists(rq))
    check(f"{what}: refused when not sent as JSON", post("api/update/" + what, {}, ctype="text/plain")[0] == 403 and not os.path.exists(rq))
    s, j = post("api/update/" + what, {"url": "https://example.invalid/x.git", "tag": "v9.9.9", "cmd": "reboot", "what": "back"})
    check(f"{what}: the request file holds that one word, nothing from the browser",
          s == 200 and j["ok"] and open(rq).read() == what + "\n")
    os.remove(rq)
for bad in ("api/update/now", "api/update/install", "api/update/check/../back", "api/update/"):
    s, j = post(bad, {})
    check(f"POST {bad}: no such request", s != 200 and not os.path.exists(rq))
req = urllib.request.Request(base + "api/update/check", data=b"{}", method="POST",
                             headers={"Content-Type": "application/json", "X-Burner": "1", "Host": "evil.example"})
try:
    urllib.request.urlopen(req, timeout=5)
    code = 200
except urllib.error.HTTPError as e:
    code = e.code
check("a request under a foreign host name is refused", code == 403 and not os.path.exists(rq))
check("update_request: only the three words", X.update_request(d2, "check; reboot") == (False, "unknown request")
      and X.update_request(d2, "") == (False, "unknown request") and not os.path.exists(rq))
post("api/update/check", {})
os.utime(rq, (time.time(), time.time()))             # file times are real; the test clock may be moved (--clock)
check("after a press the view says 'checking'", get("api/update")[1]["view"]["key"] == "asked")
s, j = post("api/update/back", {})
check("a second press while it works is refused; the first request stays", s == 409 and open(rq).read() == "check\n")
os.remove(rq)
with open(os.path.join(d2, "update.json"), "w") as f:
    json.dump(U(phase="waiting", code=None, to="v1.1.0"), f)
os.utime(os.path.join(d2, "update.json"), (time.time(), time.time()))
check("no request while the updater is at work", post("api/update/back", {})[0] == 409 and not os.path.exists(rq))
mod.store = None
resp = rd("burner_responder.py")
route = resp[resp.index('if path in ("/api/update/check"'):]
route = route[:route.index("nt = getattr")]
check("the route never reads the body", "body" not in route.replace("the body", ""))
check("the monitor itself downloads nothing for updates", "urlopen" not in route and "subprocess" not in route)

# ---------------------------------------------------------------- 3) the texts
js = rd("scada/shared.js")
blk = js[js.index("const UPD_TXT={"):js.index("const updT=")]
langs = {}
for lang, part in (("en", blk[blk.index(" en:{"):blk.index("\n da:{")]), ("da", blk[blk.index("\n da:{"):])):
    langs[lang] = dict(re.findall(r'(\w+):"((?:[^"\\]|\\.)*)"', part))
check("update texts: en and da have the same keys", set(langs["en"]) == set(langs["da"]) and len(langs["en"]) > 30,
      sorted(set(langs["en"]) ^ set(langs["da"])))
holes = lambda s: sorted(set(re.findall(r"\{(\w+)\}", s)))
bad = [k for k in langs["en"] if holes(langs["en"][k]) != holes(langs["da"].get(k, ""))]
check("update texts: the same {placeholders} in both languages", not bad, bad)
need = (["title", "ver", "note", "btn_check", "btn_install", "btn_retry", "btn_back", "back_confirm", "asked", "no_updater",
         "old", "interrupted"] + ["ph_" + p for p in X.UPDATE_PHASES] + list(X.UPDATE_CODES) + ["why_" + w for w in X.UPDATE_WHY])
miss = [k for k in need if not langs["en"].get(k) or not langs["da"].get(k)]
check("every state, result and reason has words in both languages", not miss, miss)
check("the 'no internet' text gives the hotspot tip", "hotspot" in langs["en"]["offline"] and "hotspot" in langs["da"]["offline"])
same = [k for k in langs["en"] if langs["en"][k] == langs["da"][k] and k != "ver"]
check("no Danish text is left in English", not same, same)
for page in ("home.html", "dashboard.html"):
    h = rd("scada/" + page)
    check(f"{page}: has the update card and its buttons", 'id="updCard"' in h and "function updDraw()" in h
          and "updLoad();" in h and 'closest("[data-upd]")' in h and "back_confirm" in h)
check("the pages send only the word", 'api("api/update/"+what,{})' in js)

# ---------------------------------------------------------------- 4) new SD cards
import make_card  # noqa: E402
st = rd("pi/firstboot/settings.txt")
m = re.search(r"^REPO_URL=(\S*)$", st, re.M)
PUBLIC = "https://github.com/Aemp-Aemp/pelletburner-monitor.git"
check("settings.txt: REPO_URL is the public repository (a new card gets updates)", bool(m) and m.group(1) == PUBLIC, m and m.group(1))
check("settings.txt: auto-update is on, and can be turned off there", re.search(r"^AUTO_UPDATE=yes$", st, re.M) and "yes or no" in st)
out = os.path.join(TMP, "card")
card, z = make_card.build(out, None, with_app=False)
got = open(os.path.join(card, "pelletpi", "settings.txt"), "rb").read()
check("make_card without --repo-url: the card checks the public repository", ("REPO_URL=" + PUBLIC + "\n").encode() in got and b"\r" not in got)
card, z = make_card.build(out, "https://example.invalid/me/fork.git", with_app=False)
got = open(os.path.join(card, "pelletpi", "settings.txt"), "rb").read()
check("make_card --repo-url: a fork's own address", b"REPO_URL=https://example.invalid/me/fork.git\n" in got and PUBLIC.encode() not in got.split(b"AUTO_UPDATE")[0].split(b"REPO_URL=")[1])
up = rd("pi/pelletpi-update.sh")
check("updater: an empty or CHANGE-ME address still means off", '"" | *CHANGE-ME*) finish no_repo' in up)
fb = rd("pi/firstboot/firstboot.sh")
check("firstboot hands REPO_URL to the updater", "REPO_URL=$url sh" in fb and "setting REPO_URL" in fb)
ins = rd("pi/install-auto-update.sh")
check("the updater is replaced in one move (a running update keeps its copy)", "pelletpi-update.new /usr/local/bin/pelletpi-update" in ins)
check("after a start the updater tidies up (power cut)", "OnBootSec=" in ins)
check("changed units take effect: daemon-reload, then the timer and the path unit are restarted",
      ins.index("systemctl daemon-reload") < ins.index("systemctl restart pelletpi-update.timer pelletpi-update-check.path"))
check("a half-made local history (tarball install) is removed again", 'rm -rf "$APP/.git"' in up)
check("the program folder is handed back without following links", "chown -h -R" in up)
check("status.json tells when the monitor started (the updater's 'quiet' rule)", '"started": self.started' in rd("burner_responder.py"))
m2 = R.Module(0x01)
mk = os.path.join(TMP, "last_rx")
m2.rx_mark = mk
sn = m2.feed_snapshot(since=time.time())
check("status.json: uptime by the monotonic clock, no rx_age before the first frame",
      isinstance(sn.get("uptime"), (int, float)) and sn["uptime"] >= 0 and sn.get("rx_age") is None, sn.get("uptime"))
m2.note_rx()
check("a frame from the boiler writes data/last_rx (the updater: the monitor still talks to the boiler)",
      os.path.isfile(mk) and open(mk).read().strip().isdigit() and not os.path.exists(mk + ".tmp"))
os.utime(mk, (1000, 1000))
m2.note_rx()
check("data/last_rx is written at most every 30 s", os.path.getmtime(mk) == 1000)
sn = m2.feed_snapshot(since=time.time())
check("status.json: rx_age after a frame", isinstance(sn.get("rx_age"), (int, float)) and 0 <= sn["rx_age"] < 60, sn.get("rx_age"))
m2.rx_mark = os.path.join(TMP, "no", "such", "dir", "last_rx")
m2._rx_mark_at = None
m2.note_rx()
check("a heartbeat that cannot be written never stops the serial loop", m2.last_rx > 0)
m3 = R.Module(0x01)
mk3 = os.path.join(TMP, "full", "last_rx")
os.makedirs(mk3 + ".tmp")                             # the new file cannot be made (like a full card)
with open(mk3, "w") as f:
    f.write("0\n")
os.utime(mk3, (1000, 1000))
m3.rx_mark = mk3
m3.note_rx()
check("a full card: the heartbeat still moves data/last_rx's time (needs no free space)",
      os.path.getmtime(mk3) > time.time() - 60 and m3.last_rx > 0, os.path.getmtime(mk3))

# ---------------------------------------------------------------- 5) the updater script, run for real
BASH = None
for cand in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"), r"C:\Program Files\Git\bin\bash.exe",
             "/bin/bash", "/usr/bin/bash"):
    if os.path.isfile(cand):
        BASH = cand
        break
GIT = shutil.which("git")
if not GIT and BASH and BASH.lower().endswith("bash.exe"):
    cand = os.path.join(os.path.dirname(os.path.dirname(BASH)), "cmd", "git.exe")
    GIT = cand if os.path.isfile(cand) else None


def fw(p):
    return p.replace("\\", "/")


def git(cwd, *a):
    r = subprocess.run([GIT, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "core.autocrlf=false",
                        "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *a],
                       cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {a}: {r.stderr}")
    return r.stdout.strip()


class Monitor:
    """Stands in for the program on port 8787: answers by the 'image' the stand-in docker says is running."""

    def __init__(self):
        self.fake = None
        self.data = None            # the Pi's data folder: the phase in update.json is noted at every status read
        self.phases = []
        self.behave = {}            # image -> ok | down | status | settings | nolink
        self.state, self.connected, self.last_rx_age = 0, True, 1
        self.up_age = 3600          # how long the monitor has run ("uptime"; "started" in status.json)
        self.jump = 0               # the wall clock jumped by this much since the start ("started" looks older)
        self.down_polls = 0         # the first N status reads get no answer (a missed look)
        self.busy_polls = 0         # the first N status reads say state 5 (burning)
        mon = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                try:
                    img = open(os.path.join(mon.fake, "running")).read().strip()
                except OSError:
                    img = ""
                how = mon.behave.get(img, "ok")
                path = self.path.split("?")[0]
                if path == "/status.json" and mon.down_polls > 0:
                    mon.down_polls -= 1
                    how = "down"
                if how == "down":
                    body, code = b"starting", 503
                elif path == "/status.json":
                    try:
                        with open(os.path.join(mon.data, "update.json")) as fh:
                            mon.phases.append(json.load(fh).get("phase"))
                    except (OSError, ValueError, TypeError, AttributeError):
                        mon.phases.append(None)
                    state = mon.state
                    if mon.busy_polls > 0:
                        mon.busy_polls -= 1
                        state = 5
                    now = time.time()
                    con = mon.connected and how != "nolink"
                    obj = {"connected": con, "now": now, "version": img,
                           "last_rx": None if mon.last_rx_age is None else now - mon.last_rx_age,
                           "started": now - mon.up_age - mon.jump, "uptime": mon.up_age, "rx_age": mon.last_rx_age,
                           "latest": {"epoch": now, "fields": {"state": state}}}
                    body, code = json.dumps([1] if how == "status" else obj).encode(), 200
                elif path == "/api/settings":
                    body, code = (b"broken", 500) if how == "settings" else (json.dumps({"settings": {"schedule": {}}}).encode(), 200)
                else:
                    body, code = b"?", 404
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


DOCKER = r'''#!/bin/bash
# stand-in for docker in scada/_update_test.py: images are names in files, nothing is built or started
F=$FAKE
echo "$*" >> "$F/docker.log"
tagf() { echo "$F/tags/$(printf '%s' "$1" | tr ':/' '__')"; }
case "$1 ${2:-}" in
  "compose version") exit 0 ;;
  "compose build") if [ -e "$F/build_fail" ]; then echo "no space left on device"; exit 1; fi
                   if [ -e "$F/build_slow" ]; then sleep 8; fi
                   if [ -e "$F/same_image" ]; then exit 0; fi                 # only pi/ changed: the same image id
                   echo "img-$(cat VERSION)" > "$(tagf app-burner)" ;;
  "compose up") if [ -e "$F/up_fail" ]; then echo "up failed"; exit 1; fi    # docker could not start it
                if [ -e "$F/unknown_after_up" ]; then touch "$F/docker_unknown"; fi
                rm -f "$F/stopped"                                          # a stopped container runs again
                if [ -e "$F/up_noop" ]; then exit 0; fi                       # says yes, but the old one runs on
                cat "$(tagf app-burner)" > "$F/running" ;;
  "inspect -f") case "$3" in *Env*) cat "$F/env" 2>/dev/null ;; *Config*) echo app-burner ;;
                *State*) if [ -e "$F/docker_unknown" ]; then echo "Cannot connect to the Docker daemon" >&2; exit 1; fi
                         if [ -e "$F/stopped" ]; then echo false; else echo true; fi ;;
                *) cat "$F/running" ;; esac ;;
  "image inspect") if [ "$3" = -f ]; then cat "$(tagf "$5")" 2>/dev/null; else [ -e "$(tagf "$3")" ]; fi ;;
  "image prune" | "builder prune") ;;
  tag\ *) src=$2; if [ -e "$(tagf "$src")" ]; then src=$(cat "$(tagf "$src")"); fi; echo "$src" > "$(tagf "$3")" ;;
  rmi\ *) rm -f "$(tagf "$2")" ;;
  *) echo "docker stand-in: unknown $*" >&2; exit 9 ;;
esac
'''


class Shell:
    """One bash for all the runs: each run is its child. (A fresh Git-for-Windows bash per run can stall for 20 s
    while Windows looks up the user account; a child inherits the answer.)"""

    def __init__(self):
        self.bin = os.path.join(TMP, "bin")
        os.makedirs(self.bin)
        # stat / chown / df: account and drive look-ups are slow in Git for Windows; here they have nothing to find
        for name_, text in (("docker", DOCKER), ("flock", "#!/bin/bash\nexit 0\n"),
                            ("sync", '#!/bin/bash\necho sync >> "$FAKE/docker.log"\n'),
                            ("python3", '#!/bin/bash\nexec "$REAL_PY" "$@"\n'),
                            ("stat", "#!/bin/bash\necho tester\n"), ("chown", '#!/bin/bash\necho chown >> "$FAKE/docker.log"\n'),
                            ("df", "#!/bin/bash\necho 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
                                   "echo '/dev/x 30000000 29000000 1000000 97% /'\n")):
            p = os.path.join(self.bin, name_)
            with open(p, "w", newline="\n") as f:
                f.write(text)
            os.chmod(p, 0o755)
        # no_proxy: on Windows urllib otherwise asks the system proxy settings about 127.0.0.1, which can take 20 s
        env = dict(os.environ, REAL_PY=fw(sys.executable), PB_STUBS=fw(self.bin), no_proxy="*", NO_PROXY="*")
        env.pop("PELLETPI_TEST", None)
        self.p = subprocess.Popen([BASH], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  env=env, text=True, encoding="utf-8", errors="replace")
        # in front of everything (Git for Windows puts its own folders first when bash starts)
        self.run('export PATH="$(cygpath -u "$PB_STUBS" 2>/dev/null || echo "$PB_STUBS"):$PATH"; hash -r')

    def run(self, line, timeout=300):
        """Run one command line; returns its exit code."""
        guard = threading.Timer(timeout, self.p.kill)
        guard.start()
        try:
            self.p.stdin.write(line + '\necho "@@RC=$?"\n')
            self.p.stdin.flush()
            for out in self.p.stdout:
                if out.startswith("@@RC="):
                    return int(out[5:])
            raise RuntimeError("the shell ended (a run took too long?)")
        finally:
            guard.cancel()

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(10)
        except Exception:
            self.p.kill()


SH = None


class Pi:
    """A made-up Pi: the program folder, /var/lib/pelletpi, the stand-in commands and the release repository."""

    def __init__(self, remote, mon, name, version="v1.0.0", tarball=False, shallow=False, **conf):
        self.root = os.path.join(TMP, name)
        self.app, self.state, self.fake = (os.path.join(self.root, x) for x in ("app", "state", "fake"))
        for d in (self.state, os.path.join(self.fake, "tags")):
            os.makedirs(d)
        if shallow:                                    # like firstboot: git clone --depth 1 of the branch
            git(TMP, "clone", "-q", "--depth", "1", "file:///" + fw(remote).lstrip("/"), fw(self.app))
        else:
            git(TMP, "clone", "-q", fw(remote), fw(self.app))
            git(self.app, "checkout", "-q", "--detach", version)
        if tarball:                                    # installed from app.tar.gz: no .git folder
            shutil.rmtree(os.path.join(self.app, ".git"), onerror=lambda f, p, e: (os.chmod(p, stat.S_IWRITE), f(p)))
        self.data = os.path.join(self.app, "data")
        os.makedirs(self.data)
        with open(os.path.join(self.data, "settings.json"), "w") as f:
            json.dump({"schedule": {"enabled": True}, "setup": {"done": True}}, f)
        with open(os.path.join(self.app, ".env"), "w") as f:
            f.write("BURNER_KEEP_DAYS=30\n")
        self.keep = {p: open(os.path.join(self.app, p), "rb").read() for p in ("data/settings.json", ".env")}
        with open(os.path.join(self.state, "version"), "w") as f:
            f.write(version + "\n")
        self.img(f"img-{version}")
        with open(os.path.join(self.fake, "tags", "app-burner"), "w", newline="\n") as f:
            f.write(f"img-{version}\n")
        self.mon, self.remote = mon, remote
        self.conf = dict(APP=fw(self.app), REPO_URL=fw(remote), AUTO_UPDATE="yes", STATE=fw(self.state),
                         URL=f"http://127.0.0.1:{mon.port}", CARD_SETTINGS=fw(os.path.join(self.root, "none.txt")),
                         STEP=0, START_TRIES=2, LINK_TRIES=2, WAIT_STEP=1, WAIT_PAGE=3, WAIT_AUTO=0, QUIET_WAIT=0,
                         DEAD_WAIT=0, MIN_FREE_KB=0)
        self.conf.update(conf)

    def rx(self, age, name="last_rx"):   # the monitor last heard the boiler `age` seconds ago (data/last_rx)
        p = os.path.join(self.data, name)
        with open(p, "w") as f:
            f.write("0\n")
        os.utime(p, (time.time() - age, time.time() - age))

    def img(self, name):
        with open(os.path.join(self.fake, "running"), "w", newline="\n") as f:
            f.write(name + "\n")

    def run(self, mode, ask=None, test=True, env="", **conf):
        """Run the updater once. ask = the word the page left; env = more NAME=value for the command line.
        Returns update.json."""
        self.mon.fake, self.mon.data = self.fake, self.data
        c = dict(self.conf, **conf)
        cf = os.path.join(self.root, "conf")
        with open(cf, "w", newline="\n") as f:
            f.write("".join(f'{k}="{v}"\n' for k, v in c.items()))
        if ask is not None:
            with open(os.path.join(self.data, "update-request"), "w", newline="\n") as f:
                f.write(ask)
        open(os.path.join(self.fake, "docker.log"), "w").close()
        t0 = time.time()
        out = os.path.join(self.root, "out.txt")
        self.rc = SH.run(f"{env} PELLETPI_CONF='{fw(cf)}' FAKE='{fw(self.fake)}' " + ("PELLETPI_TEST=1 " if test else "")
                         + f"bash '{fw(os.path.join(ROOT, 'pi', 'pelletpi-update.sh'))}' {mode} > '{fw(out)}' 2>&1 < /dev/null")
        with open(out, encoding="utf-8", errors="replace") as f:
            self.out = f.read()
        if os.environ.get("PB_UPD_TIMING"):
            print(f"  ({mode} {ask!r}: {time.time() - t0:.1f} s)", flush=True)
        try:
            with open(os.path.join(self.data, "update.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def docker(self):
        return open(os.path.join(self.fake, "docker.log")).read()

    def running(self):
        return open(os.path.join(self.fake, "running")).read().strip()

    def tag(self, name="app-burner"):
        try:
            return open(os.path.join(self.fake, "tags", name.replace(":", "_"))).read().strip()
        except OSError:
            return None

    def head(self):                 # the version of the files in the program folder
        return open(os.path.join(self.app, "VERSION")).read().strip()

    def version(self):
        return open(os.path.join(self.state, "version")).read().strip()

    def has(self, name):
        return os.path.exists(os.path.join(self.state, name))

    def untouched(self):            # settings and .env exactly as before, and no request / journal left behind
        return (all(open(os.path.join(self.app, p), "rb").read() == b for p, b in self.keep.items())
                and not os.path.exists(os.path.join(self.data, "update-request")) and not self.has("in-progress"))

    def on(self, v):                # everything says version v: running image, image name, files, version file
        return (self.running(), self.tag(), self.head(), self.version()) == (f"img-{v}", f"img-{v}", v, v)


def make_remote():
    """The release repository: v1.0.0 and the pre-release v1.1.0-rc1. release() adds more."""
    rem = os.path.join(TMP, "remote")
    os.makedirs(os.path.join(rem, "pi"))
    git(rem, "init", "-q")
    for name_, text in ((".gitignore", "data/\n.env\n"), ("docker-compose.yml", "services: {}\n"),
                        ("pi/pelletpi-update.sh", "#!/bin/bash\nset -e\necho updater\n"),
                        ("pi/install-auto-update.sh", '#!/bin/sh\nset -e\necho "$REPO_URL" >> "$FAKE/refreshed"\n')):
        with open(os.path.join(rem, name_), "w", newline="\n") as f:
            f.write(text)
    release(rem, "v1.0.0")
    release(rem, "v1.1.0-rc1")
    return rem


def release(rem, tag):
    with open(os.path.join(rem, "VERSION"), "w", newline="\n") as f:
        f.write(tag + "\n")
    git(rem, "add", "-A")
    git(rem, "commit", "-q", "-m", tag)
    git(rem, "tag", tag)


# Git for Windows starts every little command slowly (the whole set takes about 8 minutes there, seconds on Linux,
# where GitHub runs all of it on every push). On Windows the main cases run; all of them with PB_UPD_ALL=1.
FULL = os.name != "nt" or bool(os.environ.get("PB_UPD_ALL"))


def updater_tests():
    global SH
    SH = Shell()
    mon = Monitor()
    rem = make_remote()

    def reset(**kw):
        mon.behave, mon.state, mon.connected, mon.last_rx_age, mon.busy_polls = {}, 0, True, 1, 0
        mon.up_age, mon.phases, mon.jump, mon.down_polls = 3600, [], 0, 0
        for k, v in kw.items():
            setattr(mon, k, v)

    # --- nothing to install
    reset()
    a = Pi(rem, mon, "a")
    j = a.run("check", ask="check")
    check("a pre-release (v1.1.0-rc1) is never installed: up to date", j.get("code") == "uptodate" and j.get("latest") == "v1.0.0"
          and "build" not in a.docker() and a.on("v1.0.0") and a.untouched(), (j, a.out))
    check("update.json is the new kind", j.get("v") == 2 and j.get("phase") == "idle" and j.get("current") == "v1.0.0")
    j = a.run("check", REPO_URL="https://127.0.0.1:1/none.git")
    check("no internet (the repository cannot be reached): says so, nothing changed", j.get("code") == "offline" and a.on("v1.0.0") and a.rc == 0, (j, a.out))
    j = a.run("check", REPO_URL="https://github.com/CHANGE-ME/pelletburner-monitor.git")
    check("REPO_URL left at CHANGE-ME: updates are off", j.get("code") == "no_repo")
    j = a.run("check", test=False)
    check("an address that is not https:// is refused outside this test", j.get("code") == "no_repo" and "https" in j.get("result", ""))
    a.run("bogus")
    check("an unknown mode does nothing", a.rc == 2)

    release(rem, "v1.1.0")
    release(rem, "v1.2.0-rc1")

    # --- the nightly run
    j = a.run("auto", AUTO_UPDATE="no")
    check("night, auto-update off: found but not installed", j.get("code") == "available" and j.get("latest") == "v1.1.0"
          and j.get("auto") is False and "build" not in a.docker() and a.on("v1.0.0"), (j, a.out))
    reset(state=5)
    j = a.run("auto")
    check("night, burner busy: not even built", j.get("code") == "later" and "build" not in a.docker() and a.on("v1.0.0"), (j, a.out))
    j = a.run("check", ask="check", MIN_FREE_KB=2000000)
    check("disk nearly full: stops before the download", j.get("code") == "disk_full" and "build" not in a.docker() and a.on("v1.0.0"))

    # --- the button: check + install, auto-update off, installed from the tarball (no .git)
    reset()
    b = Pi(rem, mon, "b", tarball=True, AUTO_UPDATE="no")
    j = b.run("check", ask="check")
    dk = b.docker()
    check("button, auto-update off: v1.1.0 installed (not v1.2.0-rc1)", j.get("code") == "updated" and j.get("current") == "v1.1.0"
          and j.get("from") == "v1.0.0" and b.on("v1.1.0") and b.rc == 0, (j, b.out))
    check("built before the swap", "compose build" in dk and dk.index("compose build") < dk.index("compose up"))
    check("settings, .env and the data folder are untouched; request and journal are gone", b.untouched()
          and "data" not in git(b.app, "ls-files") and ".env" not in git(b.app, "ls-files").split())
    check("the old version is kept for Go back", (j.get("previous") or {}).get("version") == "v1.0.0"
          and j["previous"]["until"] > time.time() + 13 * 86400 and b.tag("pelletpi-previous_latest") == "img-v1.0.0" and b.has("previous"))
    check("the new version's own updater is installed afterwards", os.path.exists(os.path.join(b.fake, "refreshed")))
    check("an accepted update prunes the old build cache", "builder prune -f --filter until=336h" in dk)
    check("the journal and the version file are synced to the card", dk.count("sync") >= 2)
    check("the log is kept in the updater's own folder; the page gets a copy",
          os.path.isfile(os.path.join(b.state, "update.log")) and "updated v1.0.0 -> v1.1.0" in open(os.path.join(b.data, "update.log")).read())
    j = b.run("check", ask="check")
    check("pressed again: up to date", j.get("code") == "uptodate" and (j.get("previous") or {}).get("version") == "v1.0.0")

    # --- Go back
    reset(state=5)
    j = b.run("check", ask="back")
    check("Go back while the burner works: refused, nothing changed", j.get("code") == "back_busy" and b.on("v1.1.0") and "compose up" not in b.docker(), (j, b.out))
    check("Go back: the page shows 'going back' before the burner is looked at (a second press gets 409)",
          bool(mon.phases) and mon.phases[0] == "back", mon.phases)
    reset()
    j = b.run("check", ask="back")
    check("Go back: the old image and files run again", j.get("code") == "went_back" and j.get("current") == "v1.0.0" and b.on("v1.0.0")
          and "compose build" not in b.docker() and b.untouched(), (j, b.out))
    check("after Go back nothing is kept to go back to", j.get("previous") is None and b.tag("pelletpi-previous_latest") is None)
    j = b.run("check", ask="back")
    check("Go back twice: there is no older version", j.get("code") == "no_previous" and b.on("v1.0.0"))
    j = b.run("auto", AUTO_UPDATE="yes")
    check("the version that was left is not installed again by itself", j.get("code") == "bad_skipped" and b.on("v1.0.0"))
    j = b.run("check", ask="check")
    check("... nor by a plain press", j.get("code") == "bad_skipped" and b.on("v1.0.0"))
    j = b.run("check", ask="retry")
    check("'Try again' installs it once more", j.get("code") == "updated" and b.on("v1.1.0") and not b.has("bad-v1.1.0"), (j, b.out))
    mon.behave = {"img-v1.0.0": "down"}
    j = b.run("back")
    check("Go back to a version that does not start: stays on the new one", j.get("code") == "back_failed" and b.on("v1.1.0") and b.rc == 1
          and b.untouched(), (j, b.out))

    # --- the request file can only choose between the three words
    reset()
    c = Pi(rem, mon, "c")
    j = c.run("check", ask="back; touch pwned\n")
    check("a request with more than the word counts as 'check' (never run)", j.get("code") == "updated" and c.on("v1.1.0")
          and not os.path.exists(os.path.join(c.app, "pwned")) and not os.path.exists(os.path.join(c.data, "pwned")), (j, c.out))

    # --- roll-back: each reason
    for how, why, words in (("down", "no_answer", "does not answer"), ("status", "status", "answers with a broken status"),
                            ("settings", "settings", "cannot read the settings"), ("nolink", "boiler", "the boiler does not connect again")):
        if not FULL and how in ("status", "settings"):
            continue
        reset(behave={"img-v1.1.0": how})
        p = Pi(rem, mon, "rb_" + how)
        j = p.run("check", ask="check")
        check(f"new version {words}: back to the old one", j.get("code") == "rolled_back" and j.get("why") == why and j.get("to") == "v1.1.0"
              and j.get("current") == "v1.0.0" and p.on("v1.0.0") and p.rc == 1 and p.untouched() and p.has("bad-v1.1.0")
              and j.get("previous") is None, (j, p.out))
    j = p.run("auto")
    check("a version that failed is not tried again at night", j.get("code") == "bad_skipped" and "build" not in p.docker())
    reset(connected=False, last_rx_age=None, up_age=30, jump=7200)
    q0 = Pi(rem, mon, "quiet0")
    j = q0.run("check", ask="check")
    check("no traffic, the monitor started a moment ago (its wall clock jumped 2 h at NTP sync): not 'quiet', not swapped",
          j.get("code") == "busy" and q0.on("v1.0.0") and q0.untouched(), (j, q0.out))
    if FULL:
        reset(behave={"img-v1.1.0": "nolink"}, connected=False, last_rx_age=None)
        q = Pi(rem, mon, "quiet")
        j = q.run("check", ask="check")
        check("no boiler on the line (switched off): it updates, and the link is not part of the test", j.get("code") == "updated" and q.on("v1.1.0"), (j, q.out))
        reset(connected=False, last_rx_age=30)
        q2 = Pi(rem, mon, "quiet2")
        j = q2.run("check", ask="check")
        check("the boiler fell silent a moment ago: not a safe time, waits and gives up", j.get("code") == "busy" and q2.on("v1.0.0") and q2.untouched(), (j, q2.out))

    # --- docker does not start the new version: never "updated"
    for flag in ("up_fail", "up_noop"):
        if not FULL and flag == "up_noop":
            continue
        reset()
        u = Pi(rem, mon, "u_" + flag)
        open(os.path.join(u.fake, flag), "w").close()
        j = u.run("check", ask="check")
        check(f"docker compose up {'fails' if flag == 'up_fail' else 'leaves the old image running'}: rolled back, not 'updated'",
              j.get("code") == "rolled_back" and j.get("why") == "swap" and u.on("v1.0.0") and u.rc == 1 and u.untouched()
              and u.has("bad-v1.1.0"), (j, u.out))

    # --- a release that changes only pi/ or docker-compose.yml: the same image id
    reset()
    si = Pi(rem, mon, "same")
    open(os.path.join(si.fake, "same_image"), "w").close()
    j = si.run("check", ask="check")
    check("a release with the same image (only the updater changed): installed, not rolled back",
          j.get("code") == "updated" and si.version() == "v1.1.0" and si.head() == "v1.1.0" and si.running() == "img-v1.0.0"
          and not si.has("bad-v1.1.0") and si.untouched(), (j, si.out))
    reset(behave={"img-v1.0.0": "settings"})
    sf = Pi(rem, mon, "same_fail")
    open(os.path.join(sf.fake, "same_image"), "w").close()
    j = sf.run("check", ask="check")
    check("a same-image release (only docker-compose.yml) fails its test: the container is started again from the old files",
          j.get("code") == "rolled_back" and sf.docker().count("compose up") == 2 and sf.on("v1.0.0") and sf.has("bad-v1.1.0")
          and sf.untouched(), (j, sf.out, sf.docker()))
    if FULL:
        reset()
        uk = Pi(rem, mon, "unknown")
        open(os.path.join(uk.fake, "unknown_after_up"), "w").close()
        j = uk.run("check", ask="check")
        check("docker cannot tell what runs after the swap: undone, but the release is not marked as failed",
              j.get("code") == "rolled_back" and not uk.has("bad-v1.1.0") and uk.head() == "v1.0.0" and uk.tag() == "img-v1.0.0"
              and uk.untouched(), (j, uk.out))
        reset()
        bt = Pi(rem, mon, "slowbuild")
        open(os.path.join(bt.fake, "build_slow"), "w").close()
        j = bt.run("check", ask="check", BUILD_TIMEOUT=1)
        check("a build that takes too long is stopped and counts as failed", j.get("code") == "build_failed"
              and bt.on("v1.0.0") and "longer than" in bt.out and bt.has("buildfail-v1.1.0"), (j, bt.out))

    # --- a dead monitor can still be updated (nothing talks to the boiler)
    reset(behave={"img-v1.0.0": "down"})
    pa = Pi(rem, mon, "pageonly")
    pa.rx(5)
    j = pa.run("auto", DEAD_WAIT=3600)
    check("the page does not answer, but the monitor heard the boiler a moment ago (data/last_rx): not swapped",
          j.get("code") == "later" and "build" not in pa.docker() and pa.on("v1.0.0") and pa.untouched(), (j, pa.out))
    if FULL:
        os.remove(os.path.join(pa.data, "last_rx"))
        pa.rx(5, "hist-2026-01-01.jsonl")
        j = pa.run("auto", DEAD_WAIT=3600)
        check("... nor when only its history file is fresh (an older monitor without last_rx)", j.get("code") == "later"
              and "build" not in pa.docker(), (j, pa.out))
        os.remove(os.path.join(pa.data, "hist-2026-01-01.jsonl"))
        j = pa.run("auto", DEAD_WAIT=2)
        check("... nor when nothing shows when it last heard the boiler", j.get("code") == "later" and "build" not in pa.docker(), (j, pa.out))
        reset()
        po = Pi(rem, mon, "port", URL="")
        with open(os.path.join(po.fake, "env"), "w", newline="\n") as fh:
            fh.write(f"PATH=/usr/bin\nBURNER_HTTP_PORT={mon.port}\nBURNER_KEEP_DAYS=30\n")
        j = po.run("check", ask="check")
        check("the page's port is read from the container (BURNER_HTTP_PORT), not taken as 8787",
              j.get("code") == "updated" and po.on("v1.1.0"), (j, po.out))
    reset(behave={"img-v1.0.0": "down"})
    dm = Pi(rem, mon, "dead")
    dm.rx(3600)
    j = dm.run("auto", DEAD_WAIT=0)
    check("the page has not answered and nothing came from the boiler for the wait time: updated at night",
          j.get("code") == "updated" and dm.on("v1.1.0"), (j, dm.out))
    if FULL:
        reset(behave={"img-v1.0.0": "down"})
        dm2 = Pi(rem, mon, "dead2")
        open(os.path.join(dm2.fake, "stopped"), "w").close()
        j = dm2.run("auto", DEAD_WAIT=999)
        check("the monitor's container is stopped: updated at once", j.get("code") == "updated" and dm2.on("v1.1.0"), (j, dm2.out))

    # --- the nightly run leaves a page request alone (the path unit handles it)
    reset(state=5)
    with open(os.path.join(dm.data, "update-request"), "w") as fh:
        fh.write("back\n")
    dm.run("auto")
    check("a timer run does not delete the page's request", os.path.exists(os.path.join(dm.data, "update-request")))
    os.remove(os.path.join(dm.data, "update-request"))
    with open(os.path.join(dm.data, "update-request"), "w") as fh:
        fh.write("check\n")
    dm.run("check", APP=fw(os.path.join(dm.root, "nowhere")), env=f"PELLETPI_REQ='{fw(os.path.join(dm.data, 'update-request'))}'")
    check("APP not set: the request is removed all the same (the path unit does not loop)", dm.rc == 1
          and not os.path.exists(os.path.join(dm.data, "update-request")), dm.out)

    # --- local changes to the program's files
    reset()
    lc = Pi(rem, mon, "local")
    with open(os.path.join(lc.app, "docker-compose.yml"), "a", newline="\n") as fh:
        fh.write("# my change\n")
    j = lc.run("check", ask="check")
    check("local changes in the program folder: not installed, the change is kept", j.get("code") == "local_changes" and lc.rc == 1
          and "build" not in lc.docker() and "# my change" in open(os.path.join(lc.app, "docker-compose.yml")).read()
          and lc.head() == "v1.0.0", (j, lc.out))

    # --- failures on the way
    reset()
    f = Pi(rem, mon, "f")
    open(os.path.join(f.fake, "build_fail"), "w").close()
    j = f.run("check", ask="check")
    check("the build fails (internet lost, disk full): the old version stays", j.get("code") == "build_failed" and f.on("v1.0.0")
          and "compose up" not in f.docker() and f.untouched() and not f.has("bad-v1.1.0"), (j, f.out))
    os.remove(os.path.join(f.fake, "build_fail"))
    reset(state=5)
    j = f.run("check", ask="check")
    check("burner busy all the time: built, never swapped, the old version stays", j.get("code") == "busy" and f.on("v1.0.0")
          and "compose build" in f.docker() and "compose up" not in f.docker() and f.untouched(), (j, f.out))
    if FULL:
        reset(busy_polls=3)
        j = f.run("check", ask="check", WAIT_PAGE=60)
        check("burner busy, then off: installed when it is off", j.get("code") == "updated" and f.on("v1.1.0")
              and "waits until the burner is off" in open(os.path.join(f.data, "update.log")).read(), (j, f.out))

    if FULL:
        reset()
        bf = Pi(rem, mon, "buildfail")
        open(os.path.join(bf.fake, "build_fail"), "w").close()
        codes = [bf.run("check", ask="check").get("code") for _ in range(3)]
        j = bf.run("check", ask="check")
        check("a build that failed 3 times: the release is skipped, nothing built", codes == ["build_failed"] * 3
              and j.get("code") == "bad_skipped" and "build" not in bf.docker() and bf.on("v1.0.0"), (codes, j, bf.out))
        os.remove(os.path.join(bf.fake, "build_fail"))
        j = bf.run("check", ask="retry")
        check("'Try again' clears the count", j.get("code") == "updated" and bf.on("v1.1.0") and not bf.has("buildfail-v1.1.0"), (j, bf.out))

    # --- a run cut short by a power cut
    def cut(p, running, kind="install", new="img-v1.1.0", swapped=""):
        prev = git(p.app, "rev-parse", "HEAD")
        git(p.app, "fetch", "-q", fw(rem), "+refs/tags/v1.1.0:refs/tags/v1.1.0")
        git(p.app, "checkout", "-q", "-f", "--detach", "v1.1.0")
        with open(os.path.join(p.fake, "tags", "app-burner"), "w", newline="\n") as fh:
            fh.write(new + "\n")
        p.img(running)
        with open(os.path.join(p.state, "in-progress"), "w", newline="\n") as fh:
            fh.write(f"KIND={kind}\nFROM=v1.0.0\nTO=v1.1.0\nPREV={prev}\nOLD_IMAGE=img-v1.0.0\nIMAGE_NAME=app-burner\n"
                     + (f"SWAPPED={swapped}\n" if swapped else ""))

    reset()
    g = Pi(rem, mon, "g")
    cut(g, "img-v1.0.0")
    j = g.run("auto", REPO_URL="https://127.0.0.1:1/none.git")
    check("power cut during the download or build: the next run puts the old files and image name back", g.on("v1.0.0") and g.untouched()
          and j.get("code") == "offline" and "cut short" in g.out, (j, g.out))
    cut(g, "img-v1.1.0")
    mon.behave = {"img-v1.1.0": "down"}
    g.rx(3600)
    j = g.run("auto", REPO_URL="https://127.0.0.1:1/none.git")
    check("power cut during the test, new version does not answer: back to the old one", g.on("v1.0.0") and g.untouched(), (j, g.out))
    cut(g, "img-v1.1.0")
    reset()
    j = g.run("auto", REPO_URL="https://127.0.0.1:1/none.git")
    check("power cut during the test, new version runs fine: it is kept", g.on("v1.1.0") and g.untouched()
          and (j.get("previous") or {}).get("version") == "v1.0.0", (j, g.out))
    check("... and its own updater is installed", os.path.exists(os.path.join(g.fake, "refreshed")), g.out)
    reset()
    h = Pi(rem, mon, "h")
    cut(h, "img-v1.1.0", kind="back")
    reset(state=5)
    j = h.run("auto", REPO_URL="https://127.0.0.1:1/none.git")
    check("power cut, the other version answers while the burner works: nothing changed yet (one whole version)",
          j.get("code") == "tidy_wait" and h.head() == "v1.1.0" and h.tag() == "img-v1.1.0" and h.running() == "img-v1.1.0"
          and "compose up" not in h.docker() and h.has("in-progress"), (j, h.out))
    check("... and the page was told it waits", "waiting" in mon.phases, mon.phases)
    reset()
    j = h.run("auto", REPO_URL="https://127.0.0.1:1/none.git")
    check("... and swapped back once the burner is off", h.on("v1.0.0") and h.untouched(), (j, h.out))
    reset()
    mx = Pi(rem, mon, "mixed")
    cut(mx, "img-v1.1.0")
    reset(down_polls=1, state=5)
    j = mx.run("auto", REPO_URL="https://127.0.0.1:1/none.git")
    check("power cut during the test, the new version misses one look, burner busy: waits, nothing mixed",
          j.get("code") == "tidy_wait" and (mx.head(), mx.tag(), mx.running()) == ("v1.1.0", "img-v1.1.0", "img-v1.1.0"), (j, mx.out))
    reset()
    j = mx.run("auto", REPO_URL="https://127.0.0.1:1/none.git")
    check("... a later run finds it answering and keeps it whole (files, image name, version all new)",
          mx.on("v1.1.0") and mx.untouched(), (j, mx.out))
    reset()
    td = Pi(rem, mon, "tidy_docker")
    cut(td, "img-v1.1.0", kind="back")
    open(os.path.join(td.fake, "docker_unknown"), "w").close()
    j = td.run("auto", REPO_URL="https://127.0.0.1:1/none.git")
    check("power cut, then docker cannot tell what runs: the tidy-up waits, nothing changed",
          j.get("code") == "tidy_wait" and (td.head(), td.tag(), td.running()) == ("v1.1.0", "img-v1.1.0", "img-v1.1.0")
          and "compose up" not in td.docker() and td.has("in-progress"), (j, td.out))
    os.remove(os.path.join(td.fake, "docker_unknown"))
    open(os.path.join(td.fake, "up_fail"), "w").close()
    j = td.run("auto", REPO_URL="https://127.0.0.1:1/none.git")
    check("... docker cannot start the old version: the journal is kept for the next run",
          j.get("code") == "tidy_wait" and td.has("in-progress") and td.rc == 1, (j, td.out))
    os.remove(os.path.join(td.fake, "up_fail"))
    j = td.run("auto", REPO_URL="https://127.0.0.1:1/none.git")
    check("... and the next run finishes it", td.on("v1.0.0") and td.untouched(), (j, td.out))
    reset()
    ts = Pi(rem, mon, "tidy_same")
    cut(ts, "img-v1.0.0", new="img-v1.0.0", swapped="1")
    j = ts.run("auto", REPO_URL="https://127.0.0.1:1/none.git")
    check("power cut during the test of a same-image release that answers: kept",
          ts.version() == "v1.1.0" and ts.head() == "v1.1.0" and ts.untouched() and "compose up" not in ts.docker(), (j, ts.out))
    if FULL:
        reset(behave={"img-v1.0.0": "down"})
        tf = Pi(rem, mon, "tidy_same_down")
        cut(tf, "img-v1.0.0", new="img-v1.0.0", swapped="1")
        tf.rx(3600)
        j = tf.run("auto", REPO_URL="https://127.0.0.1:1/none.git")
        check("... that does not answer: the old files come back and the container is started again from them",
              tf.on("v1.0.0") and tf.untouched() and "compose up" in tf.docker(), (j, tf.out))
        reset()
    open(os.path.join(h.state, "in-progress"), "w").close()
    j = h.run("auto", REPO_URL="https://127.0.0.1:1/none.git")
    check("an empty journal: the running program's image is kept, and it is logged", h.on("v1.0.0") and h.untouched()
          and "empty or broken" in h.out, (j, h.out))

    # --- never to an older version; a pre-release moves on to its release
    for cur, want in (("v1.2.0", "uptodate"), ("v1.1.0-rc1", "updated")):
        reset()
        if cur == "v1.2.0":                        # a release this repository does not have yet: tagged here
            n = Pi(rem, mon, "n_" + cur.replace(".", "_"))
            git(n.app, "tag", cur)
            with open(os.path.join(n.state, "version"), "w", newline="\n") as fh:
                fh.write(cur + "\n")
        else:
            n = Pi(rem, mon, "n_" + cur.replace(".", "_"), version=cur)
        j = n.run("check", ask="check")
        check(f"running {cur}, newest release v1.1.0: {want}", j.get("code") == want and ("build" in n.docker()) == (want == "updated"), (j, n.out))

    # --- commits after a tag (v1.0.0-57-gabc): a branch copy, never replaced at night, though v1.0.0 < v1.1.0
    reset()
    dsc = Pi(rem, mon, "n_desc")
    with open(os.path.join(dsc.state, "version"), "w", newline="\n") as fh:
        fh.write("v1.0.0-57-g0123abc\n")
    j = dsc.run("auto")
    check("running v1.0.0-57-g0123abc: not installed at night (it is a branch copy)", j.get("code") == "unknown_base"
          and "build" not in dsc.docker() and dsc.head() == "v1.0.0", (j, dsc.out))
    reset()
    sv = Pi(rem, mon, "n_stale")
    with open(os.path.join(sv.app, "mine.txt"), "w", newline="\n") as fh:
        fh.write("a branch commit\n")
    git(sv.app, "add", "mine.txt")
    git(sv.app, "commit", "-q", "-m", "mine")
    j = sv.run("auto")
    check("the version file still says v1.0.0, but the files are a later commit: not replaced at night",
          j.get("code") == "unknown_base" and "build" not in sv.docker() and os.path.exists(os.path.join(sv.app, "mine.txt")), (j, sv.out))
    if FULL:
        reset()
        br = Pi(rem, mon, "n_branch", version="v1.1.0")
        with open(os.path.join(br.app, "mine.txt"), "w", newline="\n") as fh:
            fh.write("a branch commit\n")
        git(br.app, "add", "mine.txt")
        git(br.app, "commit", "-q", "-m", "mine")
        with open(os.path.join(br.state, "version"), "w", newline="\n") as fh:
            fh.write("v1.1.0-1-g0123abc\n")
        j = br.run("check", ask="check")
        check("running v1.1.0-1-g... (a commit on top of v1.1.0): the button installs nothing, the commit stays",
              j.get("code") == "uptodate" and "build" not in br.docker() and os.path.exists(os.path.join(br.app, "mine.txt")), (j, br.out))
        with open(os.path.join(br.state, "version"), "w", newline="\n") as fh:
            fh.write("v1.1.0-dirty\n")
        j = br.run("auto")
        check("running v1.1.0-dirty: not touched at night", j.get("code") == "unknown_base" and "build" not in br.docker(), (j, br.out))

    # --- not a release (a copy cloned from a branch): never at night; on a press only when it is older
    reset()
    n = Pi(rem, mon, "n_hash")
    with open(os.path.join(n.state, "version"), "w", newline="\n") as fh:
        fh.write("0123abc\n")
    git(n.app, "remote", "add", "mine", fw(rem))
    j = n.run("auto")
    check("running 0123abc (not a release): not installed at night; the button offers it", j.get("code") == "unknown_base"
          and "build" not in n.docker() and X.update_info(n.data)["view"]["btn"] == "install", (j, n.out))
    j = n.run("check", ask="check")
    check("running 0123abc, its commit is in v1.1.0's history: the button installs v1.1.0", j.get("code") == "updated"
          and n.on("v1.1.0"), (j, n.out))
    check("... a full clone stays full, and keeps its remotes and their branches",
          git(n.app, "rev-parse", "--is-shallow-repository") == "false" and "mine" in git(n.app, "remote").split()
          and git(n.app, "for-each-ref", "refs/remotes/origin") != "", git(n.app, "remote"))
    reset()
    m = Pi(rem, mon, "n_main", version="main")
    with open(os.path.join(m.state, "version"), "w", newline="\n") as fh:
        fh.write("abc1234\n")
    j = m.run("check", ask="check")
    check("running a branch copy newer than v1.1.0: the button installs nothing (no step back)", j.get("code") == "uptodate"
          and "build" not in m.docker(), (j, m.out))
    check("... its full clone stays full (no --depth) and the files are handed back", git(m.app, "rev-parse", "--is-shallow-repository") == "false"
          and "chown" in m.docker(), m.docker())
    if FULL:
        reset()
        sh = Pi(rem, mon, "n_shallow", shallow=True)
        with open(os.path.join(sh.state, "version"), "w", newline="\n") as fh:
            fh.write("abc1234\n")
        j = sh.run("check", ask="check")
        check("a --depth 1 clone of the branch (firstboot): not older than v1.1.0, nothing installed", j.get("code") == "uptodate"
              and "build" not in sh.docker(), (j, sh.out))

    # --- the kept version runs out
    with open(os.path.join(c.state, "previous"), "w", newline="\n") as fh:
        fh.write(f"VERSION=v1.0.0\nAT={int(time.time()) - 15 * 86400}\n")
    j = c.run("auto")
    check("after 14 days the kept old version is removed", j.get("previous") is None and not c.has("previous")
          and c.tag("pelletpi-previous_latest") is None and c.on("v1.1.0"), (j, c.out))
    mon.srv.shutdown()
    SH.close()


if BASH and GIT:
    try:
        updater_tests()
        if not FULL:
            print("note: Windows: the main updater cases ran (all of them: PB_UPD_ALL=1; GitHub runs all on Linux)")
    except Exception as e:            # a broken stand-in must show as a failure, not as a pass
        check(f"updater run: {e!r}", False)
else:
    print("note: no bash or git here: the updater script was not run (the other parts were)")

shutil.rmtree(TMP, ignore_errors=True)
print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
