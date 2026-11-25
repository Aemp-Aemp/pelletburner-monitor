"""Load test of the two web pages in a real browser (headless Chromium, Playwright): scada/home.html and
scada/dashboard.html are served by the real program replaying synthetic data (scada/_synth.py), and every main tab
is opened in English and Danish, and every group of the Settings tab on a phone and on a desktop, and the cards that
came over from the classic page (Plan: how the plan works out; History: short burns, the fan line), and where the two
day charts sit (Starts per day on History, Pellets per day on Pellets & ash). Checks: no console
errors, each tab and group shows content, every text exists in both languages, the page sends nothing.
Every request that is not a GET, or that goes to another host, is blocked. No boiler, no serial port, no internet.

Skips (exit 0, line "SKIP") when Playwright is not installed in this Python."""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _synth

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: Playwright is not installed (pip install playwright; playwright install chromium)")
    sys.exit(0)

FAILED = 0
PORT = 8816
# page -> (url path, tabs, CSS selector of a tab's content)
PAGES = {"home": ("/home", ("now", "pellets", "history", "plan", "settings"), "#v-{}"),
         "classic": ("/classic", ("live", "seq", "sch", "opt", "disc"), "#tab-{}")}
# the Settings tab: #settings = the list of groups, #settings/<group> = that group's own page
GROUPS = ("plan", "boiler", "pellets", "alerts", "weather", "ha", "screen", "reset", "tank", "service", "about")
VIEWS = {"phone": {"width": 390, "height": 844}, "desktop": {"width": 1366, "height": 900}}
# a replay has no phone alerts: a made-up answer (no real address) so that group can be drawn too
NTFY = {"available": True, "subscribe_url": "https://ntfy.example/burner-example-topic", "qr_svg": "", "last": None,
        "cert": {"count": 0}}


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    FAILED += 0 if cond else 1


