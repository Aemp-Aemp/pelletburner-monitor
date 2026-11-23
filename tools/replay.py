#!/usr/bin/env python3
"""Replay a recorded day through the REAL monitor program (same web pages and APIs), for agents and checks.

    py tools\\replay.py --data <data-folder> --from "2000-01-01 06:00" --to "2000-01-02 05:30" --speed 60 --port 8790

What it does
  * copies the data folder to a work folder (never writes into --data) and cuts every file at --from,
    so the program starts with exactly the history that existed at that moment;
  * runs burner_responder.main() in this process with a virtual clock: time.time(), datetime.now(),
    date.today() ... follow the replay time; --speed 1 = real time, 60 = 1 h per minute;
  * feeds the recorded raw-*.jsonl rows (every value, every second) as status_info frames into the module
    emulator, in time order, exactly when the replay clock reaches them - the program computes states,
    transitions, history, plan, health etc. itself, as it did live;
  * the web page's own clock (Date.now / new Date) follows the replay time (a script injected only in replay);
  * NO serial port is opened (pyserial is replaced by a stub that refuses), nothing is transmitted, no MQTT,
    start/stop buttons are inert and labelled "replay".

Control (JSON, 127.0.0.1 only)
  GET  /api/replay                      -> {"now", "now_iso", "speed", "paused", "ff", "ended", ...}
  POST /api/replay {"action":"pause"} | {"action":"play"} | {"action":"speed","speed":120}
                   {"action":"jump","to":"2000-01-01 07:00","then":"pause"|"play"}   (forward only)
                   {"action":"step","seconds":60}                                     (forward, then pause)
  (POST needs headers X-Burner: 1 and Content-Type: application/json, like every POST of the program.)
  The work folder also holds replay_state.json (same as GET /api/replay), rewritten every second.
"""
import argparse, datetime, glob, json, os, re, shutil, sys, tempfile, threading, time, types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
if ROOT not in sys.path:
    sys.path.append(ROOT)
from burner_common import parse_jsonl_line  # noqa: E402  (tolerant of NUL bytes / cut lines after a power cut)


def _clean_line(line):
    """A .jsonl line as it can be copied: NUL bytes and a cut start removed; None for a line with no record."""
    if "\x00" in line:
        line = line.strip().strip("\x00").rsplit("\x00", 1)[-1]
    line = line.strip()
    return line + "\n" if line and parse_jsonl_line(line) is not None else None

_real_time = time.time
_real_mono = time.monotonic
_real_localtime, _real_gmtime, _real_ctime, _real_strftime = time.localtime, time.gmtime, time.ctime, time.strftime
_RealDT, _RealDate = datetime.datetime, datetime.date


# ============================================================================ time parsing
def parse_when(s, base=None):
    """'2000-01-01 06:00[:SS]' / '2000-01-01T06:00' / epoch number -> epoch (local time of this PC).
    'HH:MM[:SS]' alone = that time on the day of `base` (epoch); earlier than base -> the next day."""
    s = str(s).strip()
    if re.fullmatch(r"\d{9,11}(\.\d+)?", s):
        return float(s)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m and base is not None:
        b = _RealDT.fromtimestamp(base)
        d = b.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3) or 0), microsecond=0)
        if d.timestamp() < base - 1:
            d += datetime.timedelta(days=1)
        return d.timestamp()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return _RealDT.strptime(s, fmt).timestamp()
        except ValueError:
            pass
    raise ValueError(f"cannot read the time {s!r} (use 'YYYY-MM-DD HH:MM' or 'HH:MM')")


def iso(epoch):
    return _RealDT.fromtimestamp(epoch).isoformat(timespec="seconds") if epoch is not None else None


