# Rad TV API — integration findings and change requests

Findings from building [radmigrate](../README.md), a YouTube→Rad TV migration tool, and
running a full live migration against production.

**Environment:** `api.rad.live`, server `1.9.0`, MCP protocol `2025-06-18`, 45 tools.
**Verified:** one video migrated end to end — create → upload → transcode → verify →
re-list → unpublish. Account on the `Rad Creators+` plan.

---

## How to use this document

Each issue is written to be **implementable without further context**. Every entry has:

- **Symptom** — what a client observes
- **Reproduce** — literal calls and payloads
- **Expected vs actual**
- **Proposed contract** — the concrete shape being asked for
- **Done when** — acceptance criteria a test can assert

Issue ids (`RAD-n`) are stable; please quote them in commits and PRs.

Calls below are MCP `tools/call` over `POST https://api.rad.live/mcp` with
`Authorization: Bearer <api key>` and
`Accept: application/json, text/event-stream`. Payload shape:

```json
{ "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": { "name": "<tool>", "arguments": { } } }
```

**Backwards compatibility:** every proposal below is additive — new optional request
fields and new response fields. No existing field changes type or disappears, so
current clients keep working. Where that is not true it is called out explicitly.

---

# Bugs

## RAD-1 · `duration` is `0` after a successful transcode

**Severity:** medium · **Surface:** `get_content`, `channel_dashboard`

### Symptom
A content record whose transcode completed successfully reports `duration: 0`.

### Reproduce
1. `publish_video { title, filename, size, channel, publish: false }`
2. POST the file to the returned ingestion endpoint as `multipart/form-data`
3. Poll `get_processing_status { contentId }` until `COMPLETE`
4. `get_content { id: <contentId> }`

Source used: 16-second, 1920×1080, H.264/AAC MP4, 8.1 MB.

### Expected vs actual
```jsonc
// actual
{ "duration": 0, "transcodeJobId": 21638,
  "assets": { "videos": [ { "deployed": true, "mime": "application/vnd.apple.mpegurl" } ] } }

// expected
{ "duration": 16, ... }   // seconds; the transcoder demonstrably knows this
```

The job reached `COMPLETE` at 100% and produced a deployed, playable HLS asset, so
duration was available to the transcoder.

### Impact
Anything keyed on runtime is wrong: player scrubber and UI, resume/continue-watching,
playlist total length, recommendation weighting, and watch-time-fraction analytics.
Also removes duration as a sanity check that the intended file was ingested.

### Likely cause
Duration is probed during transcode but never written back to the content record on
job completion. Check the job-completion handler that writes `assets.videos` — it
should be writing duration in the same transaction.

### Proposed contract
`duration` is an integer count of **seconds**, populated on transcode completion.
Prefer the container duration from the probe (`ffprobe format.duration`), rounded to
the nearest second. If a source genuinely has no duration (still image, zero-length),
use `null` rather than `0`, so "unknown" is distinguishable from "instantaneous".

### Done when
- A newly transcoded video reports a non-zero `duration` matching the source within ±1s
- `channel_dashboard` returns the same value as `get_content`
- Existing records are backfilled from their completed jobs, or a backfill task exists
- Regression test: publish a fixture of known length, assert duration after `COMPLETE`

---

## RAD-2 · `create_playlist_with_items` reports items it does not attach

**Severity:** medium · **Surface:** `create_playlist_with_items`, `channel_dashboard`

### Symptom
The create call reports `itemCount: 1`; the same playlist immediately reads back empty.

### Reproduce
```jsonc
// call
{ "name": "create_playlist_with_items",
  "arguments": { "title": "test", "summary": "…",
                 "contentIds": ["did:rad.live:content/feature/<uuid>"],
                 "channel": "did:rad.live:channel/<uuid>" } }

// response
{ "playlist": { "id": "did:rad.live:content/playlist/<uuid>",
                "metadata": { "title": "test", … } },
  "itemCount": 1 }

// then: channel_dashboard { "contentLimit": 1 }
{ "playlists": [ { "id": "did:rad.live:playlist/<uuid>",
                   "title": "test", "itemCount": 0 } ] }
```

