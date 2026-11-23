# Replay: play the web page through a recorded day

`tools/replay.py` runs the **real** monitor program (`burner_responder.main()`, same `/home`, `/classic`, APIs) on
127.0.0.1 and feeds it the recorded status values (`raw-YYYY-MM-DD.jsonl`, every value every second) in time order, as
if the boiler were live. The program recomputes everything itself: states, lighting/flame, burning, rests, alarms,
transitions and events, history, charts, plan, burner health.

```
py tools\replay.py --data <data-folder> --from "2000-01-01 06:00" --to "2000-01-02 05:30" --speed 60 --port 8790
```
Open http://127.0.0.1:8790/home (or /classic). `--speed 1` = real time, `60` = 1 h per minute.

Options: `--at "07:00"` fast-forward there at start and hold (paused) · `--pause` start paused · `--work DIR` work folder
(default: new temp folder) · `--exit-at-end` · `--show-setup` (see below). Times: `"YYYY-MM-DD HH:MM[:SS]"` or `"HH:MM"`
(on the --from day; earlier than --from = next day). Local time of this PC (times are the monitor's local time).

## Control and current replay time
- `GET /api/replay` -> `{"now", "now_iso", "speed", "paused", "ff", "ended", "rows_fed", "work_dir", ...}`
- the same JSON in `<work>/replay_state.json`, rewritten every second
- `POST /api/replay` (headers `X-Burner: 1`, `Content-Type: application/json`), body one of:
  `{"action":"pause"}`, `{"action":"play"}`, `{"action":"speed","speed":120}`,
  `{"action":"jump","to":"18:00","then":"pause"|"play"}` (fast-forward), `{"action":"step","seconds":60}`,
  `{"action":"play_to","to":"18:00"}` (play at the set speed, hold there). Forward only: for an earlier
  time start the replay again (with `--at`).
- `/status.json` `now` is the replay time. The pages' clock (`Date.now()`, `new Date()`) follows the replay time via a
  script injected **only in replay**; a small purple "REPLAY dd/mm hh:mm:ss" badge shows it (`?replaybanner=0` hides it).

## Screenshots of many moments (for web-check and data agents)
```
<python with playwright> tools\replay_snapshots.py --data <data-folder> ^
  --from "2000-01-01 06:00" --to "2000-01-01 12:00" --at "06:10,06:20,07:00" ^
  --views home:now,home:history --lang en,da --theme light,dark --device desktop,phone --out C:\temp\shots
```
Moments: `--at` list, `--every 30m`, `--state-changes` (each state change, `--after 30` s later). `--speed 120` plays
between moments instead of fast-forwarding. Views `page:tab`: home: now pellets history plan settings; classic: live seq
opt sch disc. Output per shot: `.png` (full page) + `.txt` (visible text); per moment `<stamp>_status.json`;
`index.json` (state, flame, console errors per shot) and `index.md`.

## Safety
No serial port is opened (pyserial is replaced by a stub that refuses), the fake port refuses writes, the program runs
without `--live`, `--mqtt`, `--cfg-sync`, all `BURNER_*` environment settings are ignored, web only on 127.0.0.1.
Start/stop buttons are disabled and labelled "(replay)"; `POST /api/run_mode` answers 403. Plan/warm-mode starts the
program decides are logged as `command_sent` with `"replay": "not sent - replay"`. Weather comes from the recording
(`weather.jsonl`, `weather_cache.json`), never the internet.

## The data copy
`--data` is never written. The work copy is cut at `--from`: no jsonl line, day file, pre-alarm snapshot, day summary,
weather day or learned settings entry from after that moment. `pi-health-*` lines (written by another program on the Pi)
are appended to the copy as the replay clock passes them. Settings are the recorded folder's (final) settings minus
later-learned entries, so a plan changed later in the period is replayed with the later plan. Settings recorded before
the first-start guide existed get `setup.done` so the guide does not cover the page (`--show-setup` keeps it).
The Discoveries tab is built once from the cut copy. Settings changed on the page during a replay go to the copy only.

## Test
`py scada\_replay_test.py` (no browser; synthetic day, includes a short end-to-end replay).
`py scada\run_tests.py` runs every test and prints a summary (use a Python with Playwright for the browser tests).

## API golden snapshots
`py tools\replay_golden.py --save` stores the program's JSON answers (status, settings, history, ...) at one replay
moment in `tools/golden/` (synthetic data); `py tools\replay_golden.py` compares and lists every difference. With
`--data <data-folder> --from .. --to .. --at .. --dir <folder outside the repo>` it does the same on a recording.