# ============================================================================ the virtual clock
class Clock:
    """Replay time. free() runs at `speed` from an anchor; now() = min(free(), barrier). The feeder holds the
    barrier at the epoch of the row it just delivered (until the next read), so every row is processed with
    time.time() == its recorded epoch, at any speed. Fast-forward (jump) = free() jumps to the target and the
    rows flow as fast as the program can take them."""

    def __init__(self, start, speed=1.0, paused=False):
        self.lock = threading.Lock()
        self.v0, self.r0, self.speed, self.paused = float(start), _real_mono(), float(speed), paused
        self.barrier = float(start)
        self.ff_target, self.ff_then = None, "pause"
        self.stop_at = None                      # play_to: run at speed, then hold here

    def free(self):
        if self.ff_target is not None:
            return self.ff_target
        if self.paused:
            return self.v0
        v = self.v0 + (_real_mono() - self.r0) * self.speed
        s = self.stop_at
        return s if s is not None and v >= s else v

    def now(self):
        f = self.free()
        b = self.barrier
        return f if f < b else b

    def _reanchor(self, v):
        self.v0, self.r0 = v, _real_mono()

    def pause(self):
        with self.lock:
            self._reanchor(self.now()); self.paused = True

    def play(self):
        with self.lock:
            self._reanchor(self.now()); self.paused = False

    def set_speed(self, sp):
        with self.lock:
            self._reanchor(self.now()); self.speed = float(sp)

    def jump(self, target, then="pause"):
        with self.lock:
            self.ff_then = then
            self.ff_target = float(target)

    def play_to(self, target):
        with self.lock:
            self._reanchor(self.now()); self.stop_at = float(target); self.paused = False

    def stop_reached(self):
        with self.lock:
            s = self.stop_at
            if s is None:
                return
            self.stop_at = None
            self._reanchor(s)
            self.paused = True

    def ff_done(self):
        with self.lock:
            t = self.ff_target
            if t is None:
                return
            self.ff_target = None
            self._reanchor(t)
            self.paused = self.ff_then != "play"


# ============================================================================ copy of the data folder
_DAY = re.compile(r"(\d{4}-\d{2}-\d{2})")
_STAMP = re.compile(r"prealarm-(\d{8}-\d{6})\.json$")


def _cut_jsonl(src, dst, t_cut):
    n_in = n_out = 0
    with open(src, encoding="utf-8", errors="replace") as fi, open(dst, "w", encoding="utf-8") as fo:
        for line in fi:
            n_in += 1
            if "\x00" in line or not line.rstrip().endswith("}"):  # power cut: NUL bytes, a half line
                line = _clean_line(line)
                if line is None:
                    continue
            m = re.search(r'"epoch"\s*:\s*([0-9.]+)', line)      # fast path: no full JSON parse
            if m and float(m.group(1)) > t_cut:
                continue
            fo.write(line if line.endswith("\n") else line + "\n")
            n_out += 1
    return n_in, n_out


def _trim_future(o, t_cut):
    """settings.json: drop learned list entries (dicts with an epoch) that were learned after the cut."""
    if isinstance(o, dict):
        return {k: _trim_future(v, t_cut) for k, v in o.items()}
    if isinstance(o, list):
        return [_trim_future(v, t_cut) for v in o
                if not (isinstance(v, dict) and isinstance(v.get("epoch"), (int, float)) and v["epoch"] > t_cut)]
    return o


