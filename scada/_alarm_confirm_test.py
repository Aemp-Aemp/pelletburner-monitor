"""Browser test (Playwright): in ALARM, Start / Stop on the home page first asks 'this RESETS the alarm' (EN + DA)
and only then sends reset_alarm: true; outside an alarm the usual question, without the flag. The page runs on the
synthetic replay: the page's POSTs are recorded in the page and answered by the replay there ("nothing is sent"),
and any non-GET that still reaches the network is blocked. No boiler, no serial port.

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
PORT = 8817


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    FAILED += 0 if cond else 1


WANT = {"en": ("RESET the alarm", "Reset and stop", "Reset and start", "Cancel"),
        "da": ("NULSTILLER alarmen", "Nulstil og stop", "Nulstil og start", "Annuller")}

with _synth.Replay(PORT) as rp, sync_playwright() as pw:
    br = pw.chromium.launch()
    for lang, (warn, lab_stop, lab_start, cancel) in WANT.items():
        ctx = br.new_context(locale="da-DK" if lang == "da" else "en-GB")
        ctx.add_init_script(f"try{{localStorage.setItem('burner_lang','{lang}');}}catch(e){{}}")
        posts = []

        def guard(route, posts=posts):
            q = route.request
            if q.method != "GET" or not q.url.startswith(rp.base + "/"):
                posts.append((q.url, q.post_data))
                return route.abort()
            route.continue_()
        ctx.route("**/*", lambda route: guard(route))
        pg = ctx.new_page()
        pg.goto(f"{rp.base}/home?replaybanner=0#now", wait_until="load")
        pg.wait_for_function("typeof runMode==='function' && typeof askConfirm==='function'", timeout=15000)
        pg.wait_for_timeout(1000)
        # the replay answers a POST in the page itself ("nothing is sent"); record what the page tried to send
        pg.evaluate("()=>{ window._posts=[]; const f=window.fetch; window.fetch=(u,o)=>{ if(o&&o.method&&o.method!=='GET')"
                    " window._posts.push([String(u),o.body]); return f(u,o); }; }")

        def sent():
            posts.extend(pg.evaluate("()=>window._posts.splice(0)"))
            return posts

        def ask(cmd, alarm):
            pg.evaluate("a=>{ REC=Object.assign({},REC||{},a?{state:6,alarm:4}:{state:4,alarm:0}); }", alarm)
            pg.evaluate(f"()=>{{ window._rm=runMode('{cmd}'); }}")
            pg.wait_for_selector(".scrim .sheet", timeout=5000)
            return pg.evaluate("()=>({text:document.querySelector('.scrim .mtext').textContent,"
                               "ok:document.querySelector('.scrim [data-a=\"1\"]').textContent,"
                               "no:document.querySelector('.scrim [data-a=\"0\"]').textContent})")

        a = ask("stop", True)
        check(f"[{lang}] alarm: Stop asks first, names the alarm and says it resets it",
              warn in a["text"] and "(" in a["text"] and a["ok"] == lab_stop and a["no"] == cancel)
        pg.click(".scrim [data-a='0']")
        pg.wait_for_timeout(300)
        check(f"[{lang}] alarm: Cancel sends nothing", not sent())
        a = ask("start", True)
        check(f"[{lang}] alarm: Start asks the same, with its own button", warn in a["text"] and a["ok"] == lab_start)
        pg.click(".scrim [data-a='1']")
        pg.wait_for_timeout(500)
        body = json.loads(posts[-1][1] or "{}") if sent() else {}
        check(f"[{lang}] alarm: after the confirm it sends reset_alarm: true",
              posts and posts[-1][0].endswith("api/run_mode") and body == {"cmd": "start", "reset_alarm": True})
        posts.clear()
        a = ask("stop", False)
        check(f"[{lang}] no alarm: the usual question", warn not in a["text"] and a["ok"] != lab_stop)
        pg.click(".scrim [data-a='1']")
        pg.wait_for_timeout(500)
        body = json.loads(posts[-1][1] or "{}") if sent() else {}
        check(f"[{lang}] no alarm: no reset flag", body == {"cmd": "stop"})
        ctx.close()
    br.close()

print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
