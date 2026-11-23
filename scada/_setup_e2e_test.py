"""First start, in a real browser: runs the real program (burner_responder.py) with an EMPTY data folder, once with
no boiler at all and once with a simulated boiler, opens the home page with Playwright (headless Chromium) and walks
the first-start guide in English and Danish, on a phone (390x844) and a desktop. Checks: no console errors, no
sideways scrolling, the answers end up in the monitor's settings file, the second open goes straight to the normal
page, "Run the setup again" and "Skip the setup" work, and the boiler is never written to.

Skips cleanly (exit 0) when Playwright is not installed. Screenshots of every step go to $SETUP_SHOTS (if set).
Nothing here talks to a real boiler: the simulated one is a fake serial port inside the test process, the program
runs without --live (it never transmits), and the weather service is replaced by a fake offline answer."""
import os, sys, json, time, socket, tempfile, shutil, subprocess, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")


# ------------------------------------------------------------------ server mode (child process)
def serve(mode, port, data_dir, log):
    sys.path.insert(0, ROOT)
    import serial
    import burner_features as F
    from burner_sniff import build_frame

    def fake_get(url, params, timeout=15):                   # offline: never ask the internet in a test
        if "geocoding" in url:
            return {"results": []}
        now = int(time.time())
        return {"current": {"temperature_2m": 3.0},
                "hourly": {"time": [now - 3600 * k for k in range(30, -1, -1)], "temperature_2m": [2.0 + (k % 5) * 0.5 for k in range(31)]}}
    F.Weather._get = staticmethod(fake_get)

    class FakeBoiler:
        """A pretend controller: sends a status message every second and its settings once. Writes nothing back
        (the program runs without --live, so it never transmits anyway)."""
        def __init__(self, *a, **k):
            self.n, self.next_t = 0, 0.0

        def _frame(self, obj):
            return build_frame(0x01, 3, 0x20, 4, json.dumps(obj, separators=(",", ":")).encode() + b"\x04")

        def read(self, n):
            time.sleep(0.02)
            if time.time() < self.next_t:
                return b""
            self.next_t = time.time() + 1.0
            self.n += 1
            if self.n % 30 == 1:
                cfg = {"000": 60, "001": 3300, "009": 1500, "014": 28, "015": 80, "016": 80, "017": 600, "018": 20, "019": 3,
                       "020": 35, "031": 100, "033": 1, "034": 25, "035": 5.0, "036": 0.005, "037": 0.0}
                return self._frame({"cfg_data_1": cfg})
            st = {"000": 0, "001": 41.0, "002": 0, "003": 0.0, "004": 0, "005": 0, "006": 0, "007": 0, "008": 0,
                  "009": 0, "010": 0, "011": 0, "012": 0, "013": 0, "014": 0, "015": 0, "016": 0, "017": 0, "018": 0}
            return self._frame({"status_info": st})

        def write(self, b):
            raise AssertionError("the test boiler must never be written to")

        def flush(self): pass
        def close(self): pass

    if mode == "sim":
        serial.Serial = FakeBoiler
    else:
        def no_port(*a, **k):
            raise serial.SerialException("no boiler in this test")
        serial.Serial = no_port
    sys.argv = ["burner_responder.py", "--fw-mode", "--serve", str(port), "--data-dir", data_dir, "--log", log,
                "--port", "TEST-NO-PORT", "--no-log-frames", "--remote-cmd", os.path.join(os.path.dirname(log), "rc.txt")]
    sys.path.insert(0, ROOT)
    import burner_responder as R
    R.main()


if len(sys.argv) > 1 and sys.argv[1] == "--serve":
    serve(sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5])
    sys.exit(0)

# ------------------------------------------------------------------ the test
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: Playwright is not installed (pip install playwright; playwright install chromium)")
    sys.exit(0)

