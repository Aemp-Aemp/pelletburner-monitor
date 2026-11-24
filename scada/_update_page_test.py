"""The update card in a real browser (Playwright; skipped without it): /home Settings → About this monitor and
/classic, English and Danish, from made-up data/update.json files. The monitor is the replay on synthetic data;
/api/update is answered here by the real burner_ext.update_info from a temp folder. Nothing is installed.

    py scada\\_update_page_test.py
    set PB_UPD_SHOTS=<folder>   also saves pictures of the card in each state there
"""
import json, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: Playwright is not installed (pip install playwright; playwright install chromium)")
    sys.exit(0)
import _synth
import burner_ext as X

FAILED = 0
PORT = 8843
SHOTS = os.environ.get("PB_UPD_SHOTS")
TMP = tempfile.mkdtemp(prefix="pb_updpg_")
POSTS = []


def check(name, cond, extra=""):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra and not cond else ""), flush=True)
    FAILED += 0 if cond else 1


def put(**kw):
    """The updater's file, as pi/pelletpi-update.sh writes it."""
    d = {"v": 2, "current": "v1.0.0", "latest": "v1.0.0", "available": False, "auto": True, "phase": "idle",
         "code": "uptodate", "why": None, "from": None, "to": None, "result": "", "checked": int(time.time()),
         "previous": None}
    d.update(kw)
    with open(os.path.join(TMP, "update.json.tmp"), "w") as f:
        json.dump(d, f)
    os.replace(os.path.join(TMP, "update.json.tmp"), os.path.join(TMP, "update.json"))
    os.utime(os.path.join(TMP, "update.json"), (time.time(), time.time()))    # file times are real; --clock moves ours


KEPT = {"version": "v1.0.0", "until": time.time() + 9 * 86400}
STATES = {
    "up-to-date": dict(),
    "no-internet": dict(code="offline"),
    "new-version-downloading": dict(phase="download", code=None, latest="v1.1.0", available=True, to="v1.1.0", **{"from": "v1.0.0"}),
    "installing": dict(phase="install", code=None, latest="v1.1.0", available=True, to="v1.1.0", **{"from": "v1.0.0"}),
    "now-on-new-version": dict(current="v1.1.0", latest="v1.1.0", code="updated", to="v1.1.0", previous=KEPT, **{"from": "v1.0.0"}),
    "went-back": dict(current="v1.1.0", latest="v1.2.0", available=True, code="rolled_back", why="boiler", to="v1.2.0",
                      previous=KEPT, **{"from": "v1.1.0"}),
}
WANT = {
    "en": {"up-to-date": "You have the newest version.", "no-internet": "Share internet from your phone",
           "new-version-downloading": "New version v1.1.0 found", "installing": "Installing v1.1.0 and testing it",
           "now-on-new-version": "Now on v1.1.0.", "went-back": "v1.2.0 did not work here – went back to v1.1.0. The boiler did not connect again."},
    "da": {"up-to-date": "Du har den nyeste version.", "no-internet": "Del internet fra din telefon",
           "new-version-downloading": "Ny version v1.1.0 fundet", "installing": "Installerer v1.1.0 og afprøver den",
           "now-on-new-version": "Nu på v1.1.0.", "went-back": "v1.2.0 virkede ikke her – gik tilbage til v1.1.0. Kedlen fik ikke forbindelse igen."},
}
DOWN = {"on": False}


def serve(route):
    q = route.request
    path = q.url.split("?")[0]
    if path.endswith("/api/update") and q.method == "GET":
        if DOWN["on"]:                               # the monitor is restarting
            return route.abort()
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(X.update_info(TMP)))
    if "/api/update/" in path and q.method == "POST":
        what = path.rsplit("/", 1)[1]
        POSTS.append((what, q.post_data, q.headers.get("x-burner")))
        ok, msg = X.update_request(TMP, what)
        os.utime(os.path.join(TMP, "update-request"), (time.time(), time.time()))
        return route.fulfill(status=200 if ok else 500, content_type="application/json", body=json.dumps({"ok": ok, "msg": msg}))
    if q.method != "GET":
        POSTS.append(("OTHER " + q.method + " " + path, None, None))
        return route.abort()
    route.continue_()