def prepare_copy(src, work, t_cut, log=print, keep_setup=False):
    """Copy src -> work, cut at t_cut: no line, file, snapshot or day summary from after the cut."""
    src, work = os.path.abspath(src), os.path.abspath(work)
    if os.path.normcase(work) == os.path.normcase(src) or os.path.normcase(work).startswith(os.path.normcase(src) + os.sep):
        raise SystemExit("refusing: the work folder must not be the data folder or inside it")
    os.makedirs(work, exist_ok=True)
    cut_day = _RealDT.fromtimestamp(t_cut).date().isoformat()
    kept = []
    for p in sorted(glob.glob(os.path.join(src, "*"))):
        b = os.path.basename(p)
        if not os.path.isfile(p) or b in ("responder.log",) or b.startswith("replay_state"):
            continue
        d = os.path.join(work, b)
        m = _DAY.search(b)
        day = m.group(1) if m else None
        if b.endswith(".jsonl"):
            if day and day > cut_day:
                continue
            _cut_jsonl(p, d, t_cut)
        elif b.startswith("summary-"):
            if day and day >= cut_day:                     # a day summary is written when the day is over
                continue
            shutil.copy2(p, d)
        elif _STAMP.search(b):
            if _RealDT.strptime(_STAMP.search(b).group(1), "%Y%m%d-%H%M%S").timestamp() > t_cut:
                continue
            shutil.copy2(p, d)
        elif b == "prealarm-last.json":
            continue                                       # rebuilt below from the snapshots that remain
        elif b == "settings.json":
            with open(p, encoding="utf-8") as fh:
                s = _trim_future(json.load(fh), t_cut)
            mm = s.get("minmode")
            if isinstance(mm, dict):                       # minimum-mode burn-out / "I turned it off" from after the cut
                s["minmode"] = {k: (v if not (isinstance(v, (int, float)) and v > t_cut) else None) for k, v in mm.items()}
            ws = s.get("warmstate")
            if isinstance(ws, dict) and not (isinstance(ws.get("at"), (int, float)) and ws["at"] <= t_cut):
                s.pop("warmstate")                         # a heating-plan window served after the cut
            if keep_setup is False and not (s.get("setup") or {}).get("done"):
                # recorded before the first-start guide existed (or skipped there): a user of this monitor never
                # saw the guide, so the replay does not show it either (--show-setup keeps it)
                s["setup"] = dict(s.get("setup") or {}, done=True, at=round(t_cut))
            with open(d, "w", encoding="utf-8") as fh:
                json.dump(s, fh, indent=1)
        elif b == "weather_days.json":
            with open(p, encoding="utf-8") as fh:
                wd = json.load(fh)
            with open(d, "w", encoding="utf-8") as fh:
                json.dump({k: v for k, v in wd.items() if k < cut_day}, fh)
        elif b == "weather_cache.json":
            continue                                       # the replay weather builds its own (see ReplayWeather)
        else:
            shutil.copy2(p, d)
        kept.append(b)
    snaps = sorted(glob.glob(os.path.join(work, "prealarm-????????-??????.json")))
    if snaps:
        shutil.copy2(snaps[-1], os.path.join(work, "prealarm-last.json"))
    log(f"# replay: data copied to {work} ({len(kept)} files), cut at {iso(t_cut)}")
    return work


# ============================================================================ recorded rows -> frames
def iter_raw_rows(src, t_from, t_to):
    """Recorded rows (raw-YYYY-MM-DD.jsonl: every value every second) with t_from <= epoch <= t_to, in order."""
    d0 = _RealDT.fromtimestamp(t_from).date() - datetime.timedelta(days=1)
    d1 = _RealDT.fromtimestamp(t_to).date()
    files = [p for p in sorted(glob.glob(os.path.join(src, "raw-????-??-??.jsonl")))
             if d0.isoformat() <= _DAY.search(os.path.basename(p)).group(1) <= d1.isoformat()]
    last = None
    for p in files:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    r = parse_jsonl_line(line)
                    e = float(r["epoch"])
                except (ValueError, KeyError, TypeError):
                    continue
                if e < t_from or (last is not None and e <= last):
                    continue
                if e > t_to:
                    return
                last = e
                yield r


def row_to_status(row, codes):
    """A recorded row back into the board's status_info dict {"000": state, ...}."""
    nd = {}
    for k, v in row.items():
        c = codes.get(k)
        if c is None and k.startswith("f_") and re.fullmatch(r"\d{3}", k[2:]):
            c = k[2:]
        if c is not None and isinstance(v, (int, float)) and not isinstance(v, bool):
            nd[c] = v
    return dict(sorted(nd.items()))


class SerialStub(types.ModuleType):
    """Stands in for pyserial in replay: opening any port is refused."""

    class SerialException(OSError):
        pass

    def __init__(self):
        super().__init__("serial")

        def _refuse(*a, **k):
            raise RuntimeError("replay: opening a serial port is not allowed")
        self.Serial = _refuse
        self.__version__ = "replay-stub"