Reproduced twice with two separate playlists, against a content item in `ready` state
on the same channel.

### Expected vs actual
Expected `itemCount: 1` from both endpoints, and the item retrievable from the
playlist. Actual: `1` on create, `0` on read.

### Impact
Either items are not attached at all, or `itemCount` is computed from different sources
on the two paths. Either way the create response cannot be trusted, and
playlist-building — the natural way to preserve a migrated channel's ordering — silently
produces empty playlists.

### Investigate first
Determine which of the two is true, because the fixes differ:
1. **Items are attached; the dashboard count is wrong.** Then `channel_dashboard`'s
   count query is the bug.
2. **Items are not attached.** Then `create_playlist_with_items` is reporting the
   requested count rather than the committed one.

A likely contributing factor worth checking either way: the content was `protected` /
unreleased (created with `publish: false`) at attach time. If unpublished content is
filtered out of playlist membership or its count, that behaviour should be documented —
and arguably should not apply to the owner's own view.

### Proposed contract
- `itemCount` in every response reflects **committed** membership, read back after write
- Add a read path for playlist membership — see RAD-3
- If attaching a non-public item is intentionally disallowed, the create call should
  say so per item rather than reporting success:
  ```jsonc
  { "playlist": {…}, "itemCount": 0,
    "rejected": [ { "id": "did:…", "reason": "content is not published" } ] }
  ```

### Done when
- Create and read report the same `itemCount` for the same playlist
- Attached items are retrievable via the read path
- Items that cannot be attached are reported in `rejected`, not silently dropped
- Regression test covers attaching both published and unpublished content

---

## RAD-3 · Playlist DIDs are inconsistent between endpoints

**Severity:** medium · **Surface:** `create_playlist_with_items`, `channel_dashboard`,
`update_playlist`, `get_content`

### Symptom
The same playlist has two identifier forms, and no read-one endpoint accepts either.

| Endpoint | Form returned/expected |
|---|---|
| `create_playlist_with_items` response | `did:rad.live:content/playlist/<uuid>` |
| `channel_dashboard` listing | `did:rad.live:playlist/<uuid>` |
| `update_playlist` (per schema) | `did:rad.live:content/playlist/<uuid>` |
| `get_content` | rejects both — `No content found for '<playlist did>'` |

### Impact — note the failure mode
This is the most damaging of the inconsistencies because of *how* it fails. A client
that recognises one form gets no id back from create — **while the playlist is still
created**. That reads as "the call failed, nothing happened", so the client retries, and
every retry creates another orphan.

That is exactly what happened here: two orphaned playlists from what looked like a
single failed call. Combined with RAD-5 (no delete), the orphans are permanent.

### Proposed contract
1. **One canonical form.** Pick `did:rad.live:content/playlist/<uuid>` (consistent with
   `content/feature/`) and return it everywhere.
2. **Accept both on input** indefinitely, so existing stored ids keep resolving.
3. **Add a read-one endpoint** — either make `get_content` accept playlist DIDs, or add
   `get_playlist { id }` returning metadata plus ordered items:
   ```jsonc
   { "id": "did:rad.live:content/playlist/<uuid>",
     "title": "…", "summary": "…", "channel": "did:rad.live:channel/<uuid>",
     "itemCount": 12,
     "items": [ { "id": "did:rad.live:content/feature/<uuid>",
                  "title": "…", "position": 0 } ] }
   ```

### Done when
- Every endpoint returns the canonical form for the same playlist
- Both forms are accepted as input everywhere a playlist id is taken
- A read-one endpoint returns membership in order
- Regression test asserts create→read id equality

---

## RAD-4 · `sourceUrl` reads as provenance but triggers server-side download

**Severity:** low (design) · **Surface:** `publish_video`

### Symptom
`sourceUrl` means *"server, download the video from this URL"* and switches
`publish_video` into server-side fetch mode. The name reads like "where this came from".

### Impact
A migration client wants to record origin. `sourceUrl` is the field everyone will reach
for, and reaching for it silently changes the entire ingestion path: Rad re-downloads
from the origin and the client's already-uploaded file is ignored.

