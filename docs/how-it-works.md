# How it works

## Startup

1. Load `~/.safekeep`. Missing or invalid → exit non-zero (this is why `doctor`
   is run by hand: `KeepAlive` would only restart a failing job).
2. Discover projects — `source:` mode scans the sources for `.sync`;
   auto-discovery scans `$HOME` with dedicated pruning. Destinations are always
   the global ones.
3. **Reconcile** every project: recursive walk, copy each included file whose
   destination differs by size or mtime, skip identical ones (idempotent).

## Events

```
fswatch -0 -m <monitor> -r -l 1.0 -e <exclusion regex> -- <watch roots>
```

- `-0` → NUL separator, paths with spaces and newlines are safe
- `-m <monitor>` → the monitor for the platform, never the implicit default
  (see below)
- `-l 1.0` → at least 1s of batching, flood control at the source
- `-e` → a coarse pre-filter; the Python matcher stays the only source of truth

### Monitor per OS

The monitor name is exactly what `fswatch -M` lists, and it comes from
`safekeep/platform.py` — the only place that reads `sys.platform`:

| OS | Monitor | Watching is | Boot agent |
|---|---|---|---|
| macOS | `fsevents_monitor` | FSEvents (File System Events) | launchd (`RunAtLoad` + `KeepAlive`) |
| Linux | `inotify_monitor` | inotify | systemd user unit (`WantedBy=default.target` + `Restart=always`) |

`doctor` asserts that the expected monitor is present in `fswatch -M`; `doctor`
also reports the inotify watch limit (`fs.inotify.max_user_watches`) on Linux,
the equivalent of watching FSEvents capacity on macOS.

Per event:

1. Find the project containing the path. None → if it is a `<dir>/.sync` and the
   directory exists, discover and reconcile the new project on the spot;
   otherwise ignore the path.
2. An excluded directory → skip the subtree.
3. Evaluate the matcher (last-match-wins). Not included → not copied.
4. Queue the path.

**Dedup window (2s)**: the same path arriving twice within 2s is processed
once. The first suppression schedules a fixed deadline (`emit + 2s`) that later
arrivals do not extend, and the path is emitted at the deadline anyway. No
events are lost, no threads, no timers.

**Stability check**: two `stat` calls with the same size and mtime → copy;
otherwise retry after ~1s. This avoids copying a file that is being written.

**Anti-flood**: more than 5000 distinct paths for one project in a batch →
discard the incremental queue and run a full **reconcile** of that project
instead. One walk is cheaper than 50k stat+copy pairs.

## Atomic copy

1. temp file **in the destination's own directory** (same filesystem → atomic
   rename): `<dst>.safekeep.tmp.<pid>`
2. copy bytes + `copystat` (mtime, permissions)
3. `fsync()`
4. `os.replace()` — POSIX atomic rename

A reader sees the old file or the new one, never a partial one. Leftover temp
files are removed on the next copy; `doctor` lists the remaining ones.

Writes are contained: the resolved destination must stay **inside** the
destination root, symlink components pointing out are reset to real
directories. safekeep never writes outside the destination.

## Never deletes

Deletion at the source never propagates: a removed or renamed file stays in
the backup. The single exception is `sync-once --prune`, an explicit opt-in for
destination files whose source still exists but is no longer included.

## Unmounted destinations

Before each copy the destination root is stat'ed — no subprocess. Missing path
(the volume is not mounted) → the *(project, destination)* pair goes to
**pending**, with exponential backoff on the mountpoint stat:
`1s → 2s → 4s → … → 60s` (capped). When the mount returns → reconcile that
project toward that destination, catching up on everything missed. Because
nothing is ever deleted, replaying is always safe.

## Periodic reconcile

A timer fires every **24h**: a discovery rescan (recovers `.sync` files whose
event was lost during downtime) plus a reconcile of all projects. Other
triggers: daemon start, remount of a pending destination, `sync-once`, and the
>5000-path flood case.

## TCC / FDA

**TCC (Transparency, Consent and Control)** is the macOS framework that asks
for consent on protected resources; **FDA (Full Disk Access)** is the TCC
permission covering the whole disk.

Consequences for safekeep:

- Full Disk Access must be granted to the **Python interpreter that runs the
  script** and to **`fswatch`** — not just to the terminal you typed in. A
  launchd job inherits the login context but not the terminal's per-session
  consents.
- In auto-discovery mode the privacy-protected folders (`Desktop`, `Documents`,
  `Downloads`, `Movies`, `Music`, `Pictures`, `Public`) are **not scanned**: an
  `opendir()` there can block in the kernel for a launchd agent without FDA,
  and discovery would never finish. They are excluded before descending, so
  those folders are never even opened.
- Without FDA the daemon does not spin: a `PermissionError` on a source root is
  logged and retried with a slow 60s backoff. `doctor` reports the `EACCES`
  case with instructions.
