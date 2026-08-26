# radmigrate

**Move your YouTube channel to Rad TV in one command.**

```bash
export RAD_API_KEY=rad_...
uvx radmigrate run https://youtube.com/@yourchannel
```

That's it. radmigrate downloads your channel in HD and publishes every video to Rad TV
with its title, description, thumbnail, and original release date — then builds a
playlist in the original upload order.

```
radmigrate 0.1.0
  ✓ node 22.22 · ffmpeg 8.1 · yt-dlp 2026.08 · Rad auth ok
  ✓ quality probe  1080p available (no cookies)
  → channel: Your Channel  ·  47 videos
  → 12 already on Rad · 3 vertical, skipped · 32 to migrate
  [ 7/32] Inside Barolo Grill    uploading 412.8 MB
```

Stop it any time. Run it again and it picks up exactly where it left off.

> **Built for creators moving content they own.** See [Ownership](#ownership).

---

## Install

Needs **Python 3.10+**, **Node 20+** (22 recommended), and **ffmpeg**.

```bash
uvx radmigrate --help          # no install
uv tool install radmigrate     # or install it
pipx install radmigrate        # or via pipx
```

Node is not optional. YouTube requires a JavaScript runtime to hand over HD streams,
and without it yt-dlp quietly falls back to low quality rather than failing. ffmpeg
merges YouTube's separate video and audio streams.

```bash
# macOS
brew install node ffmpeg
# Windows
winget install OpenJS.NodeJS.LTS Gyan.FFmpeg
# Debian/Ubuntu
sudo apt install nodejs ffmpeg
```

Check everything at once:

```bash
radmigrate doctor
```

---

## Do you need cookies? Probably not.

Most tools in this space open by telling you to export a `cookies.txt`. radmigrate
doesn't, because usually it isn't necessary — and when it is, cookies are the *last*
resort rather than the first.

Ask before you commit hours to a run:

```bash
radmigrate doctor @yourchannel
# ✓ quality probe   1080p available (no cookies)
```

That probes one real video and reports the resolution YouTube will actually serve. If
it says 1080p, you are done — no account, no setup, nothing to configure.

If it reports a cap, radmigrate tells you the one command that fixes it, in this order:

**1. A proof-of-origin token — no Google account involved.**

```bash
radmigrate setup-potoken
```

YouTube increasingly wants a "proof of origin" token, now bound to each individual
video id, so tokens can't be extracted by hand any more. This installs a provider that
mints them: one self-contained binary, verified against the SHA-256 checksum GitHub
publishes for the pinned release. No Node, no npm, no Docker, no sign-in. It shows you
exactly what it will download and asks before doing it.

**2. Your browser session — only if that isn't enough.**

```bash
radmigrate run @yourchannel --cookies-from-browser firefox
```

Reads cookies straight from your browser; no manual export. **Prefer Firefox.** Since
Chrome 127, Windows binds the cookie encryption key to the Chrome process, so no
external tool can read Chrome, Edge, or Brave cookies there. radmigrate detects this
and says so instead of failing with a decryption error.

> Worth knowing: this is the *risky* rung, not the easy one. Bulk-downloading against
> your own signed-in session is the pattern YouTube's abuse systems look for — and for
> a creator migrating their own channel, that session belongs to the account the
> channel lives on. The token route above touches no account at all.

**3. A cookie file** — for Docker and headless machines, where browser extraction can't
work:

```bash
radmigrate run @yourchannel --cookies-file cookies.txt
```

---

## Usage

```bash
radmigrate run @yourchannel                    # migrate everything
radmigrate plan @yourchannel                   # show what would happen, change nothing
radmigrate doctor @yourchannel                 # check the machine and the quality
radmigrate status                              # progress from previous runs
radmigrate retry @yourchannel                  # retry only what failed
```

Any of `@handle`, `handle`, a full URL, or a `UC…` channel id works.

### Choosing what moves

```bash
radmigrate run @yourchannel \
  --newest 50 \                # the 50 most recent
  --since 2024-01-01 \         # nothing older
  --landscape-only \           # skip vertical videos (Shorts)
  --min-duration 60 \          # skip anything under a minute
  --playlist "My Channel"      # build a playlist in upload order
```

### Useful flags

| Flag | What it does |
|---|---|
| `--channel` | Destination Rad channel, by id or name |
| `--create-channel` | Create the Rad channel if it doesn't exist |
| `--limit N` | Cap this run at N videos — good for a first test |
| `--quality` | `hd-strict` (default), `hd-preferred`, or `best` |
| `--min-height` | Quality floor, default 720 |
| `--allow-sd` | Accept standard definition instead of failing |
| `--cleanup-after-publish` | Delete local files once Rad confirms processing |
| `--workdir` | Where downloads and resume state live |
| `--json` | Machine-readable output |

### Quality is verified, not assumed

Under the default `hd-strict`, a video that can't be had in HD is **failed rather than
saved in standard definition** — and every finished download is probed to confirm what
actually landed on disk, not what was requested. A failure is never recorded as done,
so the next run retries it on a healthier session. That's usually all it takes.

Pass `--allow-sd` if you'd rather have the video at any resolution.

---

## Resuming, and running it twice

Safe to interrupt, safe to re-run. State advances one video at a time and is written
immediately, so a crash or a Ctrl-C loses at most the video in flight.

Re-running is idempotent because **radmigrate asks Rad what's already on the channel**
rather than trusting a local file. So a second run on a different machine, with no
local state at all, still won't publish the same video twice.

Local state lives in `<workdir>/state.db` and is a cache, not the source of truth.
Delete it and the next run re-scans; it still won't double-publish.

Two honest limits on that, both from the Rad API as it stands today:

- **Matching is by title and release date**, not by a stored source id — the API has no
  field to record where a video came from. That's a strong key in practice (the release
  date is derived deterministically from the YouTube upload date) but it isn't exact.
