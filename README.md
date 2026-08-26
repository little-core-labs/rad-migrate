# rad-migrate

**Move your YouTube channel to Rad TV in one command.**

```bash
export RAD_API_KEY=rad_...
rad-migrate @yourchannel
```

That's the whole thing. rad-migrate downloads your channel in HD and publishes every
video to Rad TV with its title, description, thumbnail, and original release date, then
builds a playlist in the original upload order.

```
rad-migrate 0.1.0
  ✓ node 22.22 · ffmpeg 8.1 · yt-dlp 2026.08 · Rad auth ok
  ✓ quality probe  1080p available (no cookies)
  → channel: Your Channel  ·  47 videos
  → 12 already on Rad · 3 vertical, skipped · 32 to migrate
  [ 7/32] Inside Barolo Grill    uploading 412.8 MB
```

Stop it any time. Run it again and it picks up exactly where it left off.

> **Built for creators moving content they own.** See [Ownership](#ownership).

---

## Contents

- [Install](#install) · [Your first run](#your-first-run) · [Do you need cookies?](#do-you-need-cookies-probably-not)
- [Commands](#commands) · [Choosing what moves](#choosing-what-moves) · [What you end up with](#what-you-end-up-with)
- [Resuming](#resuming-and-running-it-twice) · [Known limits](#known-limits)
- [Use it from an AI agent](#use-it-from-an-ai-agent) · [Docker](#docker) · [Configuration](#configuration)
- [Troubleshooting](#troubleshooting) · [Ownership](#ownership)

---

## Install

There is nothing to build. rad-migrate is a Python package — install it and run it.

```bash
# Not yet on PyPI. For now, install from source:
uv tool install git+https://github.com/little-core-labs/rad-migrate

# or with pipx
pipx install git+https://github.com/little-core-labs/rad-migrate
```

<details>
<summary>Once published to PyPI</summary>

```bash
uvx rad-migrate @yourchannel     # run without installing
uv tool install rad-migrate      # or install it
pipx install rad-migrate
```

</details>

### Two things pip cannot install for you

rad-migrate needs **Node 20+** (22 recommended) and **ffmpeg** on your PATH.

```bash
# macOS
brew install node ffmpeg
# Windows
winget install OpenJS.NodeJS.LTS Gyan.FFmpeg
# Debian/Ubuntu
sudo apt install nodejs ffmpeg
```

Node is not optional and not a build tool — YouTube requires a JavaScript runtime to
hand over HD stream URLs, and **without it downloads silently drop to low quality
rather than failing**. ffmpeg merges YouTube's separate video and audio streams.

If you would rather not install any of this, [use the Docker image](#docker) — it ships
the whole toolchain.

---

## Your first run

Four steps. The first three cost seconds and save hours.

**1. Check the machine.**

```bash
rad-migrate doctor
```

Verifies Node, ffmpeg, yt-dlp, disk space, and your Rad credentials.

**2. Check what quality you'll actually get.**

```bash
rad-migrate doctor @yourchannel
# ✓ quality probe   1080p available (no cookies)
```

This probes one real video. It is the single most useful thing you can do before a long
run — a degraded YouTube session caps everything at 360p, and this catches it in five
seconds instead of one wasted file at a time.

**3. See what would move.**

```bash
rad-migrate plan @yourchannel
```

Downloads and publishes nothing. Shows the videos, what's already on Rad, and what your
filters excluded.

**4. Migrate one video first, then the rest.**

```bash
rad-migrate @yourchannel --limit 1     # one full round trip
rad-migrate @yourchannel               # everything
```

The `--limit 1` run proves your credentials, the channel, the upload path, and the
transcode before you commit hours to it.

---

## Do you need cookies? Probably not.

Most tools in this space open by telling you to export a `cookies.txt`. rad-migrate
doesn't, because usually it isn't necessary — and when it is, cookies are the *last*
resort rather than the first.

`rad-migrate doctor @yourchannel` tells you. If it reports 1080p, you're done — no
account, no setup, nothing to configure.

If it reports a cap, rad-migrate names the one command that fixes it, in this order:

**1. A proof-of-origin token — no Google account involved.**

```bash
rad-migrate setup-potoken
```

YouTube increasingly wants a "proof of origin" token, now bound to each individual
video id, so tokens can't be extracted by hand any more. This installs a provider that
mints them: one self-contained binary, verified against the SHA-256 checksum GitHub
publishes for the pinned release. No Node, no npm, no Docker, no sign-in. It shows you
exactly what it will download and asks before doing it. Remove it with
`rad-migrate setup-potoken --remove`.

**2. Your browser session — only if that isn't enough.**

```bash
rad-migrate @yourchannel --cookies-from-browser firefox
```

Reads cookies straight from your browser; no manual export. **Prefer Firefox.** Since
Chrome 127, Windows binds the cookie encryption key to the Chrome process, so no
external tool can read Chrome, Edge, or Brave cookies there. rad-migrate detects this
and says so instead of failing with a decryption error.

> Worth knowing: this is the *risky* rung, not the easy one. Bulk-downloading against
> your own signed-in session is the pattern YouTube's abuse systems look for — and for
> a creator migrating their own channel, that session belongs to the account the
> channel lives on. The token route above touches no account at all.

**3. A cookie file** — for Docker and headless machines, where browser extraction can't
work:

```bash
rad-migrate @yourchannel --cookies-file cookies.txt
```

---

## Commands

```bash
rad-migrate @yourchannel          # migrate (shorthand for `rad-migrate run`)
rad-migrate plan @yourchannel     # show what would happen, change nothing
rad-migrate doctor @yourchannel   # check the machine and the quality
rad-migrate status                # progress from previous runs
rad-migrate retry @yourchannel    # retry only what failed
rad-migrate setup-potoken         # install the HD token provider
rad-migrate mcp                   # run as an MCP server for AI agents
```

Any of `@handle`, `handle`, a full URL, or a `UC…` channel id works.

Every command takes `--help`.

## Choosing what moves

```bash
rad-migrate @yourchannel \
  --newest 50 \                # the 50 most recent
  --since 2024-01-01 \         # nothing older
  --landscape-only \           # skip vertical videos (Shorts)
  --min-duration 60 \          # skip anything under a minute
  --playlist "My Channel"      # build a playlist in upload order
```

| Flag | What it does |
|---|---|
| `--channel` | Destination Rad channel, by id or name |
| `--create-channel` | Create the Rad channel if it doesn't exist |
| `--limit N` | Cap this run at N videos — good for a first test |
| `--newest N` | Only the N most recent |
| `--since YYYY-MM-DD` | Nothing uploaded before this date |
| `--landscape-only` | Skip vertical videos |
| `--min-duration` / `--max-duration` | Bounds in seconds |
| `--quality` | `hd-strict` (default), `hd-preferred`, or `best` |
| `--min-height` | Quality floor, default 720 |
| `--allow-sd` | Accept standard definition instead of failing |
| `--playlist TITLE` | Build a playlist from what was migrated |
| `--cleanup` | Delete local files once Rad confirms processing |
| `--workdir` | Where downloads and resume state live |
| `--skip-preflight` | Skip the pre-run checks |
| `--json` | Machine-readable output |

### Quality is verified, not assumed

Under the default `hd-strict`, a video that can't be had in HD is **failed rather than
saved in standard definition** — and every finished download is probed to confirm what
actually landed on disk, not what was requested. A failure is never recorded as done,
so the next run retries it on a healthier session. That's usually all it takes.

Pass `--allow-sd` if you'd rather have the video at any resolution.

---

## What you end up with

**On Rad:** one content item per video, with the original title, description,
thumbnail, and release date, plus a playlist in upload order if you asked for one.
Portrait video is published as portrait. Videos are marked done only once Rad confirms
the transcode finished — not merely that the upload succeeded.

**On disk**, under `--workdir` (default `./rad-migrate-work`):

```
rad-migrate-work/
├── state.db                    resume journal
└── media/
    └── <channel-id>/
        └── 2024-03-15 - Video Title [dQw4w9WgXcQ]/
            ├── video.mp4       the download
            ├── video.info.json full yt-dlp metadata
            ├── video.webp      original thumbnail
            └── thumbnail.jpg   converted for upload
```

Add `--cleanup` to delete each folder once Rad confirms processing — a channel can
easily run to tens of gigabytes.

---

## Resuming, and running it twice

Safe to interrupt, safe to re-run. State advances one video at a time and is written
immediately, so a crash or a Ctrl-C loses at most the video in flight.

Re-running is idempotent because **rad-migrate asks Rad what's already on the channel**
rather than trusting a local file. So a second run on a different machine, with no
local state at all, still won't publish the same video twice.

`state.db` is a cache, not the source of truth. Delete it and the next run re-scans; it
still won't double-publish.

## Known limits

All of these come from the Rad API as it stands today, and are documented in detail —
with proposed fixes — in [docs/rad-api-notes.md](docs/rad-api-notes.md).

| Limit | Why |
|---|---|
| Dedupe matches on title + release date | The API has no field to record a source id, so matching can't be exact |
| Dedupe only covers the 100 most recent items | The content listing endpoint caps at 100; rad-migrate warns rather than silently under-reporting |
| No delete | The API exposes `unpublish` but nothing that removes content or playlists |
| Subtitles aren't uploaded | They're downloaded and kept locally; Rad has no caption endpoint |

---

## Use it from an AI agent

### MCP server

Works with Claude Desktop, OpenClaw, Hermes, and anything else that speaks MCP:

```json
{
  "mcpServers": {
    "rad-migrate": {
      "command": "rad-migrate",
      "args": ["mcp"],
      "env": { "RAD_API_KEY": "rad_..." }
    }
  }
}
```

Tools: `check_environment`, `plan_migration`, `start_migration`, `migration_status`,
`cancel_migration`, `retry_failed`, `install_potoken_provider`.

`start_migration` returns a job id immediately and runs in the background — a channel
can take hours, so no tool call ever blocks. The agent polls `migration_status`, which
reads the same journal the CLI does, so you and the agent watch the same run.

### Claude Code

```bash
/plugin marketplace add little-core-labs/rad-migrate
/plugin install rad-migrate
```

Then `/rad-migrate @yourchannel`.

### Scripting

`--json` puts a single parseable result on stdout and streams NDJSON progress events on
stderr, so the two never interleave:

```bash
rad-migrate plan @yourchannel --json | jq '.to_migrate'
```

---

## Docker

Ships Node, ffmpeg, and yt-dlp, so there is nothing to install.

```bash
docker run --rm \
  -e RAD_API_KEY \
  -v "$PWD/work:/work" \
  ghcr.io/little-core-labs/rad-migrate @yourchannel --workdir /work
```

Browser cookie extraction can't work in a container, so mount a `cookies.txt` and use
`--cookies-file` if you need it.

---

## Configuration

Flags beat environment variables beat `.env` beat defaults. Copy
[`.env.example`](.env.example) to `.env` to get started.

| Variable | Purpose |
|---|---|
| `RAD_API_KEY` | Required. Your Rad TV API key. |
| `RAD_MIGRATE_CHANNEL` | Destination channel |
| `RAD_MIGRATE_WORKDIR` | Downloads and state, default `./rad-migrate-work` |
| `RAD_MIGRATE_COOKIES_FROM_BROWSER` | `firefox`, `chrome`, … |
| `RAD_MIGRATE_COOKIES_FILE` | Path to a `cookies.txt` |

Your API key is never written to state, logs, or `--json` output. Everything rad-migrate
prints passes through a redaction filter first, and there's a test asserting it. See
[SECURITY.md](SECURITY.md).

---

## Troubleshooting

**Everything downloads at 360p.** Run `rad-migrate doctor @yourchannel`. Almost always a
missing JS runtime (install Node 22+) or a degraded session (`rad-migrate setup-potoken`).

**"Sign in to confirm you're not a bot".** YouTube is throttling. rad-migrate backs off
automatically and reduces its API calls per video. If it persists, wait — the throttle
is per-session and lifts.

**Chrome cookies won't read on Windows.** They can't be, by design, since Chrome 127.
Use Firefox or `--cookies-file`.

**A transcode failed on Rad.** The upload worked; the conversion didn't. Retry with
`rad-migrate retry @yourchannel`. Videos are only reported done once Rad confirms
processing, so a failure here is visible rather than silent.

**It's slow.** Deliberately. YouTube throttles the extraction API per session, so
rad-migrate paces itself to survive a whole channel rather than sprinting into a block.

More detail: [skills/rad-migrate/references/troubleshooting.md](skills/rad-migrate/references/troubleshooting.md).

---

## Ownership

This tool exists for creators moving their own work to a platform of their choosing.
Downloading other people's videos to republish them is both a copyright problem and a
YouTube Terms of Service problem, and that isn't what this is for.

If you want your own originals with no bot-detection in the loop at all,
[Google Takeout](https://takeout.google.com/) exports the source files YouTube holds
for your channel. It's slower and more manual, but it's the fully sanctioned path.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
git clone https://github.com/little-core-labs/rad-migrate && cd rad-migrate
uv sync --dev
uv run pytest
```

Sources and destinations sit behind protocols in
[`source/base.py`](src/rad_migrate/source/base.py) and
[`dest/base.py`](src/rad_migrate/dest/base.py) — new ones are welcome.

## License

MIT
