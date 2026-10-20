# safekeep

[![PyPI](https://img.shields.io/pypi/v/safekeep)](https://pypi.org/project/safekeep/)
[![Python](https://img.shields.io/pypi/pyversions/safekeep)](https://pypi.org/project/safekeep/)

**Selective always-on backups for macOS and Linux**: watches source folders with
[fswatch](https://github.com/emcrisostomo/fswatch) and copies only the files you
chose to their destinations — an **allow-list** model that **never deletes**
anything in the backup (a file removed or renamed at the source stays in the
backup).

Documentation: https://foxhound87.github.io/safekeep/

## Features

- **Allow-list**: a file is copied only if it matches at least one `include:`;
  no match → not copied. Unmatched directories are still traversed, while
  directories with an *excluded* verdict are pruned together with their subtree.
- **Atomic copy, never a half-written file**: the temp file lives in the
  destination's own directory, followed by `fsync` + `os.replace` → a reader
  sees either the old file or the new one, never a partial copy.
- **Reconcile** at startup, every 24h, on remount, and on `sync-once`: copies
  whatever differs by size/mtime, idempotently — lost events, crashes and reboots
  don't matter, the next reconcile brings everything back in sync.
- **Unmounted volumes**: missing destination → `pending` state with exponential
  backoff (1s → 60s cap), then a reconcile once the mount is back.
- **launchd (macOS) or systemd (Linux) at boot**: an agent with `RunAtLoad` +
  `KeepAlive` (or `WantedBy=default.target` + `Restart=always`) keeps the process
  alive and restarts it if it dies.
- **`.sync` carries rules only, destinations live only in `~/.safekeep`**: a
  `.sync` file can neither add nor remove destinations (a `dest:` line is an
  invalid line), so a repo cloned from a third party can't redirect the backup
  somewhere else.
- **S3-compatible destination** (optional `safekeep[s3]` extra): one
  S3-compatible bucket alongside the filesystem destinations — same
  size/mtime idempotency, metadata-based, and **never deletes anything**
  (`--prune` only touches filesystem destinations).

## Installation

```bash
pipx install safekeep        # recommended for a CLI
pipx install "safekeep[s3]"  # + S3-compatible destination (botocore)
# or
pip install safekeep
pip install "safekeep[s3]"
```

**Requirements**: macOS or Linux (POSIX with systemd), Python >= 3.9 and
[fswatch](https://github.com/emcrisostomo/fswatch):

```bash
brew install fswatch      # macOS
sudo pacman -S fswatch    # Arch / Omarchy
sudo apt install fswatch  # Debian / Ubuntu
sudo dnf install fswatch  # Fedora
```

The **agent** (daemon at boot: launchd on macOS, a systemd *user unit* on Linux)
is installed from a checkout of this repository with `./install.sh` — not from
the wheel — see [Agent](#agent) below.

## Agent

```bash
git clone https://github.com/foxhound87/safekeep.git
cd safekeep

# only external dependency, if not installed yet (see the per-OS list above)
brew install fswatch      # macOS — the script suggests the right command on Linux

# copies the example into ~/.safekeep, renders the plist / systemd unit,
# runs preflight checks
bash install.sh
```

Then:

1. Fill `~/.safekeep` with your real `dest:` entries — and, optionally, `source:`
   (the example ships with placeholders — see
   [`examples/safekeep.example`](examples/safekeep.example));
2. drop a `.sync` file in the root of every project you want to follow (see
   [`examples/sync.example`](examples/sync.example)); a directory **without** a
   `.sync` is not tracked;
3. load the agent:

```bash
# macOS (launchd)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.safekeep.agent.plist
# unload with: launchctl bootout gui/$(id -u)/com.safekeep.agent

# Linux (systemd user unit)
systemctl --user enable --now safekeep
# status with: systemctl --user status safekeep
# logs with:   journalctl --user -u safekeep -f
# unload with: systemctl --user disable --now safekeep
# start at boot without a login session: loginctl enable-linger $USER
```

On macOS, once you have granted TCC/FDA (Full Disk Access) permissions to the
Python interpreter and to `fswatch`, run `safekeep doctor` for diagnostics. On
Linux the same command checks the `inotify` watch limit instead.

## Quickstart

`~/.safekeep` (the only place where destinations live):

```bash
# observed roots: only directories containing a .sync are followed
source: ~/Code
source: ~/Projects

# destination for ALL projects
dest: ~/Backup/safekeep

# relative (default): <dest>/<path relative to the source root>
#   ~/Code/myapp/docs/a.md → ~/Backup/safekeep/myapp/docs/a.md
layout: relative

log_level: info
```

`source:` is **optional**. With it present (source mode) the watched roots are
exactly those entries, as always. Without it safekeep switches to
**auto-discovery**: it scans `$HOME` for `.sync` files (skipping hidden
directories, `Library`, `.Trash`, `.cache`, `node_modules`, `.git`, `.venv`,
`__pycache__`, `venv`), watches `$HOME`, and picks up any `.sync` created after
startup. `dest:` stays mandatory in both modes.

`~/Code/myapp/.sync` (rules only, no destinations):

```bash
name: myapp

# allow-list semantics: without these lines NOT A SINGLE file would be copied
*.md
.env
!secrets/old.env      # ! = exclude
```

Rules can be written as bare gitignore-style lines — **the inverse of gitignore**:
a line lists what to **copy**, not what to ignore (`*.md` includes markdown here,
excludes it in a `.gitignore`), and a leading `!` turns it into an `exclude:`.
Bare lines share the same ordered list as the explicit `include:`/`exclude:`
keys, so last-match-wins works the same way (see
[`examples/sync.example`](examples/sync.example)).

Dry run first, then start the daemon:

```bash
safekeep sync-once --dry-run   # prints what would be copied, copies nothing
safekeep run                   # daemon: watch + copy (foreground)
```

## S3-compatible destinations

safekeep can push every project to **one S3-compatible bucket**
(S3 = Simple Storage Service) in addition to (or instead of) the filesystem
destinations. The client is [botocore](https://botocore.amazonaws.com/),
installed via the optional extra — the core package stays stdlib-only:

```bash
pipx install "safekeep[s3]"      # or: pip install "safekeep[s3]"
```

Without the extra the config still parses, but any `s3_*` key fails fast with
an install hint (`doctor` reports it as a **non-fatal** warning).

`~/.safekeep` (the `s3_*` keys are valid **only** in this file — a `.sync`
can't add them; keep the file at `chmod 600`):

```ini
s3_endpoint: https://s3.example.com:443   # http:// → cleartext warning
s3_bucket: my-backup-bucket               # at least one dest: OR s3_bucket:
s3_prefix: safekeep                       # optional, no '..'
s3_region: us-east-1                      # default
s3_access_key: REPLACEME_ACCESS_KEY       # required when s3_bucket is set
s3_secret_key: REPLACEME_SECRET_KEY       # chmod 600 — or use s3_secret_key_env:
# s3_secret_key_env: SAFEKEEP_S3_SECRET   # secret from env, never in the file
s3_addressing: path                       # path (default) | virtual
s3_tls_check: true                        # false = self-signed certificates
```

With `s3_secret_key_env:` the variable must be in the **agent's** environment
(launchd `EnvironmentVariables`, systemd `Environment=`) — a shell `export`
alone doesn't reach a daemon started at boot.

Semantics (see [SPEC.md](SPEC.md) §18): upload-only with `needs_copy` based on
object metadata (`x-amz-meta-mtime` in nanoseconds, `x-amz-meta-mode`,
symlinks = zero-byte objects with `x-amz-meta-symlink`), same 1s mtime
tolerance as filesystem destinations, network errors → `pending` with
backoff, and **no deletions ever** — `--prune` never touches S3. A slow PUT
blocks the single-process daemon until it times out (connect 10s / read 60s);
that's an accepted v1 limitation.

Compatible services:

| Service | Example `s3_endpoint` | `s3_addressing` | `s3_region` | TLS |
|---|---|---|---|---|
| MinIO (self-hosted) | `https://minio.example.com:9000` | `path` | any (`us-east-1` default) | `https` + `s3_tls_check: false` if self-signed |
| Silo (fork of MinIO) | `https://s3.example.com:443` | `path` | any (`us-east-1` default) | as the endpoint's own certificate |
| Ceph RGW | `https://rgw.example.com` | `path` | any, must match the RGW zone (`us-east-1` default) | as configured on the RGW |
| Wasabi | `https://s3.wasabisys.com` | `path` | **required**, must match the bucket (`us-east-1` on that endpoint) | `https` |
| Backblaze B2 | `https://s3.<region>.backblazeb2.com` | `virtual` or `path` | **required**, must match the bucket region | `https` only — cleartext rejected |
| AWS S3 | *(omit `s3_endpoint`)* | `virtual` | must match the bucket (`us-east-1` default) | `https` |

All of them speak SigV4 (Signature Version 4) with the same client — no
service-specific code path. B2 wants an **Application Key** (not the Master
Key) as `s3_access_key`/`s3_secret_key`.

`safekeep status` shows the bucket as `[ok]`/`[offline]` (cached HeadBucket),
and `safekeep doctor` reports credentials, file permissions and bucket
reachability — all as **non-fatal** warnings.

## CLI commands

```
safekeep <command> [--config PATH] [-v]
```

| Command | What it does |
|---|---|
| `run` | daemon: initial reconcile, fswatch loop, event dispatch, 24h timer |
| `sync-once [--dry-run] [--project PATH] [--prune]` | a single pass: walks the source and copies whatever differs (`--dry-run` only prints what it would copy; `--prune` also removes dest files whose source still exists but is no longer included) |
| `status` | read-only: config, sources, discovered projects with N rules, destination states |
| `doctor [--json]` | diagnostics: config, fswatch + platform monitor, python, TCC/launchd plist (macOS), inotify limit and systemd unit (Linux) — exits non-zero if a fatal check fails; `--json` prints the same checks as one JSON document on stdout with the same exit code, including a structured `permission_errors` count of the last 24h |

## Tests

```bash
python3 -m unittest discover -s tests
```

Stdlib (`unittest`) suite, zero dependencies: 287 tests covering the matcher,
config, atomic copy, volumes, daemon, auto-discovery, CLI, the permission-trend
wrapper and the POSIX portability layer (platform helper, per-OS fswatch
monitor, systemd template).
`fswatch` is not needed to run the suite — the few `doctor` checks that talk to
the real binary are skipped when it's missing — but CI installs it so the
Linux job exercises the real `inotify` monitor.

The S3 integration test talks to a real bucket and is **skipped** unless every
`SAFEKEEP_S3_TEST_*` variable is set (no credential is hard-coded; it uploads
under a throwaway timestamped prefix and performs **no** DeleteObject):

```bash
export SAFEKEEP_S3_TEST_ENDPOINT=https://s3.example.com:443 \
       SAFEKEEP_S3_TEST_BUCKET=my-backup-bucket \
       SAFEKEEP_S3_TEST_ACCESS_KEY=REPLACEME_ACCESS_KEY \
       SAFEKEEP_S3_TEST_SECRET_KEY=REPLACEME_SECRET_KEY \
       SAFEKEEP_S3_TEST_REGION=us-east-1        # optional, default us-east-1
       # SAFEKEEP_S3_TEST_PREFIX=safekeep       # optional, default safekeep-itest
python3 -m unittest tests.test_s3_itest -v
```

## Documentation

Everything in detail (config format, pattern semantics, event → sync flow,
atomic copy, launchd, edge cases) lives in [`SPEC.md`](SPEC.md); commented
examples in [`examples/`](examples/).

## License

MIT — see [LICENSE](LICENSE)