FAILED = 0
SHOTS = os.environ.get("SETUP_SHOTS", "")
TMP = tempfile.mkdtemp(prefix="setup_e2e_")


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    FAILED += 0 if cond else 1


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class Server:
    def __init__(self, mode, name):
        self.dir = os.path.join(TMP, name); os.makedirs(os.path.join(self.dir, "data"))
        self.data, self.port = os.path.join(self.dir, "data"), free_port()
        check(f"[{name}] data folder starts empty", os.listdir(self.data) == [])
        self.p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--serve", mode, str(self.port), self.data,
                                   os.path.join(self.dir, "resp.log")], stdout=subprocess.DEVNULL, stderr=open(os.path.join(self.dir, "err.txt"), "w"))
        self.url = f"http://127.0.0.1:{self.port}/"
        t0 = time.time()
        while time.time() - t0 < 20:
            try:
                urllib.request.urlopen(self.url + "api/settings", timeout=2).read(); break
            except Exception:
                time.sleep(0.2)
        if mode == "sim":                                    # wait for the pretend boiler's first data
            while time.time() - t0 < 30 and not self.status().get("connected"):
                time.sleep(0.3)

    def status(self):
        try:
            return json.loads(urllib.request.urlopen(self.url + "status.json", timeout=3).read())
        except Exception:
            return {}

    def settings(self):
        try:
            with open(os.path.join(self.data, "settings.json"), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def stop(self):
        self.p.terminate()
        try: self.p.wait(10)
        except Exception: self.p.kill()


VIEW = {"phone": {"width": 390, "height": 844}, "desktop": {"width": 1280, "height": 900}}


def new_page(browser, view):
    ctx = browser.new_context(viewport=VIEW[view], device_scale_factor=2 if view == "phone" else 1,
                              is_mobile=view == "phone", has_touch=view == "phone", accept_downloads=True)
    page = ctx.new_page()
    page.errors = []
    page.on("console", lambda m: m.type == "error" and page.errors.append(m.text))
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.on("dialog", lambda d: (page.errors.append("browser popup: " + d.message), d.dismiss()))
    return ctx, page


def no_sideways(page):
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def step(page):
    return page.get_attribute("#setup .su-card", "data-step")


def shot(page, tag, n, name, checks):
    page.wait_for_timeout(250)
    checks.append(no_sideways(page))
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, f"setup_{tag}_{n}_{name}.png"), full_page=True)


def next_(page, expect):
    page.click('#setup [data-su="next"]')
    page.wait_for_function(f'document.querySelector("#setup .su-card") && document.querySelector("#setup .su-card").dataset.step==="{expect}"', timeout=8000)


