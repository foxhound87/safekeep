# Troubleshooting

## Start with `doctor`

```bash
safekeep doctor
```

Checks: `fswatch` presence, version and **the monitor for your OS**
(`fsevents_monitor` on macOS, `inotify_monitor` on Linux), global config and
every `.sync` syntax, no destination inside a source (copy loop), python
version, permission errors in the log from the last 24h (0.3.1), TCC + launchd
plist lint (macOS), inotify watch limit + systemd user unit + linger (Linux),
WSL detection (0.4.0, best-effort), leftover `.safekeep.tmp.*` files. Exit `1`
on a fatal failure; every check described below is non-fatal.

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

## Recent permission errors in the log

Since 0.3.1 `doctor` counts permission errors in the **current** log
(`~/.local/state/safekeep/safekeep.log`) from the **last 24 hours** — the
follow-up to the incident where a volume lost its permissions and 223 copies
started failing with `EPERM`:

```text
✗ errori di permesso recenti: 223 nelle ultime 24h (ultimo: 2026-10-04 18:57:29)
  — log: ~/.local/state/safekeep/safekeep.log — warning non fatale: …
```

Matched lines carry `[Errno 1] Operation not permitted` or
`[Errno 13] Permission denied`. The check is **non-fatal** (`doctor` still
exits `0`): it is a recent symptom, not a system state. What to do:

1. what is still waiting: `safekeep sync-once --dry-run`;
2. re-grant the permissions — the destination **volume** on macOS, or
   [FDA](#full-disk-access-fda) for protected folders;
3. the log path is printed with the warning.

Limits, on purpose: only the current log is read (rotations `.log.1` …
`.log.5` are not), a line whose timestamp cannot be parsed is **not** counted
(so the number is a minimum), and a missing log skips the check.

To follow the trend instead of looking at a single run, use
`safekeep doctor --json`: the same data in structured form
(`permission_errors.count_24h` and `permission_errors.last`) — and
[`bin/safekeep-trend.sh`](/commands#permission-trend-csv-hourly) already
appends one line per run to `~/.local/state/safekeep/permission-trend.csv`
(hourly agent included), so the trend is a CSV away — see
[Commands](/commands#doctor-json).

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

## systemd linger

The systemd *user* unit starts with the login session only: without linger it
runs after the first login, not at boot.

```bash
loginctl enable-linger $USER            # no root needed
loginctl show-user $USER -p Linger      # yes / no
```

Since 0.3.1 `doctor` checks linger whenever
`~/.config/systemd/user/safekeep.service` is installed, and warns (non-fatal)
when it is off:

```text
✗ linger: NON abilitato — l'agent non partirà al boot senza login
  → loginctl enable-linger $USER — warning non fatale
```

It reads `/var/lib/systemd/linger/` directly — the same truth as `loginctl`,
without root. No systemd at all → the check is skipped; no unit installed → it
is not shown. See [Agent](/agent).

## Windows: WSL (best-effort)

There is no native Windows build: run safekeep **inside WSL (Windows
Subsystem for Linux)**, where it is a plain Linux program. **Honesty first:
not yet tested on a real WSL machine** — 0.4.0 ships detection and graceful
degradation only; the Linux runtime it inherits (systemd user unit, inotify,
fswatch) is the 0.3.0 one, E2E-tested on real Linux.

`doctor` prints these lines when it detects WSL (`WSL_DISTRO_NAME` set, or a
`microsoft` kernel) — all three are non-fatal:

```text
✔ WSL rilevato: Ubuntu (supporto best-effort, SPEC §17)
```

- **no systemd user manager** (WSL1, or WSL2 with systemd off) → a warning with
  the fix — enable it and restart WSL:

  ```ini
  # /etc/wsl.conf   (from Windows: wsl --shutdown, then reopen the distro)
  [boot]
  systemd=true
  ```

  …or skip the agent and run the daemon by hand: `safekeep run`.

- **sources/destinations under `/mnt/…`** → a drvfs note: that filesystem is
  slow (9p translation) and case-insensitive by default — prefer a native ext4
  destination inside the distro (e.g. `~/backup`) over `/mnt/c/...`.

`install.sh` follows the same rule: without systemd it installs **no** unit and
exits `0` with manual-start instructions instead of failing.

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
