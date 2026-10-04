# Troubleshooting

## Start with `doctor`

```bash
safekeep doctor
```

Checks: `fswatch` presence, version and **the monitor for your OS**
(`fsevents_monitor` on macOS, `inotify_monitor` on Linux), global config and
every `.sync` syntax, no destination inside a source (copy loop), python
version, TCC + launchd plist lint (macOS), inotify watch limit + systemd user
unit (Linux), leftover `.safekeep.tmp.*` files. Exit `1` on a fatal failure.

## Full Disk Access (FDA)

**FDA (Full Disk Access)** is the TCC permission that opens the whole disk.
Without it, safekeep cannot read protected folders.

Grant it to **both**:

1. the Python interpreter running safekeep (`/usr/bin/python3` or your Homebrew
   python) — *System Settings → Privacy & Security → Full Disk Access*;
2. `fswatch`, when it runs as a separate binary.

Granting it to the terminal is **not** enough for the launchd daemon: the job
inherits the login context, not the terminal's per-session consents.

Symptoms: `EACCES` / `Operation not permitted` in the log, projects missing in
auto-discovery mode.

**After changing permissions, rebuild the agent** — a loaded launchd job keeps
its old context:

```bash
launchctl bootout gui/$(id -u)/com.safekeep.agent
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.safekeep.agent.plist
```

## Linux: inotify watch limit

On Linux `fswatch` watches trees through **inotify** (FSEvents on macOS). One
watch per directory, capped per user:

```bash
sysctl fs.inotify.max_user_watches
```

`doctor` warns below 16384 (non-fatal): a home tree of ~25k files runs out, the
extra watches are silently dropped and **new** events stop arriving — files
already backed up stay safe, and the 24h reconcile still catches up. Raise it:

```bash
sudo sysctl -w fs.inotify.max_user_watches=524288            # this boot
echo 'fs.inotify.max_user_watches=524288' \
  | sudo tee /etc/sysctl.d/50-safekeep.conf                  # and at every boot
```

The systemd user unit starts with the login session only: without
`loginctl enable-linger $USER` it runs after the first login, not at boot
(see [Agent](/agent)).

## Nothing is copied

1. Is there a `.sync` in the project root? No `.sync` → not followed.
2. Does any rule match the file? It is an **allow-list**: no matching include
   → not copied. Check with:

   ```bash
   safekeep sync-once --dry-run --project ~/Code/myapp
   ```

3. Is a parent directory excluded? An excluded directory prunes its subtree.
4. Is `dest:` set in `~/.safekeep`? It is mandatory.

## Logs

```bash
tail -f ~/.local/state/safekeep/safekeep.log   # both platforms (rotating, 5 x 5 MB)
journalctl --user -u safekeep -f               # Linux: what the unit prints
```

Raise verbosity with `safekeep run -v` (foreground) or `log_level: debug` in
`~/.safekeep`.

## Destination shows `pending`

The destination volume is not mounted. safekeep retries the mountpoint stat
with exponential backoff (1s → 60s cap) and reconciles that project as soon as
the volume is back. Check with `safekeep status`.

## Leftover `.safekeep.tmp.*` files

A crash during a copy. They are removed on the next copy to that destination;
`doctor` lists any that remain, and they are safe to delete manually.