def walk(browser, lang, view, mode, full):
    tag = f"{lang}_{view}"
    srv = Server(mode, tag)
    ctx, page = new_page(browser, view)
    side = []
    try:
        page.goto(srv.url)
        page.wait_for_selector("#setup .su-card", timeout=10000)
        check(f"[{tag}] first open shows the guide, not the normal page",
              step(page) == "lang" and not page.is_visible(".wrap"))
        # 1 language
        page.click(f'#setup [data-sulang="{lang}"]')
        check(f"[{tag}] language switches the guide", page.evaluate("LANG") == lang)
        shot(page, tag, 1, "language", side)
        next_(page, "hi")
        # 2 welcome + boiler connected?
        want = "yes" if mode == "sim" else "no"
        page.wait_for_selector(f"#suConn.{want}", timeout=12000)
        check(f"[{tag}] boiler connected shows '{want}' live", True)
        if mode != "sim":
            check(f"[{tag}] no data: cable and module hint", "IO BOKS" in page.inner_text("#suConn"))
        shot(page, tag, 2, "welcome", side)
        next_(page, "boil")
        # 3 your boiler
        if mode == "sim":
            page.wait_for_selector('#setup [data-sukw="15"].on', timeout=8000)
            check(f"[{tag}] size pre-filled from the boiler type (15 kW)", True)
        else:
            check(f"[{tag}] no size guessed without a boiler", page.query_selector("#setup [data-sukw].on") is None)
            page.click('#setup [data-sukw="25"]')
        page.fill('#setup [data-ci="sw"]', "1.40.1")
        page.fill('#setup [data-ci="min_pct"]', "500")
        page.fill('#setup [data-ci="fan100_rpm"]', "1800")
        page.click('#setup [data-su="next"]'); page.wait_for_timeout(400)
        check(f"[{tag}] a wrong number stops on the step with a note",
              step(page) == "boil" and page.inner_text("#suBad").strip() != "")
        page.fill('#setup [data-ci="min_pct"]', "20")
        shot(page, tag, 3, "boiler", side)
        next_(page, "wx")
        # 4 outside temperature
        check(f"[{tag}] says only rounded coordinates are sent", ("1 km" in page.inner_text("#setup")))
        check(f"[{tag}] title and privacy line shown once, not again by the embedded card",
              page.inner_text("#setup").count("1 km") == 1 and page.inner_text("#setup").count("Udetemperatur" if lang == "da" else "Outside temperature") == 1)
        if full:
            page.fill("#wxQ", "Hamburg")
            page.wait_for_selector("[data-wxpick]", timeout=5000)
            page.click("[data-wxpick]")
            page.wait_for_function('document.querySelector("#setup .su-card").dataset.step==="wx" && (((SETS||{}).settings||{}).weather||{}).enabled===true', timeout=8000)
            page.wait_for_timeout(600)
            shot(page, tag, 4, "place", side)
            next_(page, "pel")
        else:
            shot(page, tag, 4, "place", side)
            page.click('#setup [data-su="skip"]'); page.wait_for_selector('#setup .su-card[data-step="pel"]')
        # 5 pellets
        if full:
            page.fill('#setup [data-sup="store"]', "2000")
            page.fill('#setup [data-sup="bag"]', "Test pellets A")
        page.fill('#setup [data-sup="home"]', "600" if full else "300")
        shot(page, tag, 5, "pellets", side)
        next_(page, "ash")
        # 6 ash bin
        if full:
            page.click("#setup [data-suash]")
        shot(page, tag, 6, "ash", side)
        if full:
            next_(page, "plan")
        else:
            page.click('#setup [data-su="skip"]'); page.wait_for_selector('#setup .su-card[data-step="plan"]')
        # 7 heating plan
        page.click('#setup [data-suplan="%s"]' % ("morning" if full else "both"))
        check(f"[{tag}] plan note: saved but stays off", page.evaluate('t("su_plan_off")') in page.inner_text("#setup"))
        shot(page, tag, 7, "plan", side)
        next_(page, "done")
        # 8 done
        page.wait_for_timeout(500)
        summary = page.inner_text("#setup .su-sum")
        shot(page, tag, 8, "done", side)
        check(f"[{tag}] summary lists the answers", ("1.40.1" in summary) and (("15 kW" in summary) if mode == "sim" else ("25 kW" in summary)))
        check(f"[{tag}] done flag set on the monitor when the last step shows", srv.settings().get("setup", {}).get("done") is True)
        page.click('#setup [data-su="open"]')
        page.wait_for_selector(".wrap", state="visible", timeout=5000)
        check(f"[{tag}] 'Open the page' shows the normal page", not page.is_visible("#setup"))
        check(f"[{tag}] no sideways scrolling on any step", all(side) and len(side) == 8)

        s = srv.settings()
        su, ci, pel, warm = s.get("setup", {}), s.get("controller_info", {}), s.get("pellets", {}), s.get("warm", {})
        check(f"[{tag}] settings: language", su.get("lang") == lang)
        check(f"[{tag}] settings: boiler size", su.get("boiler_kw") == (15 if mode == "sim" else 25))
        check(f"[{tag}] settings: the 3 boiler numbers (shared with the service picture)",
              ci == {"asked": True, "sw": "1.40.1", "min_pct": 20, "fan100_rpm": 1800})
        check(f"[{tag}] settings: pellets at home", (pel.get("stock") or {}).get("kg") == (600 if full else 300))
        check(f"[{tag}] settings: plan periods saved, plan still off",
              len(warm.get("periods") or []) == (1 if full else 2) and warm.get("enabled") is False)
        if full:
            check(f"[{tag}] settings: store size and bag name",
                  su.get("store_kg") == 2000 and [b.get("name") for b in pel.get("batches") or []] == ["Test pellets A"])
            check(f"[{tag}] settings: place (rounded) and ash bin emptied",
                  s.get("weather", {}).get("enabled") is True and s.get("weather", {}).get("lat") == round(s["weather"]["lat"], 2)
                  and isinstance(s.get("ash", {}).get("emptied_at"), (int, float)))
        else:
            check(f"[{tag}] settings: skipped steps saved nothing",
                  s.get("weather", {}).get("enabled") is False and s.get("ash", {}).get("emptied_at") is None and su.get("store_kg") is None)

        # second open: straight to the normal page
        page.reload(); page.wait_for_timeout(1500)
        check(f"[{tag}] second open goes straight to the normal page", page.is_visible(".wrap") and not page.is_visible("#setup"))
        # the service picture does not ask again
        page.evaluate('location.hash="#history"'); page.wait_for_timeout(1500)
        if page.query_selector('[data-act="share2"]'):
            with page.expect_download(timeout=15000):
                page.click('[data-act="share2"]')
            check(f"[{tag}] 'Picture for service' does not ask the 3 numbers again", page.query_selector("#ciAsk") is None)
        else:
            check(f"[{tag}] 'Picture for service' button there", False)
        # run the setup again from Settings, then leave it with "Skip the setup"
        page.evaluate('location.hash="#settings/service"')
        page.wait_for_selector('[data-su="again"]', timeout=8000)
        page.click('[data-su="again"]')
        page.wait_for_selector('#setup .su-card[data-step="lang"]', timeout=5000)
        check(f"[{tag}] 'Run the setup again' opens the guide", not page.is_visible(".wrap"))
        if full:
            next_(page, "hi"); next_(page, "boil")
            check(f"[{tag}] run again: answers pre-filled", page.input_value('#setup [data-ci="sw"]') == "1.40.1"
                  and page.query_selector('#setup [data-sukw="%d"].on' % (15 if mode == "sim" else 25)) is not None)
        page.click('#setup [data-su="skipall"]')
        page.wait_for_selector(".wrap", state="visible", timeout=5000)
        check(f"[{tag}] leaving it again keeps everything", srv.settings().get("controller_info", {}).get("min_pct") == 20
              and srv.settings().get("setup", {}).get("done") is True)
        check(f"[{tag}] no console errors or popups", not page.errors or print(page.errors))
        check(f"[{tag}] page script had no errors", page.get_attribute("html", "data-jserr") is None)
    finally:
        ctx.close(); srv.stop()


