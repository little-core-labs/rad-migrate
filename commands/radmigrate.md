---
description: Migrate a YouTube channel to Rad TV
argument-hint: "@channel [--limit N] [--landscape-only]"
---

Migrate the YouTube channel `$ARGUMENTS` to Rad TV using the `radmigrate` skill.

Before anything else, run `radmigrate doctor $ARGUMENTS` and report what it finds. If it
reports a quality cap, follow the escalation order in the skill — proof-of-origin token
first, browser cookies only after that — and explain why, rather than lowering the
quality bar.

If the environment checks out, show the user `radmigrate plan $ARGUMENTS` and confirm
the scope with them before starting a run that could take hours.
