#!/usr/bin/env python3
"""Screenshots of the real web pages at chosen moments of a recorded period (uses tools/replay.py + Playwright).

    <python with playwright> tools\\replay_snapshots.py --data <data-folder> \\
        --from "2000-01-01 06:00" --to "2000-01-01 12:00" --at "06:10,06:20,07:00" \\
        --views home:now,home:history --lang en --theme light --device desktop --out C:\\temp\\shots

Moments: --at (list of 'HH:MM' or 'YYYY-MM-DD HH:MM'), --every 30m, --state-changes (each state change in the
period, shown --after seconds later). They are visited in time order: the replay jumps forward and holds (paused)
at each one, so the page and its clock stand still while the screenshots are taken.
Views: home:now home:pellets home:history home:plan home:settings classic:live classic:seq classic:opt
classic:sch classic:disc (page:tab).  Per moment and view, for every --lang x --theme x --device:
  <out>/<YYYYMMDD-HHMMSS>_<page>-<tab>_<lang>_<theme>_<device>.png   screenshot (full page)
  ... .txt                                                          the page's visible text
and per moment <out>/<stamp>_status.json (the program's /status.json then). <out>/index.json lists every shot with
the boiler state and the page's console errors; <out>/index.md is the same as a short table.
"""
import argparse, datetime, json, os, re, subprocess, sys, tempfile, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import replay as RP

DEVICES = {"desktop": {"viewport": {"width": 1366, "height": 900}},
           "phone": {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True,
                     "device_scale_factor": 2}}
STATE = {0: "STOPPET (off)", 1: "WAITING (rest)", 2: "OPSTART (lighting)", 3: "IGNITION (lighting)",
         4: "DRIFT (burning)", 5: "PAUSESTOP", 6: "ALARM", 7: "REDDER BAL"}


