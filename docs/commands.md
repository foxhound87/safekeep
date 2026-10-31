# Commands

```
safekeep <command> [--config PATH] [-v]
```

| Command | What it does |
|---|---|
| `run` | daemon: initial reconcile, fswatch loop, event dispatch, 24h timer |
| `sync-once [--dry-run] [--prune] [--project PATH]` | one pass: walks the source and copies whatever differs, then exits |
| `restore [--force] [--from {local,s3,nas}] [--dest NAME] [--if-dir-exists] [PATH\|GLOB ...]` | restore from the **latest copy** in a destination — always a dry-run unless `--force`; pick files by path/glob or with the interactive menu; `--dest` restores from a named destination (0.11.0, takes precedence over `--from`); `--if-dir-exists` only restores files whose destination directory already exists |
| `status` | read-only: config, sources, discovered projects with N rules, destination states |
| `doctor [--json]` | diagnostics: config, fswatch + platform monitor, python, TCC/launchd plist and daemon executable (macOS), inotify limit and systemd unit (Linux) — exits non-zero if a fatal check fails; `--json` prints the same checks as one JSON document on stdout with the same exit code |
| `daemon {install,start,stop,status,uninstall}` | manage the boot agent — see below |

## `run`

```bash
safekeep run        # foreground
safekeep run -v     # verbose
```

Load config, discover projects, run the initial reconcile, then watch.
The boot agent (`launchd` on macOS, `systemd` on Linux) keeps it alive;
`SIGTERM` shuts down cleanly (closes fswatch, drains the queue, exits 0).
Exits non-zero if the config is invalid or a destination
sits inside a source.

## `sync-once`

```bash
safekeep sync-once --dry-run                # print what would be copied
safekeep sync-once --project ~/Code/myapp   # one project only
safekeep sync-once --prune                  # remove dest files no longer included
```

- `--dry-run` copies nothing, it only prints the plan. Use it before a first
  real run or after editing rules.
- `--prune` is the **only** option that deletes: it removes destination files
  whose source still exists but is no longer included (rules changed). Only
  under the project prefix and only where the match is certain — a source that
  disappeared or became a directory is left untouched. Without `--prune`,
  nothing is ever removed.

## `restore`

```bash
safekeep restore ~/Code/myapp/docs/note.md    # dry-run: prints the plan
safekeep restore --force ~/Code/myapp         # actually restores
safekeep restore --force 'myapp/docs/*.md'    # glob, relative to the source root
safekeep restore --from s3 --force ~/Code/myapp
safekeep restore --dest nas --force ~/Code/myapp
safekeep restore --force                      # interactive menu (terminal only)
```

Restores files from the **latest copy** held by a destination (0.8.0). safekeep
keeps no history — the destination holds the last synced state, and that is
what you get back. Nothing is ever deleted.

- **A dry-run is always the default.** Without `--force` the command only
  prints `<backup> -> <target> (crea|sovrascrive)` rows and a summary; the
  interactive menu writes nothing either. Add `--force` to actually copy.
- **Arguments** are original paths: absolute, relative to the source root, or
  an `fnmatch` glob. No match → exit 1. No arguments + no terminal → exit 1.
- **No arguments on a terminal** opens a numbered menu: destination (only when
  ambiguous) → project → top-level backup entries; `1 3` picks numbers,
  `a` picks everything, `q` cancels.
- **`--from`** picks the origin: `local` = first `dest:`, `nas` = second
  `dest:`, `s3` = the S3 destination. Without it, a single configured
  destination is auto-detected (and logged); several candidates → an error
  listing them; a destination that isn't mounted → an error, never silently
  skipped.
- **`--dest <name>`** (0.11.0) picks a **named** destination (`dest <name>:`)
  and **takes precedence over `--from`** — the name identifies the origin
  unambiguously, filesystem or S3. An undeclared name → an error listing the
  available ones — see [Destination routing](/destination-routing).
- **Writes only inside the sources.** The target is always the original path —
  there is no `--to` — and a reconstructed path outside the project's source
  is refused (EACCES). Copies are atomic (temp file + `os.replace`) and
  symlinks come back as symlinks. S3 credentials come from the config only.
- **`--if-dir-exists`** (0.10.0) restores a file only if its destination
  directory already exists — missing parent directories are never created;
  those files are skipped and counted as `saltati` (works with every origin,
  including `--from s3`, where the check runs before the download):

  ```bash
  safekeep restore --force --if-dir-exists ~/Code/myapp
  ```

## `status`

```bash
safekeep status
```

Config path, mode (`source` / `$HOME` auto-discovery), sources, discovered
projects with their rule count, per-destination state (`ok`, `absent`,
`pending`) and, when configured, the S3 bucket
(`s3://bucket/prefix [ok]` / `[offline]` — a cached HeadBucket, 30s).
Read-only, safe to run any time.

## `doctor`

```bash
safekeep doctor
```

