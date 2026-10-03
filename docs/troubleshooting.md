# Troubleshooting

## Start with `doctor`

```bash
safekeep doctor
```

Checks: `fswatch` presence and version, global config and every `.sync`
syntax, no destination inside a source (copy loop), destination writability,
launchd plist lint, source access + TCC probe, leftover `.safekeep.tmp.*`
files. Exit `1` on a fatal failure.

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
tail -f /tmp/safekeep.out.log
tail -f /tmp/safekeep.err.log
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
