# MCAttach Relay server (optional self-hosting)

This directory publishes the actual source of the MCAttach Relay - the small,
self-contained PHP server that temporarily stores encrypted MCA/1 objects so
two MeshCenter nodes can exchange a file (via the `Files` tab / MCAttach)
even when they can't reach each other directly. It is published here for
transparency and for anyone who wants to run their own instance - **not**
as a replacement for the default, already-deployed Relay.

## Nothing changes by default

A stock MeshCenter installation talks to the project's own hosted Relay at
`https://mcattach.elektroniker.help` out of the box, exactly as before. This
publication does not touch `meshsrv/attachments/relay_client.py` or
`meshsrv/attachments/provider_registry.py` (the client-side code that talks
to whichever Relay is configured) - self-hosting is an opt-in path for
people who explicitly want to run their own instance, not a new default.
See [ADR-0005](../docs/architecture/ADR-0005-relay-hosting.md) for the full
protocol/auth/rate-limiting contract this server implements, confirmed
against this exact source.

## Why self-host

- Full control over who can see the technical metadata a Relay unavoidably
  observes (see "Data protection guarantees" below) - your own hosting
  account instead of the project owner's.
- Independent capacity/quota instead of sharing the default instance's.
- Running a private Relay for a closed group of nodes.

If none of that matters to you, there is nothing to do - keep using the
default Relay.

## What it is

A dependency-free PHP 8.2+ application (PDO MySQL + Sodium + JSON, no
third-party PHP libraries) implementing the MCA/1 Relay HTTP API: it accepts
already-encrypted ciphertext chunks and an encrypted manifest, stores them
under a random `transfer_id`, and serves them back to whichever recipient
proves capability (holds the `transfer_id` and the recipient-bound envelope
inside the manifest). It never sees plaintext file contents, filenames, or
MIME types - see `SECURITY.md` for the exact boundary between what
MeshCenter encrypts before upload and what the Relay itself can observe.

- `index.php`, `src/*.php` - the HTTP API and setup wizard (`/setup`,
  one-time, generates the server's own Ed25519 keypair and MySQL schema).
- `bin/cleanup.php` - expired/tombstoned-object cleanup, meant to run from
  `cron`, not over HTTP (`.htaccess` blocks direct web access to it).
- `docs/API.md` - the HTTP API reference (endpoints, auth, status codes).
- `tests/smoke_test.py` - a black-box smoke test against a running instance
  (see `tests/README.md`).
- `SECURITY.md` - threat model, implemented mitigations, and the checklist
  of what is **not** done yet (this is a `0.1.0` pilot build, not an
  independently audited public service).
- `README_RU.md` - the original, tested step-by-step install guide (in
  Russian) for Hostinger shared hosting specifically, written by the
  project owner alongside this source and kept verbatim.

## How to install (summary)

The detailed, tested walkthrough (Hostinger-specific commands, hPanel
screenshots' worth of steps, cron setup) is in `README_RU.md`. In outline,
for any PHP 8.2+ host with PDO MySQL, Sodium, JSON, a dedicated MySQL
database, and HTTPS on the domain/subdomain you'll use:

1. Create a dedicated MySQL database and user - do not reuse one shared
   with anything else.
2. Upload/extract this directory's contents so `index.php` and `.htaccess`
   end up directly at your web root (not in a subfolder).
3. Visit `https://<your-domain>/setup` over HTTPS. Setup generates the
   server's Ed25519 keypair and a one-time `upload access token` - **save
   that token immediately**, it is shown exactly once and MeshCenter's
   client needs it to be added as a trusted provider.
4. Verify `/health` and `/v1/info` respond correctly.
5. Schedule `bin/cleanup.php` to run daily via your host's `cron`, as a PHP
   CLI script (not over HTTP).
6. Only after `/health` and `/v1/info` check out, remove any placeholder
   page your host put at the web root.

Setup writes its configuration (MySQL credentials, the Relay's own Ed25519
secret key, the rate-limiting secret) to a private directory *outside* the
web root - never commit that file, an upload access token, or database
credentials to any repository.

Pointing your MeshCenter instance at a self-hosted Relay instead of (or in
addition to) the default one is a provider-registration step on the
MeshCenter side; there is currently no guided in-app wizard for it yet
(planned separately - see the project's MCAttach UX simplification plan).
Until then it follows the same trusted-provider mechanics ADR-0005
documents for the default Relay.

## Data protection guarantees

MeshCenter encrypts the file and its manifest, computes per-chunk digests,
and generates the `transfer_id` **before** anything reaches the Relay - the
Relay only ever stores and serves ciphertext it cannot decrypt. It does,
unavoidably, see technical metadata: the uploader/downloader's IP address
(via your host's own web server logs), upload/download timestamps, exact
ciphertext sizes, the `transfer_id`, and the number of receipt hashes. None
of that should be treated as anonymous. The full list of implemented
mitigations (HTTPS enforcement, hashed tokens, HMAC-pseudonymized rate
limiting by IP, atomic commit before an object becomes visible, hard
expiry/tombstones, no directory listing, security headers, no third-party
runtime dependencies) and the explicit pre-public-launch checklist
(independent code/crypto review, load testing, backup/deletion
verification, a privacy notice, abuse handling, key rotation, and more) are
in `SECURITY.md` - read it before running this for anyone other than
yourself.

## License

MIT, same as MeshCenter Core (`LICENSE` in this directory). No third-party
PHP dependencies are bundled or required.
