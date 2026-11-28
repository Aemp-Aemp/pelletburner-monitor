"""Browser test (Playwright): "I cleaned the burner" on the home page asks "What did you clean?" with three ticks
(burner and fan ticked, the flame sensor glass not), in English and Danish, on a phone (375 px) and a desktop
(1280 px). Cancel sends nothing; OK sends only the ticked part names. The page runs on the synthetic replay: the
page's POSTs are recorded in the page, and any non-GET that still reaches the network is blocked. No boiler.

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
PORT = 8851


def check(name, cond, why=None):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name + ("" if cond or why is None else f"  {why!r}"[:300]), flush=True)
    FAILED += 0 if cond else 1


WANT = {"en": ("What did you clean?", ["Burner, herd and ash", "Fan and chamber", "Flame sensor glass"], "Cancel"),
        "da": ("Hvad har du renset?", ["Brænder, herd og aske", "Røgsuger og brændkammer", "Flammefølerens glas"],
               "Annuller")}
LOOK = ("()=>{ const s=document.querySelector('.scrim .sheet'), r=s.getBoundingClientRect();"
        " return {q:s.querySelector('.mtext').textContent, labels:[...s.querySelectorAll('label b')].map(b=>b.textContent),"
        " ticks:[...s.querySelectorAll('[data-part]')].map(x=>[x.dataset.part,x.checked]),"
        " no:s.querySelector('[data-a=\"0\"]').textContent, left:r.left, right:r.right, top:r.top, bottom:r.bottom,"
        " w:innerWidth, h:innerHeight, wide:document.documentElement.scrollWidth>innerWidth+1}; }")

with _synth.Replay(PORT) as rp, sync_playwright() as pw:
    br = pw.chromium.launch()
    for lang, (q, labels, cancel) in WANT.items():
        for width in (375, 1280):
            tag = f"[{lang} {width}]"
            ctx = br.new_context(locale="da-DK" if lang == "da" else "en-GB", viewport={"width": width, "height": 800})
            ctx.add_init_script(f"try{{localStorage.setItem('burner_lang','{lang}');}}catch(e){{}}")
            blocked = []

            def guard(route, blocked=blocked):
                r = route.request
                if r.method != "GET" or not r.url.startswith(rp.base + "/"):
                    blocked.append((r.url, r.post_data))
                    return route.abort()
                route.continue_()
            ctx.route("**/*", lambda route: guard(route))
            pg = ctx.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(f"{rp.base}/home?replaybanner=0#history", wait_until="load")
            pg.wait_for_function("typeof askClean==='function'", timeout=15000)
            pg.wait_for_selector("[data-cleaned]", timeout=20000)
            pg.evaluate("()=>{ window._posts=[]; const f=window.fetch; window.fetch=(u,o)=>{ if(o&&o.method&&o.method!=='GET')"
                        " window._posts.push([String(u),o.body]); return f(u,o); }; }")
            sent = lambda: pg.evaluate("()=>window._posts.splice(0)")
            pg.click("[data-cleaned]")
            pg.wait_for_selector(".scrim .sheet.cln", timeout=5000)
            a = pg.evaluate(LOOK)
            check(f"{tag} the button asks what was cleaned", a["q"] == q and a["no"] == cancel)
            check(f"{tag} three ticks, in this language", a["labels"] == labels)
            check(f"{tag} burner and fan ticked, the flame sensor glass not",
                  a["ticks"] == [["burner", True], ["fan", True], ["sensor", False]])
            check(f"{tag} the dialog fits the screen",
                  a["left"] >= 0 and a["right"] <= a["w"] and a["top"] >= 0 and a["bottom"] <= a["h"] and not a["wide"])
            pg.click(".scrim [data-a='0']")
            pg.wait_for_timeout(300)
            check(f"{tag} Cancel closes and sends nothing", not sent() and not pg.query_selector(".scrim"))
            pg.click("[data-cleaned]")
            pg.wait_for_selector(".scrim .sheet.cln", timeout=5000)
            for p in ("burner", "fan"):
                pg.click(f".scrim [data-part='{p}']")
            check(f"{tag} nothing ticked: OK is off", pg.is_disabled(".scrim [data-a='1']")
                  and pg.is_visible(".scrim .cln-none"))
            pg.click(".scrim [data-part='burner']")
            pg.click(".scrim [data-part='sensor']")
            pg.click(".scrim [data-a='1']")
            pg.wait_for_timeout(500)
            got = sent()
            body = json.loads(got[-1][1] or "{}") if got else {}
            check(f"{tag} OK sends only the ticked parts",
                  got and got[-1][0].endswith("api/health/cleaned") and body == {"parts": ["burner", "sensor"]})
            # the replay page's POST is stopped here (nothing is sent); nothing else may try to leave the page
            check(f"{tag} no page errors, nothing else left the page",
                  not errs and all(u.endswith("api/health/cleaned") for u, _ in blocked), (errs, blocked))
            ctx.close()
    br.close()

print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