For a YouTube importer this is a correctness failure with a security-ish edge — the
server-side fetch is subject to the same bot-detection and rate limiting the client
exists to work around, so it fails in ways the client cannot see or retry intelligently.

This bit us: the field was being sent purely as a provenance stamp.

### Proposed contract
Either
- **rename** the behavioural field to `fetchUrl` / `ingestFromUrl` (keep `sourceUrl` as
  a deprecated alias for one release), **or**
- **leave it** and add the provenance field from RAD-6, and amend the `sourceUrl`
  description to lead with *"Server-side fetch: Rad downloads the video from this URL.
  This is not a provenance field — see externalUrl."*

### Done when
- Tool description states the behaviour unambiguously in its first sentence
- A provenance field exists that does not alter ingestion behaviour (RAD-6)

---

# Gaps

## RAD-5 · No delete for content or playlists

**Severity:** high · **Surface:** content and playlist lifecycle

### Symptom
`unpublish_content { id }` sets the ACL to protected and marks content unreleased, but
the record remains. There is no delete for content, and none for playlists.

### Impact
Every failed, abandoned, or test publish is permanent. This compounds with two known
behaviours:
- transient `publish_video` failures create empty content records with no asset;
- RAD-3 causes duplicate playlist creation.

A prior migration accumulated ~13 orphan records this way. Any retry-heavy or automated
workflow accretes garbage on the channel indefinitely, and there is no supported way for
a client to clean up after itself. Our own integration test could not fully clean up —
it left one content record and two playlists that can only be removed out-of-band.

### Proposed contract
```jsonc
// delete_content
{ "name": "delete_content",
  "arguments": { "id": "did:rad.live:content/feature/<uuid>",
                 "force": false } }   // force required if currently published
// -> { "deleted": true, "id": "…", "assetsRemoved": 2 }

// delete_playlist
{ "name": "delete_playlist",
  "arguments": { "id": "did:rad.live:content/playlist/<uuid>" } }
// -> { "deleted": true, "id": "…" }
```

Suggested semantics:
- Refuse to delete published content unless `force: true`, so accidents are hard
- Soft-delete server-side is fine; the requirement is that it stops appearing in
  `channel_dashboard` and any content listing, and frees the title for reuse
- Cascade: deleting content removes it from any playlist it belongs to
- Deleting a playlist never deletes its content
- Idempotent: deleting an already-deleted id returns `{ "deleted": false }`, not an error

### Done when
- A client can create content, then remove it, leaving no trace in any listing
- Deleting content referenced by a playlist updates that playlist's `itemCount`
- Attempting to delete published content without `force` fails with a clear message
- Regression test: full create → delete → list round trip asserts absence

---

## RAD-6 · No provenance field on content

**Severity:** high · **Surface:** `publish_video`, `create_content`, `update_content`,
`channel_dashboard`

### Symptom
Nothing on a content record can record where it came from. `update_content` accepts only
`title`, `name`, `summary`, `shortSummary`, `containsAds`, `type` — no keywords, no
arbitrary metadata, no external id.

### Impact
Re-running an import cannot reliably distinguish "already imported" from "new".
radmigrate falls back to matching on normalised title plus release date. That is a
workable key but not exact: it breaks on retitled videos, and on genuinely distinct
videos sharing a title and upload date.

This is the single change that would most improve any importer, not just this one.

### Proposed contract
Add two optional fields, settable at create time and returned on read:

```jsonc
// publish_video / create_content arguments
{ "externalSource": "youtube",              // free-form, short
  "externalId": "dQw4w9WgXcQ",              // id within that source
  "externalUrl": "https://www.youtube.com/watch?v=dQw4w9WgXcQ" }  // optional, display only
```

Requirements:
- `externalUrl` must **not** trigger server-side fetch (contrast RAD-4)
- All three returned by `get_content` and `channel_dashboard`
- Settable via `update_content` so existing records can be backfilled
- Ideally indexed, with a lookup:
  ```jsonc
  { "name": "find_content_by_external_id",
    "arguments": { "externalSource": "youtube", "externalId": "dQw4w9WgXcQ" } }
  // -> { "found": true, "id": "did:rad.live:content/feature/<uuid>" }
  ```
  A uniqueness constraint on `(channel, externalSource, externalId)` would make
  double-import structurally impossible rather than merely detectable.

