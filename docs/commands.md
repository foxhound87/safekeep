# Commands

```
safekeep <command> [--config PATH] [-v]
```

| Command | What it does |
|---|---|
| `run` | daemon: initial reconcile, fswatch loop, event dispatch, 24h timer |
| `sync-once [--dry-run] [--prune] [--project PATH]` | one pass: walks the source and copies whatever differs, then exits |
| `status` | read-only: config, sources, discovered projects with N rules, destination states |
| `doctor [--json]` | diagnostics: config, fswatch + platform monitor, python, TCC/launchd plist (macOS), inotify limit and systemd unit (Linux) — exits non-zero if a fatal check fails; `--json` prints the same checks as one JSON document on stdout with the same exit code |

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

## `status`

```bash
safekeep status
```

Config path, mode (`source` / `$HOME` auto-discovery), sources, discovered
projects with their rule count, and per-destination state (`ok`, `absent`,
`pending`). Read-only, safe to run any time.

## `doctor`

```bash
safekeep doctor
```

Checks at least: `fswatch` presence, version and the monitor for the platform
(`fsevents_monitor` on macOS, `inotify_monitor` on Linux), config and `.sync`
syntax, that no destination is a subdirectory of a source (copy loop), python
version, TCC + launchd plist lint (macOS), inotify watch limit + systemd user
unit (Linux), and leftover `.safekeep.tmp.*` files. Exit `1` if a **fatal**
check fails.

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