def moments(a, t_from, t_to):
    out = set()
    for s in [x for x in (a.at or "").split(",") if x.strip()]:
        out.add(RP.parse_when(s.strip(), base=t_from))
    if a.every:
        m = re.fullmatch(r"(\d+)\s*([smh]?)", a.every.strip())
        if not m:
            sys.exit("--every like 30m, 2h or 900s")
        step = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "": 60}[m.group(2)]
        t = t_from
        while t <= t_to:
            out.add(t); t += step
    if a.state_changes:
        last = None
        for r in RP.iter_raw_rows(a.data, t_from, t_to):
            st = r.get("state")
            if last is not None and st != last:
                out.add(min(t_to, int(r["epoch"]) + 1 + a.after))
            last = st
    bad = [x for x in out if not t_from <= x <= t_to]
    if bad:
        sys.exit("moments outside --from/--to: " + ", ".join(RP.iso(x) for x in sorted(bad)))
    return sorted(out)


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=30) as r:
            return json.loads(r.read())

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Burner": "1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True)
    ap.add_argument("--from", dest="t_from", required=True)
    ap.add_argument("--to", dest="t_to", required=True)
    ap.add_argument("--at", default="", help="comma list of moments")
    ap.add_argument("--every", default="", help="e.g. 30m")
    ap.add_argument("--state-changes", action="store_true", help="a moment after every state change")
    ap.add_argument("--after", type=int, default=30, help="seconds after a state change (default 30)")
    ap.add_argument("--views", default="home:now,home:history")
    ap.add_argument("--lang", default="en", help="en,da")
    ap.add_argument("--theme", default="light", help="light,dark")
    ap.add_argument("--device", default="desktop", help="desktop,phone")
    ap.add_argument("--out", required=True)
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--speed", type=float, default=0,
                    help="play from moment to moment at this speed (e.g. 120) instead of fast-forwarding")
    ap.add_argument("--settle", type=float, default=3.0, help="seconds to let a page draw before the shot")
    ap.add_argument("--no-banner", action="store_true", help="hide the small REPLAY badge on the pages")
    a = ap.parse_args()
    t_from = RP.parse_when(a.t_from)
    t_to = RP.parse_when(a.t_to, base=t_from)
    when = moments(a, t_from, t_to)
    if not when:
        sys.exit("no moments: give --at, --every or --state-changes")
    os.makedirs(a.out, exist_ok=True)
    work = tempfile.mkdtemp(prefix="pb-replay-shots-")
    log = open(os.path.join(a.out, "replay.log"), "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "replay.py"), "--data", a.data, "--from", RP.iso(t_from),
                             "--to", RP.iso(t_to), "--port", str(a.port), "--pause", "--work", work],
                            stdout=log, stderr=subprocess.STDOUT)
    c = Client(a.port)
    from playwright.sync_api import sync_playwright
    index = []
    try:
        for _ in range(300):
            time.sleep(0.2)
            try:
                c.get("/api/replay"); break
            except Exception:
                if proc.poll() is not None:
                    sys.exit("replay.py stopped - see " + os.path.join(a.out, "replay.log"))
        if a.speed:
            c.post("/api/replay", {"action": "speed", "speed": a.speed})
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            for t in when:
                if t > c.get("/api/replay")["now"] + 0.5:
                    if a.speed:                          # play there at --speed, hold at the moment
                        code, j = c.post("/api/replay", {"action": "play_to", "to": RP.iso(t)})
                    else:                                # fast-forward there, hold
                        code, j = c.post("/api/replay", {"action": "jump", "to": RP.iso(t), "then": "pause"})
                    if code != 200:
                        print("moving the replay failed:", j); continue
                while True:
                    st = c.get("/api/replay")
                    if not st["ff"] and not st.get("stop_at"):
                        break
                    if proc.poll() is not None:
                        sys.exit("replay.py stopped - see " + os.path.join(a.out, "replay.log"))
                    time.sleep(0.1)
                stamp = datetime.datetime.fromtimestamp(t).strftime("%Y%m%d-%H%M%S")
                status = c.get("/status.json")
                with open(os.path.join(a.out, f"{stamp}_status.json"), "w", encoding="utf-8") as fh:
                    json.dump(status, fh)
                f = ((status.get("latest") or {}).get("fields") or {})
                print(f"{RP.iso(t)}  state {f.get('state')} {STATE.get(f.get('state'), '')}  flame {f.get('sensor4')}", flush=True)
                for dev in a.device.split(","):
                    for theme in a.theme.split(","):
                        for lang in a.lang.split(","):
                            ctx = br.new_context(color_scheme=theme, locale="da-DK" if lang == "da" else "en-GB",
                                                 **DEVICES[dev])
                            ctx.add_init_script(f"try{{localStorage.setItem('burner_lang','{lang}');"
                                                f"localStorage.setItem('burner_theme','{theme}');}}catch(e){{}}")
                            for view in a.views.split(","):
                                page_name, _, tab = view.partition(":")
                                pg = ctx.new_page()
                                errs = []
                                pg.on("console", lambda m, e=errs: m.type == "error" and e.append(m.text))
                                pg.on("pageerror", lambda x, e=errs: e.append("pageerror: " + str(x)))
                                q = "?replaybanner=0" if a.no_banner else ""
                                url = f"{c.base}/{page_name}{q}" + (f"#{tab}" if tab else "")
                                pg.goto(url, wait_until="load")
                                try:
                                    pg.wait_for_load_state("networkidle", timeout=8000)
                                except Exception:
                                    pass
                                pg.wait_for_timeout(int(a.settle * 1000))
                                name = f"{stamp}_{page_name}-{tab or 'main'}_{lang}_{theme}_{dev}"
                                png = os.path.join(a.out, name + ".png")
                                pg.screenshot(path=png, full_page=True)
                                text = pg.evaluate("document.body.innerText")
                                with open(os.path.join(a.out, name + ".txt"), "w", encoding="utf-8") as fh:
                                    fh.write(text)
                                index.append({"moment": RP.iso(t), "view": view, "lang": lang, "theme": theme,
                                              "device": dev, "png": png, "txt": png[:-4] + ".txt",
                                              "state": f.get("state"), "state_label": STATE.get(f.get("state")),
                                              "flame": f.get("sensor4"), "console_errors": errs})
                                pg.close()
                            ctx.close()
            br.close()
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()
        with open(os.path.join(a.out, "index.json"), "w", encoding="utf-8") as fh:
            json.dump({"from": RP.iso(t_from), "to": RP.iso(t_to), "work_dir": work, "shots": index}, fh, indent=1)
        with open(os.path.join(a.out, "index.md"), "w", encoding="utf-8") as fh:
            fh.write("| moment | state | view | lang/theme/device | errors | file |\n|---|---|---|---|---|---|\n")
            for s in index:
                fh.write(f"| {s['moment']} | {s['state_label']} | {s['view']} | {s['lang']}/{s['theme']}/{s['device']} "
                         f"| {len(s['console_errors'])} | {os.path.basename(s['png'])} |\n")
    print(f"{len(index)} shots -> {a.out}  (index.json, index.md)")


if __name__ == "__main__":
    main()