class Feeder:
    """Looks like the serial port to burner_responder.main(): read() returns the board's status_info frames
    from the recording when the replay clock reaches them; write() refuses (main never writes without --live)."""

    def __init__(self, src, work, t_from, t_to, clock, codes):
        from burner_sniff import build_frame
        self.build_frame = build_frame
        self.src, self.work, self.t_from, self.t_to, self.clock, self.codes = src, work, t_from, t_to, clock, codes
        self.rows = iter_raw_rows(src, t_from, t_to)
        self.next = next(self.rows, None)
        self.cfg_last = None
        self.fed, self.last_row, self.ended = 0, None, False
        self.side = SideFiles(src, work, t_from)

    def _frame(self, obj):
        txt = json.dumps(obj, separators=(",", ":"))
        return self.build_frame(0x01, 3, 0x20, 4, txt.encode() + b"\x04")

    def read(self, n=256):
        ck = self.clock
        self.side.pump(ck.now())
        nxt = self.next
        if ck.stop_at is not None and ck.free() >= ck.stop_at and (nxt is None or nxt["epoch"] > ck.stop_at):
            ck.barrier = ck.stop_at if nxt is None else nxt["epoch"]
            ck.stop_reached()
        if ck.ff_target is not None and (nxt is None or nxt["epoch"] > ck.ff_target):
            ck.barrier = ck.ff_target if nxt is None else nxt["epoch"]
            ck.ff_done()
        if nxt is None:
            ck.barrier = self.t_to
            if ck.free() >= self.t_to and not self.ended:
                self.ended = True
                ck.pause()
            time.sleep(0.02)
            return b""
        ck.barrier = nxt["epoch"]
        if ck.free() < nxt["epoch"]:
            time.sleep(0.02 if ck.paused else 0.003)
            return b""
        self.next = next(self.rows, None)
        out = b""
        cfg = {k: nxt[f] for k, f in (("000", "setpoint"), ("001", "fuel_gph")) if isinstance(nxt.get(f), (int, float))}
        if cfg and cfg != self.cfg_last:
            self.cfg_last = cfg
            out += self._frame({"cfg_data_1": cfg})
        out += self._frame({"status_info": row_to_status(nxt, self.codes)})
        self.fed += 1
        self.last_row = nxt["epoch"]
        return out

    def write(self, b):
        raise RuntimeError("replay: nothing is ever sent")

    def flush(self):
        pass

    def close(self):
        pass


class SideFiles:
    """Files written by OTHER programs while the monitor ran (pi-health-*.jsonl from pi/pi_health.py):
    appended to the work copy as the replay clock passes their lines."""

    def __init__(self, src, work, t_from):
        self.work, self.items, self.i, self.last, self.cut = work, [], 0, 0.0, set()
        for p in sorted(glob.glob(os.path.join(src, "pi-health-????-??-??.jsonl"))):
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = _clean_line(line)
                    m = line and re.search(r'"epoch"\s*:\s*([0-9.]+)', line)
                    if m and float(m.group(1)) > t_from:
                        self.items.append((float(m.group(1)), os.path.basename(p), line))
        self.items.sort()

    def pump(self, now):
        if now - self.last < 5 and self.i:
            return
        self.last = now
        while self.i < len(self.items) and self.items[self.i][0] <= now:
            e, name, line = self.items[self.i]
            self.i += 1
            try:                                   # a full disk loses this line; it must not stop the replay
                with open(os.path.join(self.work, name), "a", encoding="utf-8") as fh:
                    fh.write(("\n" if name in self.cut else "") + line)    # pi_health.py ends a cut line too
                self.cut.discard(name)
            except OSError:
                self.cut.add(name)