def skip_all(browser, view):
    tag = f"skipall_{view}"
    srv = Server("none", tag)
    ctx, page = new_page(browser, view)
    try:
        page.goto(srv.url)
        page.wait_for_selector("#setup .su-card", timeout=10000)
        page.click('#setup [data-su="skipall"]')
        page.wait_for_selector(".wrap", state="visible", timeout=5000)
        s = srv.settings()
        check(f"[{tag}] 'Skip the setup' sets done and saves nothing else",
              s.get("setup", {}).get("done") is True and s.get("controller_info", {}).get("asked") is False
              and s.get("setup", {}).get("boiler_kw") is None)
        page.reload(); page.wait_for_timeout(1500)
        check(f"[{tag}] then the normal page opens", page.is_visible(".wrap") and not page.is_visible("#setup"))
        if SHOTS:
            page.screenshot(path=os.path.join(SHOTS, f"setup_{tag}_after.png"))
        check(f"[{tag}] no sideways scrolling", no_sideways(page))
        check(f"[{tag}] no console errors or popups", not page.errors or print(page.errors))
    finally:
        ctx.close(); srv.stop()


try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        walk(browser, "en", "desktop", "none", full=True)
        walk(browser, "da", "phone", "sim", full=False)
        walk(browser, "en", "phone", "sim", full=True)
        walk(browser, "da", "desktop", "none", full=True)
        skip_all(browser, "phone")
        browser.close()
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
