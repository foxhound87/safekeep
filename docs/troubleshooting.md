# Troubleshooting

## Start with `doctor`

```bash
safekeep doctor
```

Checks: `fswatch` presence, version and **the monitor for your OS**
(`fsevents_monitor` on macOS, `inotify_monitor` on Linux), global config and
every `.sync` syntax, no destination inside a source (copy loop), python
version, permission errors and copy outcomes in the log from the last 24h
(0.3.1 / 0.11.2), TCC + launchd
plist lint (macOS), the running daemon's executable still on disk (0.5.1,
macOS), inotify watch limit + systemd user unit + linger (Linux),
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
safekeep daemon stop && safekeep daemon start
# (≡ launchctl bootout + bootstrap on the rendered plist)
```

## Removable volumes (macOS 26+)

**«Interagisci con i volumi rimovibili»** (*Removable Volumes*) is a TCC
permission **separate from FDA**: without it, every I/O under `/Volumes/…` is
denied with `EPERM` (`[Errno 1] Operation not permitted`). Grant it to the
**executable** that does the I/O — the Python interpreter the daemon really
uses, not the name “safekeep” (TCC authorises binaries). Find it with:

```bash
ps -p "$(launchctl print gui/$(id -u)/com.safekeep.agent | sed -n 's/^ *pid = //p')" -o comm=
```

`doctor` prints that path in its “volumi rimovibili” hint.

Since 0.11.3 a destination denied like this is **not** retried file by file:
safekeep marks it `[not-accessible]`, puts it in `pending` with backoff and
stays quiet until the permission is granted — the log no longer fills with
hundreds of identical errors per day.

## Intermittent `EPERM` on `/Volumes` — the multi-name shim (macOS)

**Symptom:** `EPERM` (`[Errno 1] Operation not permitted`) on every copy to the
external disk, but **intermittent** — the same job fails and then succeeds, the
volume is mounted, writable and has free space, and the permissions look fine.

**Root cause:** `/usr/bin/python3` is the **xcrun shim** — the *same inode* as
`/usr/bin/git` and 76 other names in `/usr/bin`, `/bin`, `/usr/sbin`, `/sbin`
(78 hardlinks). TCC (Transparency, Consent and Control) records the grant on
the **responsible path**, i.e. the *resolved* path of the process. With a
multi-name shim the kernel can resolve a different alias than the one the
grant was issued for, and the grant then simply does not match:

```text
kernel: (Sandbox) System Policy: Python(57776) deny(1) file-write-create /Volumes/…
tccd:   responsible={identifier=com.apple.sh, responsible_path=/bin/sh},
        accessing={identifier=com.apple.python3, binary_path=…/Python}
```

Measured on one machine: processes with responsible path `/usr/bin/git` →
42/42 `EPERM`; with `/usr/bin/python3` → 138/138 OK.

**Fix:** make the agent launch an interpreter with a **unique path**
(`st_nlink == 1`), e.g. Homebrew's:

```bash
# ~/Library/LaunchAgents/com.safekeep.agent.plist
#   <key>ProgramArguments</key>
#   <array>
#     <string>/opt/homebrew/bin/python3</string>   ← not /usr/bin/python3
#     <string>/Users/cla/safekeep/bin/safekeep.py</string>
#     <string>run</string>
#   </array>
```

Then reload the job (launchd does not re-read a plist on its own):

```bash
launchctl bootout gui/$(id -u)/com.safekeep.agent
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.safekeep.agent.plist
```

Since 0.11.4 `doctor` reports the situation:

```text
✗ programma del daemon: /usr/bin/python3 è uno shim multi-nome (stesso inode di
  altri binari di sistema) — TCC registra il grant sul path risolto, che può
  essere un alias diverso → EPERM intermittente sulle copie verso /Volumes →
  usa un interprete con path unico (nlink == 1), es. /opt/homebrew/bin/python3
  — warning non fatale
