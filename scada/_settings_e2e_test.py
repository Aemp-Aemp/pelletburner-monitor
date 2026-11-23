"""The Settings pages in a real browser (headless Chromium, Playwright), against the real program replaying synthetic
data (scada/_synth.py): the list of groups, a group's own page on a phone and side by side on a desktop, and the one
save rule - a value is saved at once, alone, with "Saved" under the field; a wrong value gives one plain line and
changes nothing. Also: the read-only "about" feed holds no secret, and the "buy before" reserve can be saved.

No boiler, no serial port, no internet: a replay sends nothing anywhere, and it only changes its own temporary
settings file. Skips (exit 0, line "SKIP") when Playwright is not installed in this Python."""
import json, os, sys, tempfile, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import _synth
import burner_features as F
import burner_responder as R

FAILED = 0
PORT = 8817


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    FAILED += 0 if cond else 1


# ---- the monitor's side (no browser)
d = tempfile.mkdtemp()
try:
    st = F.Settings(d, lambda *a: None)
    ok, _ = st.update({"pellets": {"reserve_kg": "200"}})
    check("the 'buy before' reserve can be saved alone", ok and st.get("pellets")["reserve_kg"] == 200)
    check("... and is kept after a restart", F.Settings(d, lambda *a: None).get("pellets")["reserve_kg"] == 200)
    for bad in ({"reserve_kg": -1}, {"reserve_kg": 2001}, {"reserve_kg": "x"}, "x"):
        ok, _ = st.update({"pellets": bad})
        check(f"refuses {bad}", not ok)
    ok, _ = st.update({"pellets": {"stock": {"kg": 5, "at": 1}, "batches": []}})
    check("stock and batches cannot be changed this way", ok and st.get("pellets")["stock"] is None)
finally:
    shutil.rmtree(d, ignore_errors=True)


class Mod:                                               # what about_info() reads from the running program
    about = {"remote_start": True, "keep_days": 30.0, "log_max_mb": 50.0, "serial": "fixed",
             "mqtt": {"on": True, "host": "broker.test", "prefix": "burner", "read_only": False, "user_set": True, "pass_set": True}}


info = R.about_info(Mod(), 8787)
check("about: version, port, days kept, remote start", info["version"] and info["http_port"] == 8787 and info["keep_days"] == 30.0
      and info["remote_start"] is True)
check("about: MQTT password and user only as 'set'", info["mqtt"]["pass_set"] is True and info["mqtt"]["user_set"] is True
      and not any(k in info["mqtt"] for k in ("pass", "password", "user", "topic")))
check("about: connection state added, never a serial adapter name", info["mqtt"]["connected"] is False and info["serial"] in ("auto", "fixed"))
check("about: nothing about phone alerts", "ntfy" not in json.dumps(info))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: Playwright is not installed (pip install playwright; playwright install chromium)")
    print("FAILED:", FAILED)
    sys.exit(1 if FAILED else 0)

GROUPS = ["plan", "boiler", "pellets", "weather", "ha", "screen", "reset", "tank", "service", "about"]   # no phone alerts in a replay