- **Only the 100 most recent items can be listed**, which is the cap on the one
  available listing endpoint. Past that, radmigrate warns you rather than silently
  under-reporting.

## Known limits

| Limit | Why |
|---|---|
| No delete | Rad's API exposes `unpublish` but nothing that removes content or playlists. radmigrate can hide a video, not erase it. |
| Dedupe past 100 items | The content listing endpoint caps at 100. |
| No provenance field | Nowhere to stamp the YouTube id, so dedupe falls back to title + date. |
| Subtitles aren't uploaded | They're downloaded and kept locally; Rad has no caption endpoint. |

---

## Use it from an AI agent

### MCP server

Works with Claude Desktop, OpenClaw, Hermes, and anything else that speaks MCP:

```json
{
  "mcpServers": {
    "radmigrate": {
      "command": "uvx",
      "args": ["radmigrate", "mcp"],
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
/plugin marketplace add little-core-labs/radmigrate
/plugin install radmigrate
```

Then `/radmigrate @yourchannel`.

### Scripting

Every command takes `--json` for a machine-readable result on stdout, and streams
NDJSON progress events on stderr:

```bash
radmigrate plan @yourchannel --json | jq '.to_migrate'
```

---

## Docker

```bash
docker run --rm \
  -e RAD_API_KEY \
  -v "$PWD/work:/work" \
  ghcr.io/little-core-labs/radmigrate run @yourchannel --workdir /work
```

The image ships Node, ffmpeg, and yt-dlp. Browser cookie extraction can't work in a
container, so mount a `cookies.txt` and use `--cookies-file` if you need it.

---

## Configuration

Flags beat environment variables beat `.env` beat defaults. Copy
[`.env.example`](.env.example) to `.env` to get started.

| Variable | Purpose |
|---|---|
| `RAD_API_KEY` | Required. Your Rad TV API key. |
| `RADMIGRATE_CHANNEL` | Destination channel |
| `RADMIGRATE_WORKDIR` | Downloads and state, default `./radmigrate-work` |
| `RADMIGRATE_COOKIES_FROM_BROWSER` | `firefox`, `chrome`, … |
| `RADMIGRATE_COOKIES_FILE` | Path to a `cookies.txt` |

Your API key is never written to state, logs, or `--json` output. Everything radmigrate
prints passes through a redaction filter first, and there's a test asserting it.

---

## Troubleshooting

**Everything downloads at 360p.** Run `radmigrate doctor @yourchannel`. Almost always a
missing JS runtime (install Node 22+) or a degraded session (`radmigrate setup-potoken`).

**"Sign in to confirm you're not a bot".** YouTube is throttling. radmigrate backs off
automatically and reduces its API calls per video. If it persists, wait — the throttle
is per-session and lifts.

**Chrome cookies won't read on Windows.** They can't be, by design, since Chrome 127.
Use Firefox or `--cookies-file`.

**A transcode failed on Rad.** The upload worked; the conversion didn't. Retry with
`radmigrate retry @yourchannel`. Videos are only reported as done once Rad confirms
processing, so a failure here is visible rather than silent.

**It's slow.** Deliberately. YouTube throttles the extraction API per session, so
radmigrate paces itself to survive a whole channel rather than sprinting into a block.

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

```bash
git clone https://github.com/little-core-labs/radmigrate && cd radmigrate
uv sync --dev
uv run pytest
uv run ruff check . && uv run mypy src
```

Sources and destinations sit behind protocols in
[`source/base.py`](src/radmigrate/source/base.py) and
[`dest/base.py`](src/radmigrate/dest/base.py) — new ones are welcome.

## License

MIT