with _synth.Replay(PORT) as rp, sync_playwright() as pw:
    br = pw.chromium.launch()
    INS = rp.get("/api/insights")
    cards = len(INS["cards"])
    idata = {c["id"]: c.get("data") for c in INS["cards"]}
    OPT = rp.get("/api/optimiser")
    OPT["days"][0]["short_burns"] = 1            # the synthetic burns are all long: mark one short, so its bar part shows
    texts = {}
    for lang in ("en", "da"):
        ctx = br.new_context(locale="da-DK" if lang == "da" else "en-GB", viewport={"width": 1366, "height": 900})
        ctx.add_init_script(f"try{{localStorage.setItem('burner_lang','{lang}');}}catch(e){{}}")
        blocked = []

        def guard(route, blocked):
            q = route.request
            if q.method != "GET" or not q.url.startswith(rp.base + "/"):
                blocked.append(f"{q.method} {q.url}")
                return route.abort()
            route.continue_()
        ctx.route("**/*", lambda route, blocked=blocked: guard(route, blocked))
        for page, (path, tabs, sel) in PAGES.items():
            for tab in tabs:
                pg, errs = ctx.new_page(), []
                pg.on("console", lambda m, e=errs: m.type == "error" and e.append(m.text))
                pg.on("pageerror", lambda x, e=errs: e.append("pageerror: " + str(x)))
                pg.goto(f"{rp.base}{path}?replaybanner=0#{tab}", wait_until="load")
                try:
                    pg.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                pg.wait_for_timeout(1500)
                shown = pg.evaluate("s=>{const e=document.querySelector(s);"
                                    "return e&&e.offsetParent!==null?e.innerText.trim():''}", sel.format(tab))
                texts[(page, tab, lang)] = shown
                check(f"[{lang}] {page}#{tab}: no console errors" + (f" {errs[:3]}" if errs else ""), not errs)
                check(f"[{lang}] {page}#{tab}: the tab shows content ({len(shown)} characters)", len(shown) > 40)
                if (page, tab) == ("home", "now"):
                    # the heating plan stopped it at low power and waits for its next start: say "resting until"
                    r = pg.evaluate("""()=>{const old=S, nx=new Date(2030,0,1,9,0).getTime()/1000, out={};
                        S=Object.assign({}, S||{}, {warm:{enabled:true, rest_since:nx-7200, next_start:nx}});
                        out.a=statusText({state:0});
                        S.warm={enabled:true, rest_since:nx-7200, next_start:null}; out.b=statusText({state:0});
                        S.warm={enabled:true, rest_since:null, next_start:nx}; out.c=statusText({state:0});
                        S=old; return out;}""")
                    want = {"en": ("Resting until 09:00", "Resting"), "da": ("Hviler til 09:00", "Hviler")}[lang]
                    check(f"[{lang}] home: plan rest headline says until when ({r['a']['h']})", r["a"]["h"] == want[0])
                    check(f"[{lang}] home: plan rest without a next start ({r['b']['h']})", r["b"]["h"] == want[1])
                    check(f"[{lang}] home: no 'paused by the page' for the plan",
                          all("page" not in x["h"].lower() and "siden" not in x["h"] for x in r.values()))
                if (page, tab, lang) == ("classic", "live", "en"):
                    # pellet use from the silo counter (whole kg): made-up burn, 1 kg every 30 min, read every 10 s
                    r = pg.evaluate("""()=>{siloBuf=[]; siloSteps=[]; sessionRef=null; let v=[], t0=1e9;
                        for(let i=0;i<=720;i++){ v.push(kgph({epoch:t0+i*10, silo_kg:200-Math.floor(i/180), state:4})); }
                        const off=kgph({epoch:t0+7210, silo_kg:196, state:0});
                        siloBuf=[]; siloSteps=[]; sessionRef=null; return {v, off};}""")
                    shown = [x for x in r["v"] if x is not None]
                    check("classic: pellet use = kg between silo steps / time between them (2 kg/h, not 4+)",
                          len(shown) == 361 and all(abs(x - 2.0) < 0.01 for x in shown))
                    check("classic: no pellet use before two silo steps", all(x is None for x in r["v"][:360]))
                    check("classic: no pellet use while the burner is off", r["off"] is None)
                if (page, tab) == ("classic", "opt"):
                    # switching the language on the Analysis tab redraws the ash plate line at once (not at the next poll)
                    other = "da" if lang == "en" else "en"
                    r = pg.evaluate("""([o,l])=>{const old=ASH; ASH={turn_hist:[[1e9,6],[1e9+600,6.5]]}; renderAshTurns();
                        const a=document.getElementById('atNow').textContent; setLang(o);
                        const b=document.getElementById('atNow').textContent; setLang(l); ASH=old; renderAshTurns();
                        return {a,b};}""", [other, lang])
                    want = {"en": "Last turn", "da": "Sidste drejning"}
                    check(f"[{lang}] classic#opt: ash plate line follows a language switch ({r['b']})",
                          r["a"].startswith(want[lang]) and r["b"].startswith(want[other]))
                if (page, tab) == ("classic", "seq"):
                    # RUNNING (4) has no card: the note says no step is active, instead of pointing at a highlighted card
                    r = pg.evaluate("""()=>{const old=lastRec; renderSeq({state:4}); const a=document.querySelector('#seqWrap .seqnote').innerText;
                        renderSeq({state:5}); const b=document.querySelector('#seqWrap .seqnote').innerText,
                        n=document.querySelectorAll('#seqWrap .seqcard.active').length; renderSeq(old); return {a,b,n};}""")
                    want = {"en": "none of these steps is active", "da": "ingen af disse trin er aktive"}[lang]
                    check(f"[{lang}] classic#seq: RUNNING says no step is active ({r['a'][-45:]})", want in r["a"])
                    check(f"[{lang}] classic#seq: PAUSE highlights its card, no 'none' line", r["n"] == 1 and want not in r["b"])
                if (page, tab) == ("classic", "sch"):
                    # the heating plan status comes as a code + params: the words are the page's, in its language
                    r = pg.evaluate("""()=>{const old=LASTWM, o=v=>({enabled:true, history:[], next_start:null, stop_pct:20, status:v});
                        renderWarm(o({code:"keep", text:"keeping warm (Bad)", p:{name:"Bad"}}));
                        const a=document.getElementById('wmStatus').innerText;
                        renderWarm(o({code:"keep_wait", text:"x", p:{name:"Bad", c:8}}));
                        const b=document.getElementById('wmStatus').innerText;
                        renderWarm(o({code:"new_code", text:"server text"}));
                        const c=document.getElementById('wmStatus').innerText; if(old) renderWarm(old); return {a,b,c};}""")
                    want = {"en": ("keeping warm now (Bad)", "cooled 8 °C"), "da": ("holder varmen nu (Bad)", "kølet 8 °C af")}[lang]
                    check(f"[{lang}] classic#sch: heating plan 'keep' in the page language ({r['a']})", want[0] in r["a"])
                    check(f"[{lang}] classic#sch: heating plan 'keep_wait' with its numbers ({r['b']})", want[1] in r["b"])
                    check(f"[{lang}] classic#sch: an unknown code still shows the server text", r["c"] == "server text")
                if (page, tab) == ("classic", "disc"):
                    n = pg.evaluate("document.getElementById('discCards').children.length")
                    check(f"[{lang}] classic#disc: one card per discovery ({n} of {cards})", cards > 0 and n == cards)
                pg.close()
        check(f"[{lang}] the pages send nothing on their own" + (f" {blocked[:3]}" if blocked else ""), not blocked)
        ctx.close()
        for view, vp in VIEWS.items():
            ctx = br.new_context(locale="da-DK" if lang == "da" else "en-GB", viewport=vp)
            ctx.add_init_script(f"try{{localStorage.setItem('burner_lang','{lang}');}}catch(e){{}}")
            blocked = []
            ctx.route("**/*", lambda route, blocked=blocked: guard(route, blocked))
            ctx.route("**/api/ntfy", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(NTFY))
                      if route.request.method == "GET" else route.abort())
            pg, errs = ctx.new_page(), []
            pg.on("console", lambda m, e=errs: m.type == "error" and e.append(m.text))
            pg.on("pageerror", lambda x, e=errs: e.append("pageerror: " + str(x)))
            pg.goto(f"{rp.base}/home?replaybanner=0#settings", wait_until="load")
            pg.wait_for_selector(f"#stList .st-row:nth-child({len(GROUPS) - 4})", timeout=15000)      # 7 everyday groups
            pg.wait_for_timeout(800)
            look = lambda: pg.evaluate("""()=>{const v=s=>{const e=document.querySelector(s);return !!e&&e.offsetParent!==null};
                return {list:v('#stList'),pane:v('#stPane'),back:v('#stPane .st-back'),
                        rows:[...document.querySelectorAll('#stList .st-row')].map(a=>a.getAttribute('href')),
                        states:[...document.querySelectorAll('#stList .st-row .s')].every(e=>e.textContent.trim().length>0),
                        on:(document.querySelector('#stList .st-row.on')||{getAttribute:()=>''}).getAttribute('href'),
                        h:(document.querySelector('#stPane h2')||{}).textContent||'',text:document.querySelector('#stPane').innerText.trim(),
                        tab:[...document.querySelectorAll('[data-tab].on')].every(a=>a.dataset.tab==='settings'),
                        wide:document.documentElement.scrollWidth>window.innerWidth+1}}""")
            at = look()
            check(f"[{lang} {view}] settings: the list has every group, each with its state in one line",
                  at["rows"] == ["#settings/" + g for g in GROUPS] and at["states"])
            check(f"[{lang} {view}] settings: " + ("the list alone" if view == "phone" else "list and first group side by side"),
                  at["list"] and at["pane"] == (view == "desktop") and not at["back"])
            texts[("settings", "list", lang)] = pg.evaluate("document.querySelector('#stList').innerText")
            for g in GROUPS:
                pg.evaluate("h=>{location.hash=h}", f"#settings/{g}")
                pg.wait_for_function("g=>document.querySelector('#stPane').dataset.g.startsWith(g)", arg=g, timeout=5000)
                pg.wait_for_timeout(250)
                at = look()
                ok = at["pane"] and at["tab"] and len(at["h"]) > 2 and len(at["text"]) > 40 and not at["wide"]
                ok = ok and ((not at["list"] and at["back"]) if view == "phone" else (at["list"] and not at["back"] and at["on"] == f"#settings/{g}"))
                check(f"[{lang} {view}] settings/{g}: its own page ({len(at['text'])} characters)", ok)
                texts[("settings", g, lang)] = at["text"]
                if g == "about" and view == "desktop":
                    # the port the browser uses; the monitor's own port only when it differs (port 80 forwarded to it)
                    pp = pg.evaluate("""()=>({a:pagePort({http_port:8787},""), b:pagePort({http_port:8787},"8787"), c:pagePort({},""),
                        shown:document.querySelector('#stPane').innerText})""")
                    want = {"en": "80 (inside: 8787)", "da": "80 (indeni: 8787)"}[lang]
                    check(f"[{lang}] settings/about: page port as the browser uses it ({pp['a']}, {pp['b']}, {pp['c']})",
                          pp["a"] == want and pp["b"] == "8787" and pp["c"] == "80")
                    check(f"[{lang}] settings/about: shows this page's port {PORT}", str(PORT) in pp["shown"])
            # ---- the cards that came over from the classic page
            ctx.route("**/api/optimiser", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(OPT))
                      if route.request.method == "GET" else route.abort())
            p2 = ctx.new_page()
            p2.on("console", lambda m, e=errs: m.type == "error" and e.append(m.text))
            p2.on("pageerror", lambda x, e=errs: e.append("pageerror: " + str(x)))
            p2.goto(f"{rp.base}/home?replaybanner=0#plan", wait_until="load")
            p2.wait_for_selector("#poCard", timeout=15000)
            po = p2.evaluate("""()=>({marks:document.querySelectorAll('#wiRow>div').length,rows:[...document.querySelectorAll('#ppRows>div')].map(r=>r.innerText.trim()),
                text:document.querySelector('#poCard').innerText.trim(),wide:document.documentElement.scrollWidth>window.innerWidth+1,
                fits:[...document.querySelectorAll('#poCard *')].every(e=>e.getBoundingClientRect().right<=document.querySelector('#poCard').getBoundingClientRect().right+1)})""")
            check(f"[{lang} {view}] plan: 'warm in time' has one mark per start, 'pellets per period' one row per period",
                  po["marks"] == len(idata["warm_by"]["starts"]) > 0 and len(po["rows"]) == len(idata["pellets_period"]["periods"]) > 0
                  and all("kg" in r for r in po["rows"]))
            check(f"[{lang} {view}] plan: the card fits the screen", not po["wide"] and po["fits"])
            texts[("card", "plan", lang)] = po["text"]
            p2.evaluate("location.hash='#history'")
            p2.wait_for_selector("#cSPD svg", timeout=15000)
            p2.wait_for_timeout(300)
            hi = p2.evaluate("""()=>({parts:document.querySelectorAll('#cSPD .bar-part').length,leg:(document.querySelector('#spdLeg')||{innerText:''}).innerText.trim(),
                fan:[...document.querySelectorAll('#health .hc>*')].map(r=>r.innerText.trim())[2]||'',wide:document.documentElement.scrollWidth>window.innerWidth+1})""")
            check(f"[{lang} {view}] history: the short burn is a light part of its bar, with a legend", hi["parts"] == 1 and len(hi["leg"]) > 10 and not hi["wide"])
            check(f"[{lang} {view}] history: burner health has its fan line ({hi['fan'][:40]!r})", len(hi["fan"]) > 8 and "{" not in hi["fan"])
            texts[("card", "history", lang)] = hi["leg"] + hi["fan"]
            # ---- the day charts: each in its own full-width card, "Starts per day" on History, "Pellets per day" on Pellets & ash
            lay = """v=>{const k=[...document.getElementById(v).children], at=q=>k.findIndex(e=>e.matches(q)||e.querySelector(q)), w=e=>Math.round(e.getBoundingClientRect().width);
                return {at:{ign:at('#cIgn'),batch:at('#batchRow'),spd:at('#cSPD'),ppd:at('#cPPD'),ash:at('#cAsh'),hero:at('.phero')},
                    same:k.filter(e=>e.matches('.card,.phero')).every(e=>w(e)===w(k[k.length-1])),bars:document.querySelectorAll('#cPPD svg rect').length,
                    dots:document.querySelectorAll('#cPPD svg circle').length,note:!!document.querySelector('#cPPD .note'),
                    wide:document.documentElement.scrollWidth>window.innerWidth+1}}"""
            la = p2.evaluate(lay, "v-history")["at"] | {"same": p2.evaluate(lay, "v-history")["same"]}
            check(f"[{lang} {view}] history: 'Starts per day' is a full-width card under the lighting times, 'Pellets per day' is not here",
                  0 <= la["ign"] < la["batch"] < la["spd"] == la["batch"] + 1 and la["ppd"] == -1 and la["same"])
            p2.evaluate("location.hash='#pellets'")
            p2.wait_for_selector("#cPPD svg", timeout=15000)
            p2.wait_for_timeout(300)
            la = p2.evaluate(lay, "v-pellets")
            check(f"[{lang} {view}] pellets: 'Pellets per day' is a full-width card between the pellets and the ash bin ({la['bars']} bars, {la['dots']} dots)",
                  la["at"]["hero"] == 0 and la["at"]["ppd"] == 1 and la["at"]["ash"] == 2 and la["at"]["spd"] == -1 and la["same"]
                  and la["bars"] > 0 and (la["dots"] > 0) == la["note"] and not la["wide"])
            if view == "phone":
                # the day chart on a phone: crowded start times use a third label row instead of losing one (made-up plan)
                p2.evaluate("location.hash='#now'")
                p2.wait_for_selector("#v-now", state="visible", timeout=15000)
                lb = p2.evaluate("""()=>{const old=S.warm, D=new Date(nowEp()*1000); D.setHours(0,0,0,0); const d=D.getTime()/1000,
                    P=[[5,7,"keep"],[9,9.5,"start"],[13,13.5,"start"],[16.5,19.5,"keep"],[17.25,17.75,"start"]].map(([a,b,k])=>({a:d+a*3600,b:d+b*3600,kind:k,name:"x"}));
                    S.warm={enabled:true, plan:P}; const el=document.getElementById('todayCard'); renderToday(el,false);
                    const r=[...el.querySelectorAll('svg text[font-size="10.5"]')].map(t=>t.textContent); S.warm=old; return r;}""")
                check(f"[{lang} phone] now: every plan time has its label on the day chart ({lb})",
                      all(any(h in x for x in lb) for h in ("05:00", "09:00", "13:00", "16:30", "17:15")))
            p2.close()
            if view == "phone":
                pg.click("#stPane .st-back")
                pg.wait_for_timeout(300)
                check(f"[{lang} phone] settings: back to the list", pg.evaluate("location.hash") == "#settings" and look()["list"])
            miss = pg.evaluate("[Object.keys(I18N.en).filter(k=>!(k in I18N.da)),Object.keys(I18N.da).filter(k=>!(k in I18N.en))]")
            check(f"[{lang} {view}] settings: no console errors" + (f" {errs[:3]}" if errs else ""), not errs
                  and pg.get_attribute("html", "data-jserr") is None)
            check(f"[{lang} {view}] settings: the pages send nothing on their own" + (f" {blocked[:3]}" if blocked else ""), not blocked)
            if view == "phone":
                check(f"[{lang}] every text of the home page is in English and Danish" + (f" {miss}" if miss[0] or miss[1] else ""),
                      not miss[0] and not miss[1])
                holes = pg.evaluate("(()=>{ const h=v=>[...new Set(JSON.stringify(v).match(/\\{\\w+\\}/g)||[])].sort().join();"
                                    " return Object.keys(I18N.en).filter(k=>k in I18N.da&&h(I18N.en[k])!==h(I18N.da[k])); })()")
                check(f"[{lang}] the same {{placeholders}} in English and Danish" + (f" {holes}" if holes else ""), not holes)
            ctx.close()
    br.close()
    for page, (path, tabs, sel) in PAGES.items():
        same = [t for t in tabs if texts[(page, t, "en")] == texts[(page, t, "da")]]
        check(f"{page}: Danish text differs from English on every tab" + (f" (same: {same})" if same else ""), not same)
    same = [c for c in ("plan", "history") if texts[("card", c, "en")] == texts[("card", c, "da")]]
    check("the new cards: Danish text differs from English" + (f" (same: {same})" if same else ""), not same)
    same = [g for g in ("list",) + GROUPS if texts[("settings", g, "en")] == texts[("settings", g, "da")]]
    check("settings: Danish text differs from English in every group" + (f" (same: {same})" if same else ""), not same)

print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
