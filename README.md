# safekeep

**Selective always-on backups for macOS**: watches source folders with
[fswatch](https://github.com/emcrisostomo/fswatch) and copies only the files you
chose to their destinations — an **allow-list** model that **never deletes**
anything in the backup (a file removed or renamed at the source stays in the
backup).

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
- **launchd at boot**: an agent with `RunAtLoad` + `KeepAlive` keeps the process
  alive and restarts it if it dies.
- **`.sync` carries rules only, destinations live only in `~/.safekeep`**: a
  `.sync` file can neither add nor remove destinations (a `dest:` line is an
  invalid line), so a repo cloned from a third party can't redirect the backup
  somewhere else.

## Installation

```bash
git clone git@gitlab.com:foxhound87/safekeep.git
cd safekeep

# only external dependency
brew install fswatch

# copies the example into ~/.safekeep, renders the plist, runs preflight checks
bash install.sh
```

Then:

1. Fill `~/.safekeep` with your real `source:` and `dest:` entries (the example
   ships with placeholders — see [`examples/safekeep.example`](examples/safekeep.example));
2. drop a `.sync` file in the root of every project you want to follow (see
   [`examples/sync.example`](examples/sync.example)); a directory **without** a
   `.sync` is not tracked;
3. load the agent:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.safekeep.agent.plist
# unload with: launchctl bootout gui/$(id -u)/com.safekeep.agent
```

Once you have granted TCC/FDA (Full Disk Access) permissions to the Python
interpreter and to `fswatch`, run `python3 bin/safekeep.py doctor` for
diagnostics.

## Quick example

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

`~/Code/myapp/.sync` (rules only, no destinations):

```bash
name: myapp

# allow-list semantics: without these includes NOT A SINGLE file would be copied
include: /.env
include: /.env.*
include: .vault/
include: *.md
```

See what would be copied, without copying anything:

```bash
python3 bin/safekeep.py sync-once --dry-run
```

## CLI commands

```
bin/safekeep.py <command> [--config PATH] [-v]
```

| Command | What it does |
|---|---|
| `run` | daemon: initial reconcile, fswatch loop, event dispatch, 24h timer |
| `sync-once [--dry-run] [--project PATH]` | a single pass: walks the source and copies whatever differs (`--dry-run` only prints what it would copy) |
| `status` | read-only: config, sources, discovered projects with N rules, destination states |
| `doctor` | diagnostics: config, fswatch, TCC, launchd plist — exits non-zero if a fatal check fails |

## Tests

```bash
python3 -m unittest discover -s tests
```

Stdlib (`unittest`) suite, zero dependencies: 94 tests covering the matcher,
config, atomic copy, volumes, daemon and CLI. `fswatch` is not needed to run the
tests.

## Documentation

Everything in detail (config format, pattern semantics, event → sync flow,
atomic copy, launchd, edge cases) lives in [`SPEC.md`](SPEC.md); commented
examples in [`examples/`](examples/).

## License

MIT — see [LICENSE](LICENSE)