with _synth.Replay(PORT) as rp, sync_playwright() as pw:
    sets = lambda: rp.get("/api/settings")["settings"]
    topic = sets()["ntfy"]["topic"]
    about = json.dumps(rp.get("/api/about"))
    check("about feed of the running program: no alert topic, no password", topic and topic not in about and "pass\"" not in about.replace("pass_set\"", ""))
    br = pw.chromium.launch()

    def new_page(view, lang="en"):
        ctx = br.new_context(viewport={"width": 390, "height": 844} if view == "phone" else {"width": 1280, "height": 900},
                             locale="da-DK" if lang == "da" else "en-GB")
        ctx.add_init_script(f"try{{localStorage.setItem('burner_lang','{lang}');}}catch(e){{}}")
        pg = ctx.new_page()
        pg.errors, pg.posts = [], []
        pg.on("console", lambda m: m.type == "error" and pg.errors.append(m.text))
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.on("dialog", lambda x: (pg.errors.append("browser popup: " + x.message), x.dismiss()))
        pg.on("request", lambda q: q.method != "GET" and pg.posts.append((q.url.split("/", 3)[3], q.post_data)))
        return ctx, pg

    def go(pg, hash_):
        pg.goto(f"{rp.base}/home?replaybanner=0#{hash_}", wait_until="load")
        pg.wait_for_selector("#stList .st-row", timeout=15000)
        pg.wait_for_timeout(600)

    def typed(pg, key, text, how="Tab"):                  # type into a field and leave it (or press Enter)
        pg.fill(f'#stPane [data-f="{key}"]', text)
        pg.press(f'#stPane [data-f="{key}"]', how)
        pg.wait_for_timeout(700)

    msg = lambda pg, key: pg.inner_text(f'#stPane [data-fmsg="{key}"]').strip()
    vis = lambda pg, sel: pg.is_visible(sel)

    # ---- phone: the list, then one group at a time
    ctx, pg = new_page("phone")
    go(pg, "settings")
    rows = pg.eval_on_selector_all("#stList .st-row", "a=>a.map(x=>x.getAttribute('href').split('/')[1])")
    check(f"phone: the list shows every group ({len(rows)})", rows == GROUPS)
    check("phone: the list alone, no group page yet", vis(pg, "#stList") and not vis(pg, "#stPane"))
    check("no 'Show advanced' switch and no big Save button", not pg.query_selector("#setAdv") and not pg.query_selector("#setSave")
          and not pg.query_selector("#ciSave"))
    check("every row says its state in one line", all(pg.eval_on_selector_all("#stList .st-row .s", "a=>a.map(x=>x.textContent.trim().length>0)")))
    pg.click('#stList a[href="#settings/boiler"]'); pg.wait_for_timeout(400)
    check("phone: a tap opens the group's own page", pg.evaluate("location.hash") == "#settings/boiler" and vis(pg, "#stPane")
          and not vis(pg, "#stList") and vis(pg, "#stPane .st-back"))
    check("phone: the Settings tab stays marked", pg.evaluate("document.querySelector('#tabbar a.on').dataset.tab") == "settings")

    # a text and a number: saved when the field is left, with "Saved" under it
    typed(pg, "controller_info.sw", "1.40.1")
    ci = sets()["controller_info"]
    check("software version saved at once, with a 'Saved' tick", ci["sw"] == "1.40.1" and "Saved" in msg(pg, "controller_info.sw"))
    check("... one value per save, and typed here counts as asked",
          pg.posts[-1] == ("api/settings", '{"controller_info":{"sw":"1.40.1","asked":true}}') and ci["asked"] is True)
    n = len(pg.posts)
    typed(pg, "controller_info.min_pct", "500")
    check("a wrong number: one plain line, the field marked, nothing sent",
          msg(pg, "controller_info.min_pct") == "Type a whole number from 1 to 100." and len(pg.posts) == n
          and "bad" in pg.get_attribute('#stPane [data-f="controller_info.min_pct"]', "class") and sets()["controller_info"]["min_pct"] is None)
    typed(pg, "controller_info.min_pct", "20", "Enter")
    check("corrected and Enter: saved, the red line is gone", sets()["controller_info"]["min_pct"] == 20
          and "Saved" in msg(pg, "controller_info.min_pct") and "bad" not in pg.get_attribute('#stPane [data-f="controller_info.min_pct"]', "class"))
    pg.wait_for_timeout(4500)
    check("the 'Saved' tick goes away by itself", msg(pg, "controller_info.min_pct") == "")
    pg.click('#stPane [data-fpick="setup.boiler_kw"][data-v="25"]'); pg.wait_for_timeout(700)
    check("boiler size can be changed outside the guide", sets()["setup"]["boiler_kw"] == 25
          and pg.query_selector('#stPane [data-fpick="setup.boiler_kw"][data-v="25"].on') is not None)
    typed(pg, "controller_info.sw", "")
    check("an optional value can be emptied again", sets()["controller_info"]["sw"] == "")
    check("the list shows what is missing", "2 numbers missing" in pg.evaluate(
        "document.querySelector('#stList a[href=\"#settings/boiler\"] .s').textContent"))

    pg.click("#stPane .st-back"); pg.wait_for_timeout(400)
    check("phone: back to the list", pg.evaluate("location.hash") == "#settings" and vis(pg, "#stList") and not vis(pg, "#stPane"))
    pg.click('#stList a[href="#settings/pellets"]'); pg.wait_for_timeout(400)
    typed(pg, "setup.store_kg", "2000"); typed(pg, "pellets.reserve_kg", "200"); typed(pg, "ash.bin_kg", "1,5")
    s = sets()
    check("store size, 'buy before' reserve and ash bin size saved (decimal comma works)",
          s["setup"]["store_kg"] == 2000 and s["pellets"]["reserve_kg"] == 200 and s["ash"]["bin_kg"] == 1.5)
    typed(pg, "pellets.reserve_kg", "")
    check("a value that must be there cannot be emptied", sets()["pellets"]["reserve_kg"] == 200 and "0" in msg(pg, "pellets.reserve_kg"))

    # the heating plan: the switch lets the page act on the burner, so it asks first
    pg.evaluate('location.hash="#settings/plan"'); pg.wait_for_timeout(500)
    check("stop at power: the help says when it stops, with the monitor's own minutes",
          "burned at least 20 minutes" in pg.inner_text("#stPane") and "this % for 2 minutes" in pg.inner_text("#stPane"))
    typed(pg, "warm.stop_pct", "45", "Enter")
    w = sets()["warm"]
    check("stop at power saved at once (and no longer taken from the boiler)", w["stop_pct"] == 45 and w["use_board"] is False)
    check("the field keeps the saved value", pg.input_value('#stPane [data-f="warm.stop_pct"]') == "45")
    pg.click("#stPane details.f-more summary"); pg.wait_for_timeout(200)
    typed(pg, "warm.pause_h", "49")
    check("more rules: limits checked on the page", sets()["warm"]["pause_h"] == w["pause_h"] and "48" in msg(pg, "warm.pause_h"))
    check("more rules stays open after a redraw", pg.evaluate("renderSettings(true),document.querySelector('#stPane details.f-more').open"))
    periods = w["periods"]
    pg.click('#stPane .sw2:has([data-f="warm.enabled"])'); pg.wait_for_selector(".scrim .sheet", timeout=3000)
    check("turning the plan on asks first", "plan" in pg.inner_text(".scrim .mtext").lower() and sets()["warm"]["enabled"] is False)
    pg.click('.scrim [data-a="0"]'); pg.wait_for_timeout(400)
    check("cancel: still off, switch back", sets()["warm"]["enabled"] is False and not pg.is_checked('#stPane [data-f="warm.enabled"]'))
    pg.click('#stPane .sw2:has([data-f="warm.enabled"])'); pg.wait_for_selector(".scrim .sheet", timeout=3000)
    pg.click('.scrim [data-a="1"]'); pg.wait_for_timeout(900)
    w = sets()["warm"]
    check("confirmed: the plan is on, the periods untouched", w["enabled"] is True and w["periods"] == periods)
    pg.click('#stPane .sw2:has([data-f="warm.enabled"])'); pg.wait_for_timeout(900)
    check("turning it off needs no question", sets()["warm"]["enabled"] is False and not pg.query_selector(".scrim"))

    pg.evaluate('location.hash="#settings/reset"'); pg.wait_for_timeout(500)
    pg.check('#stPane [data-ar="12"]'); pg.wait_for_timeout(700)
    check("alarm reset: a ticked alarm is saved at once", sets()["auto_reset"]["alarms"] == [4, 5, 6, 12] and sets()["auto_reset"]["enabled"] is False)
    pg.evaluate('location.hash="#settings/tank"'); pg.wait_for_timeout(500)
    pg.select_option('#stPane [data-f="vvb.source"]', "http"); pg.wait_for_timeout(700)
    check("tank sensor: a choice is saved at once", sets()["vvb"]["source"] == "http" and sets()["vvb"]["enabled"] is False)

    # service & relearn: reachable from home; each asks first
    pg.evaluate('location.hash="#settings/service"'); pg.wait_for_timeout(500)
    check("service: picture, 4 relearn buttons, the guide and the classic link",
          vis(pg, '#stPane [data-act="share2"]') and len(pg.query_selector_all("#stPane [data-stsv]")) == 4
          and vis(pg, '#stPane [data-su="again"]') and vis(pg, '#stPane a[href="classic"]'))
    n = len(pg.posts)
    pg.click('#stPane [data-stsv="fan"]'); pg.wait_for_selector(".scrim .sheet", timeout=3000)
    pg.click('.scrim [data-a="0"]'); pg.wait_for_timeout(300)
    check("relearn asks first; cancel sends nothing", len(pg.posts) == n)
    pg.click('#stPane [data-stsv="fan"]'); pg.wait_for_selector(".scrim .sheet", timeout=3000)
    pg.click('.scrim [data-a="1"]'); pg.wait_for_timeout(800)
    check("relearn confirmed: one call to the monitor's own memory", [p[0] for p in pg.posts[n:]] == ["api/fan/reset"])
    check("nothing on these pages ever asks for a start or stop", not any("run_mode" in p[0] for p in pg.posts))
    pg.evaluate('location.hash="#settings/about"'); pg.wait_for_timeout(1200)
    txt = pg.inner_text("#stPane")
    ab = rp.get("/api/about")
    check("about: version, page port and days kept, read-only", str(rp.port) in txt and ab["version"] in txt
          and f"{ab['keep_days']:,.0f}" in txt and not pg.query_selector("#stPane input"))
    check("about and Home Assistant never show the alert topic", topic not in txt)
    pg.evaluate('location.hash="#settings/ha"'); pg.wait_for_timeout(600)
    check("Home Assistant: read-only, says it is not set up here", not pg.query_selector("#stPane input") and "not set up" in pg.inner_text("#stPane"))
    check("phone: no sideways scrolling", pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
    check("phone: no console errors or popups" + (f" {pg.errors[:3]}" if pg.errors else ""), not pg.errors)
    check("page script had no errors", pg.get_attribute("html", "data-jserr") is None)
    ctx.close()

    # ---- desktop: list and group side by side; Danish
    ctx, pg = new_page("desktop", "da")
    go(pg, "settings")
    check("desktop: list left, the first group right", vis(pg, "#stList") and vis(pg, "#stPane") and not vis(pg, "#stPane .st-back")
          and pg.evaluate("document.querySelector('#stList .st-row.on').getAttribute('href')") == "#settings/plan")
    pg.click('#stList a[href="#settings/pellets"]'); pg.wait_for_timeout(500)
    check("desktop: a click opens the group beside the list", vis(pg, "#stList") and pg.input_value('#stPane [data-f="setup.store_kg"]') == "2000"
          and pg.evaluate("document.querySelector('#stList .st-row.on').getAttribute('href')") == "#settings/pellets")
    typed(pg, "ash.bin_kg", "abc")
    check("Danish: the error line is Danish and plain", msg(pg, "ash.bin_kg") == "Skriv et tal fra 0 til 200.")
    typed(pg, "ash.bin_kg", "2")
    check("Danish: 'Gemt' under the field", "Gemt" in msg(pg, "ash.bin_kg") and sets()["ash"]["bin_kg"] == 2)
    # a field being typed in is not wiped when the page is drawn again (every minute, or after another save)
    pg.fill('#stPane [data-f="pellets.reserve_kg"]', "17")
    pg.evaluate("renderSettings()")
    check("a redraw never wipes a field that is being typed in", pg.input_value('#stPane [data-f="pellets.reserve_kg"]') == "17")
    pg.evaluate('location.hash="#plan"'); pg.wait_for_selector("#plSave", timeout=5000)
    check("the Plan tab keeps its own Save button and links to the rules", vis(pg, "#plSave") and vis(pg, '#v-plan a[href="#settings/plan"]'))
    # the store size is the boiler's silo; bags at home are counted apart (no "more than the store" warning)
    pane = pg.inner_text("#stPane")
    check("Danish: 'Silostørrelse (kg)' with a plain help line", "Silostørrelse (kg)" in pane and "Sække derhjemme" in pane
          and pg.evaluate("typeof pelOverHTML") == "undefined")
    nt = pg.evaluate("ntCardHTML()")
    check("phone alerts: the topic in a copy field with a Copy button, no 'scan in the app'",
          'id="ntTopicCopy"' in nt and f'value="{topic}"' in nt and 'data-nt="copy"' in nt and "Kopiér" in nt
          and "kamera" in nt)
    check("phone alerts: the alerts topic shown once (no address fold, no 2nd topic field), 'New alerts topic' at the top",
          nt.count(topic) == 1 and 'data-fold="ntaddr"' not in nt and 'id="ntTopic"' not in nt
          and nt.count('data-nt="new"') == 1 and "Nyt beskedemne" in nt and "Egen server og certifikater" in nt
          and "Beskedemne:" in nt and "Kommando-emne:" in nt and "Nyt kommando-emne" in nt)
    check("phone commands: the PIN field shows the PIN, with 'Lav en PIN' and 'Gem PIN'",
          'id="pcPin" type="text"' in nt and 'data-pc="gen"' in nt and "Lav en PIN" in nt and "Gem PIN" in nt)
    check("desktop: no console errors or popups" + (f" {pg.errors[:3]}" if pg.errors else ""), not pg.errors)
    ctx.close()
    br.close()

print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
