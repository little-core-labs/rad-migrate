---
name: radmigrate
description: Migrate a YouTube channel to Rad TV — download in HD and publish with metadata, thumbnails, and an ordered playlist. Use when someone wants to move, copy, back up, or republish a YouTube channel to Rad TV, or asks about radmigrate, HD download quality caps, YouTube bot checks, PO tokens, or failed Rad transcodes.
---

# radmigrate

Moves a YouTube channel to Rad TV. Your job is to drive the CLI and interpret what it
reports — the tool handles the mechanics.

## The short version

```bash
radmigrate doctor @channel     # is this machine capable, and what quality will we get?
radmigrate plan @channel       # what would move
radmigrate run @channel        # do it
```

Always `doctor` before a real run. A migration takes hours, and the failure it catches
in five seconds — a missing JS runtime, a degraded session capping everything at 360p —
is otherwise discovered one wasted file at a time.

## Operational judgement

These are the things the CLI can't decide for the user.

### Never treat a quality cap as something to work around by lowering the bar

If `doctor` reports a resolution below the minimum, the fix is to raise the quality, not
to pass `--allow-sd`. Suggest `--allow-sd` only if the user explicitly says they'd
rather have the video at any resolution — republishing a whole channel in 360p is
usually worse than waiting.

Escalate in this order, and stop as soon as quality is sufficient:

1. **Nothing.** Often 1080p works with no auth at all. `doctor` tells you.
2. **`radmigrate setup-potoken`.** One verified binary, no Google account. Prefer this.
3. **`--cookies-from-browser firefox`.** Only if the token route isn't enough.

Never suggest cookies before the token route. Bulk-downloading against a signed-in
session risks the account — and for a creator migrating their own channel, that's the
account the channel lives on. Say so if you recommend it.

On Windows, do not suggest `--cookies-from-browser chrome`/`edge`/`brave`. Since Chrome
127 the cookie key is bound to the browser process and no external tool can read it.
Firefox works.

### Start small on an unfamiliar channel

```bash
radmigrate run @channel --limit 1
```

One full round trip proves the credentials, the channel resolution, the upload path, and
the transcode before committing hours. Then run without `--limit`.

### Interpret the outcome honestly

- **`published` ≠ `verified`.** Published means Rad accepted the bytes; verified means
  the transcode finished. Report the verified count when the user asks what worked.
- **`failed_processing`** is Rad's side, not the user's. Retry it; don't re-download.
- **`failed_quality`** means a download came in under the bar and was discarded on
  purpose. It will be retried on a later, healthier session. This is working as
  designed — don't present it as data loss.
- **"not selected"** means `--newest`/`--limit` didn't reach those videos. That is not
  the same as **"filtered"**, which means a content rule rejected them. Don't conflate
  the two when summarising.

### Interruptions are safe

Ctrl-C, a crash, and a killed process all resume cleanly. Never suggest starting over or
deleting state to "clean up" — re-running the same command is the recovery path. Nothing
gets published twice; radmigrate asks Rad what's already there rather than trusting
local files.

### Long runs

A full channel takes hours. Don't sit in a foreground loop waiting. Either let the user
run it themselves, or use the MCP server's `start_migration`, which returns a job id
immediately, and poll `migration_status`.

## Selection flags

| Want | Flag |
|---|---|
| Skip Shorts and vertical video | `--landscape-only` |
| Recent only | `--newest 50` or `--since 2024-01-01` |
| Skip clips and stings | `--min-duration 60` |
| Preserve channel order on Rad | `--playlist "Channel Name"` |
| Reclaim disk as it goes | `--cleanup-after-publish` |

## When scripting or parsing output

Use `--json`. Results go to stdout, NDJSON progress events to stderr. Don't scrape the
human-readable console output.

## Ownership

This is for creators moving their own work. If someone asks to migrate a channel that
isn't theirs, say plainly that the tool is for content you own and stop there. For
originals with no bot-detection involved at all, Google Takeout is the sanctioned path.

## More detail

See `references/troubleshooting.md` for specific errors and their causes.