def card(pg):
    return pg.evaluate("()=>{const c=document.getElementById('updCard'); return c&&!c.hidden&&c.offsetParent!==null?c.innerText:''}")


def shot(pg, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        pg.evaluate("()=>window.scrollTo(0,0)")
        pg.screenshot(path=os.path.join(SHOTS, name + ".png"))


with _synth.Replay(PORT) as rp, sync_playwright() as pw:
    br = pw.chromium.launch()
    for lang in ("en", "da"):
        ctx = br.new_context(locale="da-DK" if lang == "da" else "en-GB", viewport={"width": 1100, "height": 820})
        ctx.add_init_script(f"try{{localStorage.setItem('burner_lang','{lang}');}}catch(e){{}}")
        ctx.route("**/*", serve)
        pg, errs = ctx.new_page(), []
        pg.on("pageerror", lambda x, e=errs: e.append("pageerror: " + str(x)))
        put()
        pg.goto(f"{rp.base}/home?replaybanner=0#settings/about", wait_until="load")
        pg.wait_for_selector("#updCard button[data-upd]", timeout=20000)
        for name, kw in STATES.items():
            put(**kw)
            pg.evaluate("()=>updLoad()")
            pg.wait_for_function("k=>{const e=document.getElementById('updState'); return e&&e.dataset.key===k}",
                                 arg=X.update_info(TMP)["view"]["key"], timeout=8000)
            txt = card(pg)
            check(f"[{lang}] home, {name}: the card says it", WANT[lang][name] in txt, txt)
            btns = pg.evaluate("()=>[...document.querySelectorAll('#updCard button[data-upd]')].map(b=>b.dataset.upd+':'+b.textContent)")
            busy = name in ("new-version-downloading", "installing")
            sw = [x for x in btns if x.startswith("auto-")]
            check(f"[{lang}] home, {name}: the auto-update switch shows when idle {sw}", (sw == []) if busy else len(sw) == 1)
            btns = [x for x in btns if not x.startswith("auto-")]
            check(f"[{lang}] home, {name}: buttons fit the state {btns}", (btns == []) if busy else
                  (btns[0].startswith("check:") and (len(btns) == 2) == (name in ("now-on-new-version", "went-back"))))
            if name in ("now-on-new-version", "went-back"):
                check(f"[{lang}] home, {name}: Go back names the kept version", btns[1] in ("back:Go back to v1.0.0", "back:Gå tilbage til v1.0.0"))
            if lang == "en":
                shot(pg, "update-card-" + name)
            elif name == "went-back":
                shot(pg, "update-card-went-back-danish")
        check(f"[{lang}] home: no script errors", not errs, errs[:3])
        foot = pg.evaluate("()=>document.getElementById('updLine').innerText")
        check(f"[{lang}] home: the line at the bottom has the version and the result", "v1.1.0" in foot and "v1.2.0" in foot, foot)

        if lang == "en":
            # ---- one press: only the word goes out; the steps then show by themselves, across a restart
            put()
            pg.evaluate("()=>updLoad()")
            pg.wait_for_function("()=>document.getElementById('updState').dataset.key==='uptodate'")
            POSTS.clear()
            pg.click("#updCard button[data-upd=check]")
            pg.wait_for_function("()=>document.getElementById('updState').dataset.key==='asked'", timeout=8000)
            check("press: one POST /api/update/check with an empty body and the X-Burner header",
                  POSTS == [("check", "{}", "1")], POSTS)
            check("press: 'Checking…' and no button while it works", "Checking…" in card(pg)
                  and not pg.query_selector("#updCard button[data-upd]"))
            os.remove(os.path.join(TMP, "update-request"))             # the updater took it
            put(**STATES["new-version-downloading"])
            pg.wait_for_function("()=>document.getElementById('updState').dataset.key==='ph_download'", timeout=10000)
            check("progress shows without a reload: downloading", "New version v1.1.0 found" in card(pg))
            put(phase="waiting", code=None, latest="v1.1.0", to="v1.1.0")
            pg.wait_for_function("()=>document.getElementById('updState').dataset.key==='ph_waiting'", timeout=10000)
            check("waiting for the boiler: says so, and that internet is no longer needed",
                  "as soon as the boiler is off or waiting" in card(pg) and "Internet is no longer needed" in card(pg))
            put(**STATES["installing"])
            pg.wait_for_function("()=>document.getElementById('updState').dataset.key==='ph_install'", timeout=10000)
            DOWN["on"] = True                                           # the monitor restarts: reads fail
            pg.wait_for_timeout(7000)
            check("monitor away in the middle: the card keeps the last step", "Installing v1.1.0" in card(pg))
            put(**STATES["now-on-new-version"])
            DOWN["on"] = False
            pg.wait_for_function("()=>document.getElementById('updState').dataset.key==='updated'", timeout=10000)
            check("the result shows by itself: Now on v1.1.0", "Now on v1.1.0." in card(pg))
            with pg.expect_navigation(timeout=15000):                   # the page loads itself again (new version)
                pass
            pg.wait_for_selector("#updCard button[data-upd=back]", timeout=20000)
            check("after the page loaded itself again it still says Now on v1.1.0", "Now on v1.1.0." in card(pg))
            # ---- Go back asks first
            POSTS.clear()
            pg.click("#updCard button[data-upd=back]")
            pg.wait_for_selector(".sheet .mtext")
            check("Go back asks first", "Go back to v1.0.0? Settings and history are kept." in pg.inner_text(".sheet .mtext") and not POSTS)
            pg.click('.sheet [data-a="0"]')
            pg.wait_for_timeout(300)
            check("Cancel sends nothing", not POSTS)
            pg.click("#updCard button[data-upd=back]")
            pg.click('.sheet [data-a="1"]')
            pg.wait_for_function("()=>document.getElementById('updState').dataset.key==='asked'", timeout=8000)
            check("OK sends only 'back'", POSTS == [("back", "{}", "1")], POSTS)
            os.remove(os.path.join(TMP, "update-request"))
            # ---- phone width
            ph = br.new_context(locale="en-GB", viewport={"width": 375, "height": 812})
            ph.add_init_script("try{localStorage.setItem('burner_lang','en');}catch(e){}")
            ph.route("**/*", serve)
            p2 = ph.new_page()
            put(**STATES["went-back"])
            p2.goto(f"{rp.base}/home?replaybanner=0#settings/about", wait_until="load")
            p2.wait_for_selector("#updCard button[data-upd=back]", timeout=20000)
            wide = p2.evaluate("()=>document.documentElement.scrollWidth>window.innerWidth+1")
            inside = p2.evaluate("()=>[...document.querySelectorAll('#updCard button')].every(b=>{const r=b.getBoundingClientRect(); return r.left>=0&&r.right<=window.innerWidth})")
            check("phone width (375): no sideways scroll, the buttons fit", not wide and inside)
            p2.evaluate("()=>document.getElementById('updCard').scrollIntoView({block:'start'})")
            if SHOTS:
                p2.screenshot(path=os.path.join(SHOTS, "update-card-went-back-phone-375.png"))
            ph.close()

        # ---- /classic
        put(**STATES["went-back"])
        pc = ctx.new_page()
        pc.goto(f"{rp.base}/classic?replaybanner=0", wait_until="load")
        pc.wait_for_selector("#updCard button[data-upd=back]", timeout=20000)
        check(f"[{lang}] classic: the same card", WANT[lang]["went-back"] in card(pc), card(pc))
        put()
        os.remove(os.path.join(TMP, "update.json"))
        pc.evaluate("()=>updLoad()")
        pc.wait_for_function("()=>document.getElementById('updCard').hidden", timeout=8000)
        pg.evaluate("()=>updLoad()")
        pg.wait_for_function("()=>document.getElementById('updCard').hidden", timeout=8000)
        foot = pg.evaluate("()=>document.getElementById('updLine').innerText")
        check(f"[{lang}] no updater on this machine: no card, only the version at the bottom", foot.startswith("Version ") and "·" not in foot, foot)
        ctx.close()
    br.close()

shutil.rmtree(TMP, ignore_errors=True)
print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