```

Caveat: the Homebrew path is version-pinned in the Cellar — a `brew upgrade`
can change it and invalidate the grant, so re-check with `doctor` afterwards.
The current official installer (`safekeep daemon install`) already avoids this
class of incident: the main plist has no interpreter at all (`ProgramArguments`
is the `safekeep` shim → `run`).

## Daemon runs from a deleted executable (Homebrew upgrade)

**Root cause of the ~45h EPERM incident (03/10/2026), fixed in 0.5.1.** Until
0.5.0 `install.sh` rendered the agent's Python as `/opt/homebrew/bin/python3`,
which Homebrew resolves into its versioned **Cellar** folder:

```text
03/10 18:54   launchd starts the daemon (exe = Cellar/python@3.14/3.14.6/…)
03/10 21:20   brew upgrade: python 3.14.6 → 3.14.8 DELETES the old Cellar
              folder → the executable under the live process is gone
hours later   TCC can no longer identify the process
              (proc_pidpath_audittoken() failed) → removable volumes denied
              → EPERM on EVERY copy, 5.251 identical ERROR log lines
recovery      re-grant permissions + launchctl kickstart -k
```

Symptoms: `doctor`'s "recent permission errors" exploding, every copy failing
with `Operation not permitted` while the permissions themselves look fine,
`fswatch` still delivering events normally.

**Detection (0.5.1):** `doctor` reads the *live process*
(`launchctl print` + `ps -o comm=`) and warns when that executable is gone —
it works even with an old, fragile plist still installed:

```text
✗ exe del daemon: /opt/homebrew/…/Python NON esiste su disco — il processo
  gira su un eseguibile cancellato … → launchctl kickstart -k
  gui/$(id -u)/com.safekeep.agent — warning non fatale
```

**Fix:** re-run the installer (idempotent) and reload the job.

```bash
safekeep daemon install   # re-render the unit (idempotent; does not start)
safekeep daemon stop      # bootout / disable (exit 1 if it was not loaded — fine)
safekeep daemon start     # bootstrap + start
./install.sh --trend      # same treatment for the hourly trend agent
```

Since 0.7.0 the main plist has **no interpreter at all**: `ProgramArguments`
is the `safekeep` shim → `run`, so this incident class no longer applies to
the main agent (verify with `ps -p <pid> -o comm=`: it must print an
executable **still on disk**). The `--trend` agent renders `__PYTHON__` =
`/opt/homebrew/bin/python3` since 0.11.4 — a **unique** path (`st_nlink == 1`),
not the multi-name `/usr/bin/python3` shim (see above). launchd does **not**
re-read a plist on its own: without `bootout`+`bootstrap` (or a reboot) the old
rendering keeps running. If Full Disk Access was granted to a Homebrew python,
grant it to `/opt/homebrew/bin/python3` too — a one-time step, that path never
changes again. A shell wrapper that re-resolves `python3` at every start was
considered and rejected: it would only resolve at start, recreating the same
incident on the next upgrade while the daemon keeps running (SPEC §8.4).

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
   [FDA](#full-disk-access-fda) for protected folders, or the stable
   interpreter if the [daemon runs from a deleted
   executable](#daemon-runs-from-a-deleted-executable-homebrew-upgrade);
3. the log path is printed with the warning.

Since 0.5.1 repeated failures for the **same (path, errno)** are throttled:
one line per 300-second window, later lines suffixed `— ripetuto N volte`,
instead of one line per failure (the incident wrote 5.251 identical lines in
two days). The count stays honest — `doctor` adds `N + 1` for every suffixed
line, so `count_24h` is still the true number of failures, just with far fewer
lines on disk.

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

## No successful copies in the last 24h

Since 0.11.2 `doctor` also counts **successful** copies against failed ones
in the same 24-hour window of the current log — the question the failure
counters alone could not answer during the incident above: *did anything
actually go through?*

```text
✗ copie nelle ultime 24h: 0 riuscite su 478 fallite — warning non fatale:
  vedi i pending con `safekeep sync-once --dry-run` e il log: …
