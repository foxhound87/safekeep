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

## Installation

```bash
pipx install safekeep     # recommended for a CLI
# or
pip install safekeep
```

**Requirements**: macOS or Linux (POSIX with systemd), Python >= 3.9 and
[fswatch](https://github.com/emcrisostomo/fswatch).
Windows: no native build — run it inside **WSL** (best-effort, see
[Troubleshooting](https://foxhound87.github.io/safekeep/troubleshooting/#windows-wsl-best-effort)):

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

## CLI commands

```
safekeep <command> [--config PATH] [-v]
```

| Command | What it does |
|---|---|
| `run` | daemon: initial reconcile, fswatch loop, event dispatch, 24h timer |
| `sync-once [--dry-run] [--project PATH] [--prune]` | a single pass: walks the source and copies whatever differs (`--dry-run` only prints what it would copy; `--prune` also removes dest files whose source still exists but is no longer included) |
| `status` | read-only: config, sources, discovered projects with N rules, destination states |
| `doctor [--json]` | diagnostics: config, fswatch + platform monitor, python, permission errors in the log (last 24h), TCC/launchd plist (macOS), inotify limit, systemd unit and linger (Linux), WSL detection (best-effort) — exits non-zero if a fatal check fails; `--json` prints the same checks as one JSON document on stdout with the same exit code, including a structured `permission_errors` count of the last 24h |

## Tests

```bash
python3 -m unittest discover -s tests
```

Stdlib (`unittest`) suite, zero dependencies: 206 tests covering the matcher,
config, atomic copy, volumes, daemon, auto-discovery, CLI, the permission-trend
wrapper and the POSIX portability layer (platform helper, per-OS fswatch
monitor, systemd template, doctor log/linger checks, WSL detection,
`install.sh` without systemd).
`fswatch` is not needed to run the suite — the few `doctor` checks that talk to
the real binary are skipped when it's missing — but CI installs it so every
matrix leg (3.9 / 3.12 / 3.14) exercises the real `inotify` monitor and the
live E2E daemon.

## Documentation

Everything in detail (config format, pattern semantics, event → sync flow,
atomic copy, launchd, edge cases) lives in [`SPEC.md`](SPEC.md); commented
examples in [`examples/`](examples/).

## License

MIT — see [LICENSE](LICENSE)
