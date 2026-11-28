# Changelog

What changed in each version of the pellet burner monitor. The newest version is at the top.
The version is shown at the bottom of both pages, in Settings → About this monitor and in `/status.json` (`"version"`).
A release is a git tag like `v1.0.0`; the Pi's auto-update only installs release tags.

## 1.0.11

### Better
- "Did the change help?" needs 8 starts on each side and a clear difference before it says better or worse, so it is rarely fooled by chance. If the flame came sooner but more starts failed, it says "mixed". Pellet batch names that differ only in capitals count as one batch.
- "Pellets and the weather" uses the same kg as the rest of the page, counts days without burning, says "roughly" while it is young, and only takes over the "Buy before" date after 21 learned days and inside the temperatures it has seen.
- One rule for a real lighting everywhere (no relights, no starts that went on burning).
- Burner health gives the same verdict on the page, in Home Assistant and in the phone alert; it says "Still learning" instead of "All good" while rows are learning.
- "Picture for service" uses the right factory values for your software.
- Discoveries use the same state names and the same pellet numbers as the rest of the page.

### Fixed
- Danish dates no longer end with two full stops.
- Phone: "Flame and fan by power" shows hours; the Today chart labels no longer run into the "now" line; the /classic tabs wrap instead of hiding.
- "1 emptying(s)" reads "1 emptying".
- Removed the unused "pump start" setting and other leftovers.

## 1.0.10

### New
- "I cleaned the burner" asks what you cleaned: burner, herd and ash / fan and chamber / flame sensor glass. Only those parts learn again.

### Better
- Phone: a start or stop that is more than 5 minutes old (for example after the internet was down) is not acted on; you get "Too old: send it again". Repeats act once.
- Phone alerts: alarms and other important messages are kept while the internet is down and sent when it is back, with the time they happened. The daily summary is marked done only when it was sent.
- "Monitor updated" has its own slot, so a restart message can no longer swallow it.
- The automatic update installs only at night; after a restart it only tidies up an interrupted update.
- Home Assistant: the old auto-start schedule switch is gone; "last lighting time" is the time to flame of real lightings; the start counter is no longer seen as a meter reset after a power cut.
- Raspberry Pi: the status says "hotspot" when the Pi is on its own setup Wi-Fi; the Wi-Fi watchdog sees the scan failures seen on a real Pi; records after a power cut are no longer lost.
- Docs: what you need (USB-RS-485 adapter, cut cable to J4), the install order (IO BOKS on after the page opens), the IO BOKS menu path, unzipping on Windows, when pelletpi.local does not open, getting help, cleaning, and a Danish "Start her".

## 1.0.9

### Better
- Pellets & ash: pellets left, then Pellets per day (full width), then the ash bin, then Pellets and the weather.
- History: Burner health, then Lighting time per start, then Starts per day (full width), ..., Did the change help? just above Recent events.
- Burner health is only on History now; its "I cleaned the burner" button is there too.
- All cards on /home have the same width, padding and spacing; long titles no longer squeeze on a phone.

### Fixed
- Two timing races in a Wi-Fi setup test (no change on the Pi).

## 1.0.8

### New
- "Did the change help?" (History): every cold start as a dot, a line where a setting, the pellet batch or a cleaning changed, and a plain verdict for time to flame, full heat, failed starts, flame strength and short burns: better / worse / the same / too few to say.
- "Pellets and the weather" (Pellets & ash): learns your house's line (kg a day against the outside temperature). Until it has 14 good days it says "Learning". Then: "At 0 °C about X kg a day" and the coming week in kg from the weather forecast. kg only.
- Phone alerts: after an update the message says "Monitor updated: now on v1.0.8 (was v1.0.7)", or that the update did not work and it went back.

### Better
- Discoveries: the misleading "How fast the boiler warms at each power" chart is gone.

## 1.0.7

### Better
- README: a short Start here (what you need, how to install, where the help is). README and guide describe Check / Install and the nightly switch.
- Every release has pelletpi-card.zip attached (made by GitHub itself).

### Fixed
- A test that sometimes failed on GitHub (Wi-Fi setup); no change on the Pi.

## 1.0.6

### Better
- New guide pictures (made from the demo), including Burner health and the update card.
- /classic Overview: the temperature control line says PID with P, I and D and the factory values.
- /home: a little more room under the big boiler number.
- The Pi tests run with all the other tests.

## 1.0.5

### Fixed
- GitHub's own test machine installs the picture library too; no changes on the Pi.

## 1.0.4

### New
- History tab: Burner health is the hub. Time to flame, Flame sensor and the new Fire rescues row open in place: closed when all is fine, open by themselves when something needs a look.
- Time to flame shows the last 10 starts (who started it, time to flame, time to full heat, off before), a line where a boiler setting changed between them, and the lighting settings compared with their typical time to flame. The "Trouble lighting?" help is inside it.
- Fire rescues: when each one happened and whether the boiler relit it.
- Phone alerts: the reply to "status" includes a picture of today's and tomorrow's graph (07:00-22:00, at most once an hour), with Danish letters.

### Better
- The flame sensor check learns from short burns (ready after about 2-3 days) and from the saved history, instead of needing 6 h of steady burning.
- Settings → About this monitor shows the port the browser uses.
- /classic: the old start windows and three cards that the new page already has are gone.

### Fixed
- "Off (h)" before a start is known again after a restart of the monitor.
- A start that went on burning without lighting is no longer counted as a start in the averages.
- GitHub's own test no longer fails on a new version number.

## 1.0.3

### New
- Wi-Fi watchdog on the Pi: if the Wi-Fi is gone for 3 minutes it reconnects by itself, restarts the Wi-Fi if needed, and opens the setup Wi-Fi after about 10 minutes. The Pi never gives up on a known Wi-Fi and Wi-Fi power saving is off.
- Switch in the update card: Automatic updates at night on/off.

### Better
- Check for update only looks; Install X installs. Nothing is installed until you press Install.
- The phone alert about lost internet says when the Wi-Fi was lost and came back.

## 1.0.2

### Fixed
- Update card: after Go back it says you went back (not that the version failed) and offers Try again.
- The update card follows a button press by itself; no need to reload the page.

## 1.0.1

### Better
- A test of the automatic update: no changes you can see, only the version number.

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