### Done when
- Provenance survives a create → read round trip
- `channel_dashboard` returns it for every item
- Lookup by external id resolves to the right content, or reports not-found
- Regression test asserts a second import of the same external id is detected

---

## RAD-7 · No paginated content listing

**Severity:** high · **Surface:** `channel_dashboard`

### Symptom
`channel_dashboard` is the only way to enumerate a channel's content. It defaults to 10
items, **caps at 100**, and exposes `hasMoreContent` but no cursor or offset.

### Impact
On a channel with more than 100 items, a client cannot determine what is already
published, so deduplication is impossible past that boundary. Source channels in the
migration case routinely have hundreds or thousands of videos — the one we tested
against has 818, another has 1,119. This is the hard limit on doing the job correctly.

radmigrate detects `hasMoreContent` and warns the user rather than silently
under-reporting, but it cannot solve it client-side.

### Proposed contract
```jsonc
{ "name": "list_channel_content",
  "arguments": { "channel": "did:rad.live:channel/<uuid>",   // optional, defaults to own
                 "limit": 100,                                // default 50, cap 500
                 "cursor": "<opaque>",                        // omit for first page
                 "visibility": "all" } }                      // all | public | protected
```
```jsonc
// response
{ "items": [
    { "id": "did:rad.live:content/feature/<uuid>",
      "title": "…",
      "releaseDate": "2022-08-15T12:00:00Z",
      "visibility": "public",
      "processingStatus": "ready",
      "duration": 16,
      "externalSource": "youtube",        // from RAD-6
      "externalId": "dQw4w9WgXcQ" }
  ],
  "nextCursor": "<opaque or null>",
  "total": 818 }
```

Requirements:
- Stable ordering across pages (e.g. `createdAt` descending, id as tiebreak) so
  concurrent writes cannot cause an item to be skipped or repeated
- `releaseDate` included — `channel_dashboard` currently omits it, which is what forces
  title-only matching today
- `nextCursor: null` unambiguously means "end"

### Done when
- A channel with >100 items can be fully enumerated by following cursors
- Page boundaries do not drop or duplicate items when content is added mid-walk
- `total` matches the number of items yielded by a full walk

---

## RAD-8 · Prose responses require regex scraping

**Severity:** medium · **Surface:** `publish_video` and others

### Symptom
Several tools answer in human-readable text, so ids and URLs must be recovered with
regular expressions:

```python
re.search(r"Content ID: (did:rad\.live:content/feature/[\w-]+)", text)
re.findall(r"https://api\.rad\.live/ingestion/[^\s\"'\\]+", text)
```

The ingestion-URL convention — video first, thumbnail second — is positional and
undocumented.

### Impact
Fragile against any wording change. A copy edit to a tool's response string silently
breaks every integrator.

### Proposed contract
Return structured JSON alongside (or instead of) the prose:

```jsonc
{ "contentId": "did:rad.live:content/feature/<uuid>",
  "ingestion": {
    "video":     { "url": "https://api.rad.live/ingestion/<token>", "method": "POST",
                   "encoding": "multipart/form-data", "field": "file" },
    "thumbnail": { "url": "https://api.rad.live/ingestion/<token>", "method": "POST",
                   "encoding": "multipart/form-data", "field": "file" } },
  "autoFinalize": true }
```

MCP `structuredContent` is the natural home for this if the prose must stay for
human readers.

### Client impact: none
radmigrate already prefers a structured payload when present and only falls back to
scraping. Returning structured JSON requires **no client change** — the regexes simply
stop being reached. See `_parse_publish` in
[`src/radmigrate/dest/radtv.py`](../src/radmigrate/dest/radtv.py) for the precedence.

### Done when
- `publish_video` returns machine-readable `contentId` and ingestion targets
- Field names and encoding are explicit, not positional
- A client can integrate without a single regex

---

# API ergonomics

Non-blocking, but each costs an integrator a failed call and a debugging session.