# ============================================================================ outside temperature from the recording
def install_weather(F, src):
    """Weather in replay: the recorded readings (weather.jsonl) and the recorded hourly curve
    (weather_cache.json) instead of asking Open-Meteo - never the internet, never today's weather."""
    readings, hourly = [], {}
    try:
        with open(os.path.join(src, "weather.jsonl"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = parse_jsonl_line(line)
                    readings.append((float(o["epoch"]), o.get("c")))
                except (ValueError, KeyError, TypeError):
                    pass
    except OSError:
        pass
    readings.sort()
    for e, c in readings:                                  # coarse hourly curve from the readings ...
        if c is not None:
            hourly[int(e // 3600 * 3600)] = c
    try:
        with open(os.path.join(src, "weather_cache.json"), encoding="utf-8") as fh:
            for t_, c_ in (json.load(fh).get("hourly") or []):   # ... the recorded one wins where it exists
                hourly[int(t_)] = c_
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    curve = sorted(hourly.items())

    def fetch(self, lat, lon):
        now = time.time()
        past = [r for r in readings if r[0] <= now]
        if not past and not curve:
            raise ValueError("replay: no recorded weather")
        at, c = past[-1] if past else (now, None)
        self.cache = {"at": at, "c": c, "hourly": [[t_, c_] for t_, c_ in curve if now - 2 * 86400 <= t_ <= now + 2 * 86400],
                      "lat": lat, "lon": lon}
        if self.cache_path:
            with open(self.cache_path, "w", encoding="utf-8") as fh:
                json.dump(self.cache, fh)

    def _get(*a, **k):
        raise OSError("replay: no internet requests")
    F.Weather.fetch = fetch
    F.Weather._get = staticmethod(_get)


# ============================================================================ page clock (injected only in replay)
PAGE_JS = r"""<script>/* REPLAY ONLY (tools/replay.py): the page clock follows the replay time; start/stop are inert */
(function(){
  var RD=Date, R=window.__REPLAY__=%STATUS%;
  function base(st){ R.v=st.now*1000; R.w=RD.now(); R.k=(st.paused||st.ended)?0:(st.ff?0:st.speed); R.st=st; }
  base(R);
  function vnow(){ return R.v+(RD.now()-R.w)*R.k; }
  function D(){
    if(!new.target) return new RD(vnow()).toString();
    var a=[].slice.call(arguments);
    return a.length? new (Function.prototype.bind.apply(RD,[null].concat(a)))() : new RD(vnow());
  }
  D.prototype=RD.prototype; D.now=function(){ return Math.round(vnow()); }; D.parse=RD.parse; D.UTC=RD.UTC;
  window.Date=D;
  var rf=window.fetch.bind(window);
  window.fetch=function(u,o){
    if(String(u).indexOf("api/run_mode")>=0)
      return Promise.resolve(new Response(JSON.stringify({ok:false,msg:"replay - nothing is sent"}),{status:403,headers:{"Content-Type":"application/json"}}));
    return rf(u,o);
  };
  function sync(){ rf("/api/replay",{cache:"no-store"}).then(function(r){return r.json();}).then(function(st){ base(st); badge(); }).catch(function(){}); }
  setInterval(sync,1000);
  var hide=/[?&]replaybanner=0/.test(location.search);
  function two(n){ return (n<10?"0":"")+n; }
  function badge(){
    if(hide||!document.body) return;
    var b=document.getElementById("replayBadge");
    if(!b){ b=document.createElement("div"); b.id="replayBadge";
      b.style.cssText="position:fixed;left:6px;bottom:6px;z-index:2147483647;pointer-events:none;font:600 11px/1.3 system-ui,sans-serif;"+
        "background:rgba(160,40,160,.88);color:#fff;padding:3px 8px;border-radius:9px;letter-spacing:.02em";
      document.body.appendChild(b); }
    var d=new RD(vnow()), st=R.st||{};
    b.textContent="REPLAY "+two(d.getDate())+"/"+two(d.getMonth()+1)+" "+two(d.getHours())+":"+two(d.getMinutes())+":"+two(d.getSeconds())+
      (st.ff?"  fast-forward":st.ended?"  end":st.paused?"  paused":"  x"+st.speed);
  }
  function mark(){
    var bs=document.querySelectorAll('button[data-act="start"],button[data-act="stop"],button[data-act="reset"]');
    for(var i=0;i<bs.length;i++){ var x=bs[i]; if(x.dataset.replay) continue; x.dataset.replay="1"; x.disabled=true;
      x.title="replay - nothing is sent"; x.textContent=x.textContent+" (replay)"; }
  }
  document.addEventListener("DOMContentLoaded",function(){ badge(); mark();
    new MutationObserver(mark).observe(document.body,{childList:true,subtree:true}); setInterval(badge,250); });
})();
</script>"""


class Replay:
    """The object burner_responder.REPLAY points to while a replay runs."""

    def __init__(self, a, clock, feeder, work, t_from, t_to):
        self.a, self.clock, self.feeder, self.work, self.t_from, self.t_to = a, clock, feeder, work, t_from, t_to
        self.insights_path = os.path.join(work, "insights.json")
        self.mod = None

    def attach(self, mod):
        self.mod = mod

        def queue_command(cmd, source):
            # what the live program would queue for the next keep-alive; in replay nothing is ever sent.
            # Marked as delivered at once so the state change that follows in the recording gets its cause.
            now = time.time()
            with mod.cmd_lock:
                mod.last_cmd = {"cmd": cmd, "source": source, "queued": now, "delivered": now,
                                "state_at_queue": (mod.latest or {}).get("fields", {}).get("state"), "replay": True}
            mod.note_event({"type": "command_sent", "epoch": now, "cmd": cmd, "source": source,
                            "replay": "not sent - replay"})
        mod.queue_command = queue_command
        mod.take_command = lambda: None

    def status(self):
        ck, fd = self.clock, self.feeder
        now = ck.now()
        return {"replay": True, "now": now, "now_iso": iso(now), "from": iso(self.t_from), "to": iso(self.t_to),
                "speed": ck.speed, "paused": ck.paused, "ff": ck.ff_target is not None,
                "ff_target": iso(ck.ff_target) if ck.ff_target is not None else None,
                "stop_at": iso(ck.stop_at) if ck.stop_at is not None else None,
                "ended": fd.ended, "rows_fed": fd.fed, "last_row": iso(fd.last_row),
                "next_row": iso(fd.next["epoch"]) if fd.next else None,
                "work_dir": self.work, "source": os.path.abspath(self.a.data), "port": self.a.port}

    def control(self, body):
        act = str(body.get("action", "")).lower()
        ck = self.clock
        try:
            if act == "pause":
                ck.pause()
            elif act == "play":
                if self.feeder.ended:
                    return 409, {"ok": False, "msg": "replay is at its end (--to)", **self.status()}
                ck.play()
            elif act == "speed":
                sp = float(body.get("speed"))
                if not 0 < sp <= 100000:
                    raise ValueError("speed must be > 0")
                ck.set_speed(sp)
            elif act in ("jump", "step", "play_to"):
                if ck.ff_target is not None:
                    return 409, {"ok": False, "msg": "a jump is still running", **self.status()}
                now = ck.now()
                if act == "step":
                    target = now + float(body.get("seconds", 60))
                else:
                    target = parse_when(body.get("to"), base=self.t_from)
                if target < now - 0.5:
                    return 409, {"ok": False, "msg": "only forward: start the replay again with --at for an earlier time",
                                 **self.status()}
                if target > self.t_to + 0.5 and act != "step":
                    return 400, {"ok": False, "msg": f"after the end of the replay ({iso(self.t_to)})"}
                target = min(target, self.t_to)
                if act == "play_to":                     # run at the set speed, hold at the target
                    ck.play_to(target)
                    return 200, {"ok": True, **self.status()}
                ck.jump(target, "pause" if act == "step" else str(body.get("then", "pause")))
            else:
                return 400, {"ok": False, "msg": "action: pause | play | speed | jump | step | play_to"}
        except (TypeError, ValueError) as e:
            return 400, {"ok": False, "msg": str(e)}
        return 200, {"ok": True, **self.status()}

    def inject_html(self, page):
        js = PAGE_JS.replace("%STATUS%", json.dumps(self.status())).encode("utf-8")
        i = page.find(b"<head>")
        if i < 0:
            return js + page
        return page[:i + 6] + js + page[i + 6:]


# ============================================================================ patches (replay process only)
def install_clock(clock):
    time.time = clock.now
    time.localtime = lambda secs=None: _real_localtime(clock.now() if secs is None else secs)
    time.gmtime = lambda secs=None: _real_gmtime(clock.now() if secs is None else secs)
    time.ctime = lambda secs=None: _real_ctime(clock.now() if secs is None else secs)
    time.strftime = lambda fmt, t=None: _real_strftime(fmt, _real_localtime(clock.now()) if t is None else t)

    class VDateTime(_RealDT):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(clock.now(), tz)

        @classmethod
        def today(cls):
            return cls.fromtimestamp(clock.now())

        @classmethod
        def utcnow(cls):
            return cls.utcfromtimestamp(clock.now())

    class VDate(_RealDate):
        @classmethod
        def today(cls):
            return cls.fromtimestamp(clock.now())
    datetime.datetime, datetime.date = VDateTime, VDate


def build_parser():
    ap = argparse.ArgumentParser(description="Replay a recorded period through the real monitor program.")
    ap.add_argument("--data", required=True, help="recorded data folder (read only; a copy is used)")
    ap.add_argument("--from", dest="t_from", required=True, help="start, e.g. '2000-01-01 06:00'")
    ap.add_argument("--to", dest="t_to", required=True, help="end, e.g. '2000-01-02 05:30' (or 'HH:MM')")
    ap.add_argument("--speed", type=float, default=60, help="1 = real time, 60 = one hour per minute (default 60)")
    ap.add_argument("--port", type=int, default=8790, help="web port on 127.0.0.1 (default 8790)")
    ap.add_argument("--at", default="", help="fast-forward to this time at start and hold (paused) there")
    ap.add_argument("--pause", action="store_true", help="start paused (POST /api/replay {action:play} to run)")
    ap.add_argument("--work", default="", help="work folder for the copy (default: a new temp folder)")
    ap.add_argument("--show-setup", action="store_true",
                    help="keep the first-start guide if the recorded settings never finished it (default: hidden)")
    ap.add_argument("--exit-at-end", action="store_true", help="stop the program when --to is reached")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    t_from = parse_when(a.t_from)
    t_to = parse_when(a.t_to, base=t_from)
    if t_to <= t_from:
        sys.exit("--to must be after --from")
    if not os.path.isdir(a.data):
        sys.exit(f"--data folder not found: {a.data}")
    work = a.work or tempfile.mkdtemp(prefix="pb-replay-" + _RealDT.fromtimestamp(t_from).strftime("%Y%m%d-%H%M") + "-")
    prepare_copy(a.data, work, t_from, keep_setup=a.show_setup)
    if not glob.glob(os.path.join(a.data, "raw-????-??-??.jsonl")):
        sys.exit("no raw-YYYY-MM-DD.jsonl in --data: nothing to replay")

    # ---- the program sees: a pyserial that refuses, a virtual clock, no BURNER_* settings (no MQTT etc.)
    sys.modules["serial"] = SerialStub()
    for k in [k for k in os.environ if k.startswith("BURNER_")]:
        os.environ.pop(k)
    clock = Clock(t_from, a.speed, paused=a.pause)
    install_clock(clock)
    sys.path.insert(0, ROOT)
    import burner_responder as R
    import burner_features as F
    install_weather(F, a.data)
    feeder = Feeder(a.data, work, t_from, t_to, clock, {v: k for k, v in R.STATUS_NAMES.items()})
    rp = Replay(a, clock, feeder, work, t_from, t_to)
    R.REPLAY = rp
    R.open_serial = lambda args, log, state={}: feeder

    # Discoveries tab: built once from the cut copy (the live program does it hourly into scada/)
    def _insights():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("insights_gen_replay", os.path.join(ROOT, "scada", "insights_gen.py"))
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            m.DATA, m.OUT = work, rp.insights_path
            m.main()
        except Exception as e:
            print(f"# replay: insights not built ({e!r})", flush=True)
    _insights()          # before the program starts: it writes ignitions.jsonl, which the start-list card reads

    if a.at:
        t_at = parse_when(a.at, base=t_from)
        if not t_from <= t_at <= t_to:
            sys.exit("--at must lie between --from and --to")
        clock.jump(t_at, "pause")

    sys.argv = ["burner_responder.py", "--fw-mode", "--live-interval", "1", "--allow-remote-start",
                "--port", "REPLAY", "--serve", str(a.port), "--serve-host", "127.0.0.1", "--allowed-hosts", "",
                "--data-dir", work, "--keep-days", "3650", "--log", os.path.join(work, "responder.log"),
                "--remote-cmd", os.path.join(work, "remote_cmd.txt"), "--no-log-frames", "--mqtt", ""]
    threading.Thread(target=R.main, daemon=True, name="responder").start()

    state_path = os.path.join(work, "replay_state.json")
    print(f"# replay: http://127.0.0.1:{a.port}/  ({iso(t_from)} -> {iso(t_to)}, speed {a.speed:g}"
          + (f", jump to {a.at}" if a.at else "") + (", paused" if a.pause else "") + ")", flush=True)
    print(f"# replay: state in {state_path} and GET /api/replay; Ctrl+C stops", flush=True)
    try:
        while True:
            try:                                 # written first, then every second: the file is there from the start
                st = rp.status()
                tmp = state_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(st, fh)
                os.replace(tmp, state_path)
            except OSError:
                pass
            if a.exit_at_end and feeder.ended:
                print("# replay: end reached", flush=True)
                break
            _real_sleep(1.0)
    except KeyboardInterrupt:
        pass
    os._exit(0)


_real_sleep = time.sleep

if __name__ == "__main__":
    main()