Checks at least: `fswatch` presence, version and the monitor for the platform
(`fsevents_monitor` on macOS, `inotify_monitor` on Linux), config and `.sync`
syntax, that no destination is a subdirectory of a source (copy loop), python
version, TCC + launchd plist lint and the live daemon's executable still on
disk (macOS, 0.5.1), inotify watch limit + systemd user
unit (Linux), and leftover `.safekeep.tmp.*` files. With an S3 destination
configured it also reports the credentials' origin, the config file
permissions, cleartext endpoints and bucket reachability — all as
**non-fatal** warnings. Exit `1` if a **fatal** check fails.

Run it **after** granting Full Disk Access — see
[Troubleshooting](/troubleshooting).

### `doctor --json`

```bash
safekeep doctor --json
```

Same checks, same order, same exit code — but the whole report is a single
JSON document on stdout, nothing else is printed, so `json.load` on stdout
never fails. Built to track the permission-error trend over time: a wrapper
appends one line per run to a CSV and the trend reads from there.

```json
{
  "safekeep": "0.4.0",
  "timestamp": "2026-10-04T10:20:30",
  "exit": 0,
  "checks": [
    {"id": "config_valida", "status": "ok", "message": "config valida: …"}
  ],
  "permission_errors": {"count_24h": 0, "last": null}
}
```

`status` is `ok` (a ✔ line), `warn` (a ✗ non-fatal line) or `fail` (a ✗ fatal
line — the one that makes the exit code 1); `checks[].message` is exactly the
text of the corresponding normal line, `checks[].id` a stable slug of it.
`permission_errors` is the structured form of the "permission errors in the
last 24h" check: `count_24h` plus the `last` timestamp (`null` when there is
no log). Full schema in SPEC.md §9.1.

### Permission-trend CSV (hourly)

`bin/safekeep-trend.sh` is the wrapper this flag was built for: it runs
`doctor --json`, extracts four fields and appends one line to
`~/.local/state/safekeep/permission-trend.csv` (SPEC.md §9.2).

```bash
bin/safekeep-trend.sh [--config PATH]     # one row per run
```

```csv
timestamp,exit,count_24h,last
2026-10-04T23:46:12,0,4828,2026-10-04 18:57:30
```

- the header is written **once** (created only when the file is missing or
  empty), one row per run — readable with a plain `split(',')`, no quoting;
- `exit` is `doctor`'s exit code, i.e. **data**: the row is appended even when
  `doctor` exits `1` (a fatal check is exactly what the trend must capture);
- the script fails (exit `1`, message on stderr) only when the output is not
  JSON — it never writes an invented row;
- overrides: `HOME` (fake HOME in tests), `SAFEKEEP_TREND_CSV`,
  `SAFEKEEP_PYTHON`, `--config PATH`.

On macOS the hourly agent is installed with:

```bash
./install.sh --trend          # renders launchd/com.safekeep.trend.plist + bootstrap
launchctl kickstart gui/$(id -u)/com.safekeep.trend   # first row now, not in an hour
./uninstall.sh --trend        # removes only the trend job
```

`StartInterval` 3600 → 24 rows/day (~1 KB), `StandardOut/ErrorPath` both to
`~/.local/state/safekeep/trend.log`. `install.sh --trend` never touches nor
reloads the main `com.safekeep.agent` job. On Linux no unit ships yet — run it
from cron or a systemd timer.

## `daemon`

```bash
safekeep daemon install                 # preflight + render + write the unit
safekeep daemon install --dry-run       # preview, nothing is written
safekeep daemon install --enable-linger # Linux: also run loginctl enable-linger
safekeep daemon start                   # load + start (bootstrap / enable --now)
safekeep daemon stop                    # unload + stop (bootout / disable --now)
safekeep daemon status                  # exit 0 if running, 1 otherwise
safekeep daemon uninstall               # remove the unit, keep ~/.safekeep + logs
safekeep daemon uninstall --purge       # also remove ~/.safekeep and state/logs
```

Manages the boot agent (launchd plist on macOS, systemd user unit on Linux —
full reference in [Agent](/agent)). Since 0.7.0; the `install.sh` /
`uninstall.sh` wrappers are deprecated (removal still pending) and just delegate
here — their `--trend` branch is the one legacy piece that stays.

- `install` is idempotent and **never starts the agent**: preflight first
  (`fswatch`, `safekeep` shim on `PATH`, python >= 3.9, WSL systemd-on) —
  a failed preflight writes nothing and exits `1`; then the config example
  (created only if missing, chmod `600`), the rendered unit (existing units
  with leftover placeholders are reported and replaced) and `plutil -lint` /
  `systemctl --user daemon-reload`;
- `start` is idempotent (already loaded → exit 0); missing unit → exit `1`
  with the install hint;
- `status` is read-only: exit `0` running, `1` not running (or no unit);
- `uninstall` removes **only** the unit — config, logs and state survive, so
  `daemon install` again restores everything; `--purge` deletes `~/.safekeep`
  and `~/.local/state/safekeep/` too.
