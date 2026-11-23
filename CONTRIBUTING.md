# Contributing

This is a hobby project. Bug reports, boiler data and small fixes are all welcome.

## Report a bug

Open an issue with the **Bug report** template and tell us:

- **Controller software version:** on the boiler, MENU → FLERE → INFO (for example V1.40.1).
- **Monitor version:** at the bottom of the page (shown as "Version ...", for example 1.0.0).
- What you did, what you expected, what happened. A screenshot of the page helps.
- **Logs:** `docker compose logs --tail 200`, and if it is about a start or an alarm, that day's
  `data/events-YYYY-MM-DD.jsonl`.

**Before you attach anything,** remove what is private: passwords (`.env`), the ntfy topic, your home
addresses and Wi-Fi name, and your town if you set one.

## Share boiler data

Data from other boilers and software versions helps most. Use the **Boiler data share** template. Nothing is
needed beyond the files in `data/`; the template says which ones and what to remove first.

## Change the code

- Keep it simple: plain words on the pages, English and Danish both.
- Never add anything that bypasses the boiler's safety functions or resets alarms without the user asking.
- Run the tests before a pull request (see "Tests" in the README). New Python code gets a test.
- No brand names and no personal data in code, tests or pictures; use made-up values.

By sending a change you agree it is shared under the MIT license (see `LICENSE`).
