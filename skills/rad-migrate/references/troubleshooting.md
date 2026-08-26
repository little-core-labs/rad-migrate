# Troubleshooting

Specific failures, what actually causes them, and what to do.

## Quality

### Everything comes down at 360p

Two causes, in order of likelihood.

**No JavaScript runtime.** YouTube's HD stream URLs are behind a signature/n challenge
that has to be solved in JS. Without node, yt-dlp doesn't fail — it silently serves what
it can, which is low quality. `rad-migrate doctor` reports node's version; install 22+.

**A degraded session.** YouTube throttles per session and per IP. Once flagged, HD
formats stop being offered. Run `rad-migrate setup-potoken` — a proof-of-origin token
provider fixes this without any account. Failing that, wait; the throttle lifts.

Do not reach for `--allow-sd` to make the error go away. It doesn't fix quality; it
lowers the bar and publishes a 360p channel.

### "Got 480p but need at least 720p — discarded, will retry"

Working as designed. The file was verified after download, found short of the bar, and
deleted so it isn't published at the wrong resolution. It was never recorded as done, so
the next run picks it up. Usually resolves on its own once the session recovers.

### Quality probe says "could not probe"

The channel's first video may be private, members-only, or region-locked. Not
necessarily a problem for the migration — try `rad-migrate plan` and see whether
discovery works.

## YouTube auth and throttling

### "Sign in to confirm you're not a bot"

YouTube's bot check. rad-migrate backs off exponentially and collapses to a single player
client, which cuts extraction API calls per video by roughly 4x and lets a session
survive far more videos. Let it run.

If it persists across runs:

1. `rad-migrate setup-potoken`
2. `--cookies-from-browser firefox`
3. Wait a few hours. Session throttles expire.

### Chrome cookies fail to decrypt on Windows

Not fixable from outside Chrome. Since Chrome 127, Windows app-bound encryption ties the
cookie key to the Chrome process itself; copying the database yields ciphertext. Edge,
Brave, and other Chromium forks inherit this.

Firefox stores cookies in plain SQLite and works. Or export a `cookies.txt` and use
`--cookies-file`.

### Chrome cookie database is locked

Chrome holds an exclusive lock while running. Close it completely — including background
tasks from the tray — and retry. On Windows this is usually moot; see above.

## Rad TV

### Transcode failed (`failed_processing`)

The upload succeeded and Rad's conversion didn't. Retry:

```bash
rad-migrate retry @channel
```

This re-uploads onto the existing content record rather than re-downloading. If the same
video fails repeatedly with the job stuck at 0% progress and an epoch-zero end time, the
job was created but never picked up by a worker — that is a Rad-side issue and needs the
content id and job id reported, not another retry.

### "Key not found in database" / RESOURCE_NOT_FOUND on upload

The ingestion target went missing between creating the content record and uploading to
it. Treated as transient and retried automatically. If it persists, Rad's backend is
having trouble; wait rather than hammering it — each attempt can leave an empty content
record behind.

### Empty content records on the channel

A publish call that returns a truncated response may still have created a record. This
is why rad-migrate does not retry the create step — retrying multiplies orphans. Clean
them up in the Rad web app.

### "Your account has more than one channel"

rad-migrate won't guess which one. Pass `--channel` with the id or name, or
`--create-channel` to make a new one named after the source channel.

### Could not list existing Rad content

Dedupe falls back to local state, so re-runs on *this* machine are still safe, but a run
from a fresh checkout could republish. The warning is accurate — it needs a
list-channel-content API tool to be fully reliable.

## Local

### Out of disk

A channel can be tens of gigabytes. Use `--cleanup-after-publish` to delete each video
once Rad confirms it processed, or point `--workdir` at a bigger drive. Resume state
travels with the workdir, so keep them together.

### ffmpeg not found

Needed to merge YouTube's separate video and audio streams.

- macOS: `brew install ffmpeg`
- Windows: `winget install Gyan.FFmpeg`
- Debian/Ubuntu: `sudo apt install ffmpeg`

Or pass `--ffmpeg-location` pointing at the binary.

### ffprobe not found

Downloads still work, but the quality guard is skipped — nothing verifies that a file
really is HD. Install the full ffmpeg package rather than a video-only build.

### A run was interrupted

Re-run the same command. State is written after every transition, so at most the video
in flight is lost. Do not delete the state database to "start clean" — that only forces
a re-scan, and Rad-side dedupe still prevents double-publishing.

## Reading the state

```bash
rad-migrate status
```

| State | Meaning |
|---|---|
| `discovered` | Known, not yet downloaded |
| `downloaded` | On disk, not yet published |
| `published` | Rad accepted the upload; transcoding |
| `verified` | Done — processed and playable |
| `already_present` | Was on Rad before this run |
| `skipped` | Excluded by a filter |
| `failed_quality` | Below the HD bar, discarded, will retry |
| `failed_download` | Download failed |
| `failed_publish` | Upload to Rad failed |
| `failed_processing` | Rad's transcode failed |

Only `verified` means the video is actually playable on Rad.
