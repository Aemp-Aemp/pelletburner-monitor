# Changelog

What changed in each version of the pellet burner monitor. The newest version is at the top.
The version is shown at the bottom of both pages, in Settings → About this monitor and in `/status.json` (`"version"`).
A release is a git tag like `v1.0.0`; the Pi's auto-update only installs release tags.

## 1.0.0 (the first release)

Everything in 0.9.0, plus:

### New

- Start and stop from the phone (optional, off by default): send "start" or "stop" in the ntfy app, then reply
  with a one-time code and your PIN. Refused during an alarm; 3 wrong tries lock it for 15 minutes.
- Phone alert when the Wi-Fi was weak for 30 minutes or the internet was down, and a notice on the home page.
- Heating plan tab: "How the plan works out" - was the house warm in time, and pellets used per period.
- Warm by: the boiler starts earlier by its own learned lighting and warm-up time (at most 90 minutes extra).
- Boiler settings history: every change of a boiler setting is logged (also one made while the monitor was off);
  /classic Analysis shows the last 10 in plain words.
- History: short burns (under 30 minutes) are shown as the light part of each bar in "Starts per day".
- The home page shows a short notice while the monitor cannot save data (full SD card).
- One-command install on the Raspberry Pi: `sudo sh pi/install.sh`.
- `AI.md`: a short install guide for your own AI assistant, with the safety rules it must follow.
- Tests run on GitHub on every push and pull request (`run_tests.py --no-browser` skips the browser tests).
- "Trouble lighting?" card (/home History, /classic Analysis): how the last 10 starts went, the boiler's lighting
  settings next to the factory values, and what to try, one thing at a time. "Copy lighting report" gives a short
  text to paste where you ask for help. Read only: the monitor changes no boiler setting.
- Update from the page (Raspberry Pi): a **Check for update** button in Settings → About this monitor checks,
  downloads and installs the newest release and shows each step, in English and Danish.
- **Go back to the previous version** for 14 days after an update (only while the boiler is off or waiting).
- Guide: what to do before and after upgrading the boiler's software (1.13 → 1.40 or newer).

### Better

- Safer updates: the new version must start, answer and reach the boiler again, or the Pi goes back by itself and
  the page says why. The updater never installs a pre-release or an older version and tidies up after a power cut.
  A Pi with the first updater needs `sudo sh pi/install-auto-update.sh` once.
- New SD cards point at the project's public repository, so they get updates out of the box.

- Much less Wi-Fi traffic: an open page uses about 10 kB a minute instead of about 100 kB a second. On slow
  Wi-Fi the pages no longer pile up waiting requests.
- No data on the page: it says "This device has no network" when the phone or PC itself is offline, instead of
  blaming the monitor.
- Daily phone summary covers the last 24 hours before it is sent (across midnight) and counts alarms.
- After the heating plan's own low-power stop, the headline says "Resting until 09:00" / "Hviler til 09:00"
  instead of "Paused by the page".
- Settings are a list of small pages and save at once. Number fields take plain digits only (also on /classic).
- The SD card is spared: settings are only written when something changed.
- Danish texts everywhere on the home page, including flame, hot-water, auto-start and alarm reset messages;
  plainer English and Danish wording overall, Danish decimal comma.
- Heating plan: the minimum boiler temperature shows "off" / "fra" instead of "0 °C".
- Burner health, Discoveries and the lighting-time card all use the same usual time to flame. The fan line says
  how many % slower the fan is.
- Fan relearn button: "Fan replaced or fan settings changed", so it is clear it is also for changed fan settings.
- After changing the boiler type, the usual lighting times start over (the page says so).
- Discoveries are kept over a restart.
- Charts use round axis steps (fan rpm in 1000 steps); short labels on the classic flame-and-fan chart on phones.
- Guide: the fan screen on controller 1.40, the hidden service menu with its default values, a warning that
  changing the boiler type resets other settings, and a short soot tip.
- Phone alerts: changing to a new topic is explained better and the old topic gets a last message.
- Log lines carry a local date and time; a full disk logs one line per outage, not one per write.
- More safety tests: a check-only boiler setting is proven never to be written, and the whole suite can run at
  any clock time.

### Fixed

- A setting that cannot be saved (full or read-only SD card) is no longer shown as saved; the page says so.
  Same for the pellet count and a new pellet batch.
- Full SD card: a write cut off half-way no longer spoils the next records.
- Bad values in settings (too big, not a number, wrong type) get a clear error instead of breaking the settings
  file.
- /classic "Pellet use" (and the kW from it) was about twice too high; it now agrees with the silo counter.
- /classic: the Power tile shows "–" while the burner is stopped, waiting or in alarm; the Analysis cards switch
  language at once; the state diagram says "stop" and "resting until …" like /home.
- A restart during a heating period no longer starts the boiler again when that period was already done.
- Warm mode no longer counts the same cool-down twice after a restart, which spoiled the warm-by start time.
- Phone commands work even when the Pi clock is off, a message is never acted on twice, and codes typed in other
  digit styles are accepted.
- Data files survive a power cut while writing; settings keep a backup copy that is used if the file is broken.
- Outside temperature just below zero shows "0.0°", not "-0.0°". Pellets: "Burned since" shows "0 kg", not
  "- 0 kg", before the first burn.
- Pellet batches whose names differ only in capitals or spaces count as one batch.
- Opening the page under a host name that is not allowed shows a plain message on what to do.
- On Windows, a web port already in use now gives a clear message instead of two programs sharing it.
- Burner health "Time to flame": no longer says "Still learning" when the usual time is already known, and it
  judges the last 3 starts that lit, not only the newest (one slow start showed a green tick).
- /classic Analysis: "Started by" is filled in when the page is opened straight on the Analysis tab.
- /classic Settings: the heating plan line is now Danish on the Danish page. Step by step no longer points at a
  highlighted card that is not there while running.
- /home day chart on a phone: crowded start times no longer lose their labels.

## 0.9.0 (never released; the state before 1.0.0)

The monitor as it was when the project went public:

- Two web pages: the home page (Now, Pellets, History, Heating plan, Settings) and the classic page with charts.
- Burner health checklist, lighting time per start, pellets per day, ash plate turns, alarms with what came before.
- Who or what started and stopped the boiler.
- Heating plan that can start and stop the burner (only when remote start is on).
- "Share my burner" and "Picture for service" pictures for a post or for help.
- Settings on the page and from `.env`; first-start guide.
- Home Assistant over MQTT, phone alerts (ntfy), weather.
- Raspberry Pi: Docker, ready SD card folder, one-command install (`sudo sh pi/install.sh`), safe auto-update
  (only while the burner is off or waiting).
- Try it without a boiler: made-up data and the replay tool.
- English and Danish texts. MIT license.