| # | Issue | Fix |
|---|---|---|
| RAD-9 | `unpublish_content` takes `id`; most content tools take `contentId` | Accept both; document one |
| RAD-10 | `submit_content_for_processing` rejects `contentId` with an input-validation error — the argument name differs from sibling tools | Accept `contentId` |
| RAD-11 | `list_channels` enumerates the whole platform, not the caller's channels. Own channel is only discoverable via `verify_connection` | Add `mine: true`, or rename, or add `list_my_channels` |
| RAD-12 | `create_content_asset` is described as *"TUS upload"* but accepts a plain `multipart/form-data` POST. Following the description fails | Correct the description, or support TUS as documented |

RAD-12 is worth prioritising within this group: it actively sends integrators down the
wrong path. Posting a raw body with `Content-Type: video/mp4` — the obvious reading of
"TUS upload endpoint" — fails, and the working shape (multipart, `file` field) is
discoverable only by trial.

---

# Behaviour to document (not bugs)

These are all correct, but undocumented, and each one is a plausible integration bug.

- **Transcode jobs are created asynchronously.** For a window after upload,
  `get_processing_status` returns the *error* `No transcode job found for this content`.
  This is **pending**, not failure — the job appears shortly after and completes
  normally. A client treating it as an error will condemn perfectly good videos; we hit
  this on the first live run.
  **Suggested fix:** return `{ "status": "pending", "job": null }` instead of an error,
  so pending is representable without string-matching. If the error must stay, say so in
  the tool description.
- **`autoFinalize: true`** on the video upload response means the server submits for
  processing and publishes on its own; `finalize_content_upload` is a fallback, not a
  required step. The thumbnail upload returns `autoFinalize: false`, which is expected
  and not an error.
- **Release dates must be in the past.** A fresh upload dated today is rejected; clients
  must clamp. Worth stating the boundary precisely — is it server-now, or start of day?
- **Status vocabulary.** Job status is `TRANSCODING` / `COMPLETE` / `ERROR`; the
  content-level field is `processingStatus` (`ready`). These are distinct and both
  appear in responses.
- **`layout`** accepts `LANDSCAPE` / `PORTRAIT` / `VR` and defaults to `LANDSCAPE`.
  Clients should set it from real video dimensions; a vertical video left at the default
  displays incorrectly.

---

# Verified working

The full path, with no workarounds beyond the above:

```
verify_connection
  → channel resolution
  → channel_dashboard (existing content)
  → publish_video (publish:false → draft)
  → multipart video upload      → {uploaded:true, autoFinalize:true}
  → multipart thumbnail upload  → {uploaded:true, autoFinalize:false}
  → auto-finalize
  → TRANSCODING 20% → 90% → COMPLETE 100%
  → deployed HLS asset (master.m3u8)
  → re-listed via channel_dashboard and matched
  → unpublish_content
```

Auth, permissions, channel resolution, ingestion, transcoding, and status reporting all
behaved correctly throughout.

---

# Suggested order of work

1. **RAD-7** (pagination) + **RAD-6** (provenance) — together these are what stop an
   importer from being correct on a real channel. Highest value, and they are
   complementary: pagination is far more useful when the listing carries provenance.
2. **RAD-5** (delete) — without it every mistake is permanent, and RAD-3 manufactures
   mistakes.
3. **RAD-2** + **RAD-3** (playlists) — playlists are currently not usable
   programmatically. RAD-3 first: it is cheap and stops orphan creation.
4. **RAD-1** (duration) — user-visible wherever runtime is shown.
5. **RAD-8** (structured responses), **RAD-12** (wrong upload docs), then the remaining
   ergonomics — all cheap, each removes a class of integration bug.

## Reference client

[radmigrate](../README.md) exercises all of this and is written to adopt the fixes with
no changes where possible:

- structured responses are already preferred over prose (RAD-8)
- provenance matching already prefers a stamped external id and only falls back to
  title+date (RAD-6) — see `DestinationContent.matches` in
  [`src/radmigrate/models.py`](../src/radmigrate/models.py)
- both playlist DID forms are already accepted (RAD-3)
- `pending` transcode state is already handled (see "Behaviour to document")

Its test suite includes fixtures for the current behaviour, so it can serve as a
regression check that changes are backwards compatible:
[`tests/test_radtv_live_findings.py`](../tests/test_radtv_live_findings.py).
