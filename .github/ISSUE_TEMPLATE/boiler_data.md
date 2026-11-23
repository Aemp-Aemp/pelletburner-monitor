---
name: Boiler data share
about: Share data from your boiler to help understand other boilers and software versions
labels: data
---

**Boiler**
- Size (10 / 15 / 25 kW):
- Controller software (MENU → FLERE → INFO):
- Anything special (no flue sensor, changed start settings, ...):

**Files** (zip them and attach): from the monitor's `data/` folder, a few days of
`hist-*.jsonl`, `events-*.jsonl` and `ignitions.jsonl`. `raw-*.jsonl` only if asked (they are large).

**Remove first:**
- do not attach `settings.json`, `.env`, `responder.log` or `pi-health-*` (they can hold passwords, the ntfy
  topic, Wi-Fi names and addresses)
- do not write your town, name or address in the issue

What do you want to know, or what did you notice?