✔ copie nelle ultime 24h: 12 riuscite, 3 fallite — log: …
```

Successes are the `INFO copiato …` lines (one per event-driven copy — at
INFO since 0.11.2; they used to be DEBUG and were invisible at the default
`log_level: info`) plus the per-pass totals `reconcile: N copie` and
`sync-once: N copie` (`sync-once` logs its own total since 0.11.2, dry-run
excluded — planning is not copying). Failures are the `copia fallita …`
lines, any errno, with the same `N + 1` throttle accounting as the check
above.

The check **warns (non-fatal) only when there were failures and zero
successes**. Any success in the window → ✔ with both counts visible: one
destination failing while another succeeds is what the
[permission-errors check](#recent-permission-errors-in-the-log) reports —
that one stays the per-destination signal. No activity at all → ✔
"nessuna attività"; missing log → check skipped.

What to do when it warns:

1. what is still waiting: `safekeep sync-once --dry-run`;
2. fix the failures themselves — see the permission-errors section above
   and [FDA](#full-disk-access-fda);
3. if manual `sync-once` runs succeed while the daemon keeps failing, the
   daemon's own process is the one without permission (the [root cause of
   the EPERM incident](#daemon-runs-from-a-deleted-executable-homebrew-upgrade)
   is the same shape: same user, different process identity).

Limits, on purpose: mount/network errors never write `copia fallita` lines
(the destination goes pending instead — see
[Destination shows `pending`](#destination-shows-pending)), and `restore`
does not contribute (its failures log as `restore fallito`, its successes
are not logged): only backup copies are counted. Same window and timestamp
rules as the check above — current log only, unparsable timestamps skipped
(counts are a minimum), missing log skips the check.

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
safekeep daemon install --enable-linger   # runs it for you (best-effort)
loginctl enable-linger $USER            # or by hand, no root needed
loginctl show-user $USER -p Linger      # yes / no
```

By default `daemon install` only **prints** the hint; with `--enable-linger`
it runs `loginctl enable-linger $USER` itself (a failure there is a warning,
not an error).

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

`safekeep daemon install` follows a stricter rule since 0.7.0: without
systemd the **preflight fails (exit 1)** with the fix above and the
manual-start alternative (`safekeep run`) — no unit is written.
(`install.sh`, pre-0.7.0, used to install nothing and exit `0`; the wrapper
now delegates to the new command.) `safekeep doctor` stays **exit 0** on WSL
without systemd: all three WSL checks are informational.

## `safekeep daemon install` fails (preflight)

`daemon install` writes nothing and exits `1` when one of these is missing —
each line comes with its fix:

- **`fswatch` missing** — install it for your platform (the error prints the
  right command: `brew` / `pacman` / `apt` / `dnf`). The unit's `PATH` can
  only *find* binaries, never install them.
- **`shim safekeep not found on PATH`** — `ExecStart` is the absolute path of
  the `safekeep` shim resolved on `PATH` at install time. With
  `pipx install safekeep` it is `~/.local/bin/safekeep`; with
  `pip install --user safekeep` make sure `~/.local/bin` is on the `PATH` of
  both your login shell and the service environment (put it in `~/.profile` /
  `~/.zprofile`, then open a new session). A shim that moves *after* install
  only breaks at the next `daemon start` — re-run `daemon install` to re-resolve.
- **WSL without systemd** — see
  [Windows: WSL](/troubleshooting#windows-wsl-best-effort).
- **python < 3.9 / unsupported OS** — upgrade python; safekeep needs macOS or
  Linux.

Nothing was written when this happens: fix the reported item and re-run
`safekeep daemon install`. Use `--dry-run` to check the preflight without
touching anything.

## Nothing is copied

1. Is there a `.sync` in the project root? No `.sync` → not followed.
2. Does any rule match the file? It is an **allow-list**: no matching include
   → not copied. Check with:

   ```bash
   safekeep sync-once --dry-run --project ~/Code/myapp
   ```

3. Is a parent directory excluded? An excluded directory prunes its subtree.
4. Is `dest:` set in `~/.safekeep`? At least one `dest:` **or** `s3_bucket:`
   is mandatory.

## Logs

```bash
tail -f ~/.local/state/safekeep/safekeep.log   # both platforms (rotating, 5 x 5 MB)
journalctl --user -u safekeep -f               # Linux: what the unit prints
```

Raise verbosity with `safekeep run -v` (foreground) or `log_level: debug` in
`~/.safekeep`.

## Destination shows `pending`

The destination volume is **not mounted**, or it is mounted but **not
accessible** (every I/O denied — e.g. the macOS *Removable Volumes* permission
missing, see below). safekeep retries with exponential backoff (1s → 60s cap)
and reconciles that project as soon as the volume is usable again. Check with
`safekeep status`: a destination that is mounted but denied shows
`[not-accessible]`.

## Leftover `.safekeep.tmp.*` files

A crash during a copy. They are removed on the next copy to that destination;
`doctor` lists any that remain, and they are safe to delete manually.
