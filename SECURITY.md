# Security Policy

## Reporting a vulnerability

Please report security issues privately via
[GitHub Security Advisories](https://github.com/little-core-labs/rad-migrate/security/advisories/new).

Do not open a public issue for a vulnerability.

We aim to acknowledge within 3 business days and to ship a fix or a mitigation plan
within 30 days for confirmed issues.

## What this tool handles

rad-migrate touches credentials, so it is worth being explicit about what it does with
them.

**Rad API key.** Read from the environment, a `.env` file, or `--api-key`. Held in
memory as a `SecretStr` so an accidental `repr()` in a log line or traceback prints
`**********` rather than the value. Sent only as a `Bearer` header to the configured
API host. **Never** written to the state database, `--json` output, NDJSON progress
events, or any log.

**YouTube cookies.** Only read when you explicitly ask for them, via
`--cookies-from-browser` or `--cookies-file`. Passed straight to yt-dlp and never
copied, cached, or transmitted anywhere except YouTube. rad-migrate has no telemetry.

**Redaction.** Everything the tool prints passes through a redaction filter first
([`src/rad_migrate/redact.py`](src/rad_migrate/redact.py)). It masks registered secret
values literally, plus a pattern layer covering bearer tokens, credential assignments,
Netscape cookie lines, session cookie names, and signed-URL parameters. The pattern
layer exists to catch secrets we were never told about — a token echoed inside an
upstream error message, for instance. This is enforced by tests, including an
end-to-end assertion that a registered key cannot reach stdout or stderr in any output
mode.

## Third-party binaries

`rad-migrate setup-potoken` downloads a proof-of-origin token provider. This is the one
place the tool fetches an executable, and it is deliberately constrained:

- it never runs automatically — only when you invoke that command;
- the release is **pinned** in [`src/rad_migrate/potoken.py`](src/rad_migrate/potoken.py),
  never resolved as "latest";
- every file is verified against the SHA-256 digest GitHub publishes for that asset,
  fetched from the API rather than from the download itself;
- the URL and hash are printed before anything is written, and you are asked to confirm;
- everything installs under rad-migrate's own data directory, and `--remove` deletes it.

If a checksum does not match, installation aborts and nothing is written.

## Supply chain

- Dependencies are pinned in `uv.lock` and installed with `uv sync --frozen` in CI.
- CI runs gitleaks over the **full history** on every push and pull request.
- Releases publish to PyPI via trusted publishing (OIDC), so there is no long-lived
  API token to leak.
- GitHub Actions are pinned to major version tags.

## Scope

In scope: credential leakage, path traversal, unsafe deserialisation, dependency
vulnerabilities, and anything that causes rad-migrate to write outside its working
directory.

Out of scope: vulnerabilities in yt-dlp, ffmpeg, or the Rad API itself — please report
those upstream. Rate limiting or bot detection by YouTube is a platform behaviour, not
a vulnerability.
