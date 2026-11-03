# Agent (launchd / systemd)

The daemon runs as a per-user agent: started at boot, restarted if it dies —
a launchd agent on macOS, a systemd *user unit* on Linux. Since 0.7.0 it is
installed and managed with the `safekeep daemon` command (0.7.0 deprecates the
old `install.sh` / `uninstall.sh` wrappers; removal in 0.8.0).

## Install

```bash
pipx install safekeep        # or: pip install --user safekeep

# only external dependency, if not installed yet (see Install for other distros)
brew install fswatch      # macOS — Linux: pacman / apt / dnf

safekeep daemon install        # preflight + render + write; does NOT start
safekeep daemon install --dry-run   # preview only, nothing is written
```

`daemon install` is idempotent and does three things:

1. **preflight** — fails fast (exit 1, nothing written) if the OS is
   unsupported, `fswatch` is missing (prints the right install hint for your
   platform), python < 3.9, the `safekeep` shim is not on `PATH`, or you are
   on WSL (Windows Subsystem for Linux) **without systemd**;
2. creates `~/.safekeep` from the embedded example if missing (chmod `600`,
   never overwritten);
3. renders the platform template from the package — placeholders become real
   values:

   | | macOS | Linux |
   |---|---|---|
   | Template | `safekeep/data/com.safekeep.agent.plist.tmpl` | `safekeep/data/safekeep.service.tmpl` |
   | Rendered to | `~/Library/LaunchAgents/com.safekeep.agent.plist` | `~/.config/systemd/user/safekeep.service` |
   | Check | `plutil -lint` | `systemctl --user daemon-reload` (best-effort) |

It does **not** start the job: configure `~/.safekeep` first, then
`safekeep daemon start`.

### Preflight failures

```text
✗ fswatch mancante — installa con: brew install fswatch
✗ shim `safekeep` non trovato nel PATH — serve per ExecStart: pipx install safekeep …
✗ WSL (Windows Subsystem for Linux) senza systemd — abilita systemd in /etc/wsl.conf: …
```

- **`fswatch` missing** — the unit's `PATH` can only *add* directories, never
  install binaries: install it first (see [Install](/install#requirements)).
- **`safekeep` shim missing** — `ExecStart` is the absolute path of the shim
  found on `PATH` at install time (`shutil.which('safekeep')`): without it the
  agent could not start. With `pipx install safekeep` it lives in
  `~/.local/bin`; with `pip install --user` make sure `~/.local/bin` is on the
  `PATH` of **both** your login shell and the service environment.
- **WSL without systemd** — see [Windows: WSL](/troubleshooting#windows-wsl-best-effort).

## Start / stop / status

```bash
safekeep daemon start      # launchctl bootstrap / systemctl --user enable --now
safekeep daemon stop       # launchctl bootout / systemctl --user disable --now
safekeep daemon status     # read-only; exit 0 if running, 1 otherwise
```

`start` is idempotent (already loaded → "già caricato", exit 0). `stop` and
`status` exit `1` when the job was not loaded / is not running.

The equivalent manual commands:

```bash
# ---------- macOS (launchd) ----------
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.safekeep.agent.plist
launchctl bootout gui/$(id -u)/com.safekeep.agent
launchctl kickstart -k gui/$(id -u)/com.safekeep.agent   # restart in place
launchctl print gui/$(id -u)/com.safekeep.agent | head -40

# ---------- Linux (systemd user unit) ----------
systemctl --user enable --now safekeep     # ≈ bootstrap: register + start
systemctl --user disable --now safekeep    # ≈ bootout
systemctl --user restart safekeep          # ≈ kickstart -k
systemctl --user status safekeep           # ≈ print

journalctl --user -u safekeep -f           # logs

# boot without a login session (otherwise the unit starts after the first login)
safekeep daemon install --enable-linger    # runs loginctl enable-linger $USER
# or by hand (no root needed):  loginctl enable-linger $USER
```

**linger**: a systemd *user* unit starts with a login session only; linger
makes user units active even without a login. `daemon install` prints the hint
by default and runs it with `--enable-linger` (best-effort — a failed
`loginctl` is a warning, not an error).

## The plist (macOS)

Label `com.safekeep.agent`, `ProgramArguments` = the **`safekeep` shim** +
`run` (since 0.7.0: no interpreter in the template), `RunAtLoad` +
`KeepAlive`, `ThrottleInterval 30` (no frantic restart loop), `PYTHONUNBUFFERED=1`,
`PATH` (so `fswatch` is found — launchd does not inherit your login `PATH`).

| Setting | Value |
|---|---|
| Template | `safekeep/data/com.safekeep.agent.plist.tmpl` |
| Rendered to | `~/Library/LaunchAgents/com.safekeep.agent.plist` |
| ExecStart | `__EXEC__` → absolute `safekeep` shim, then `run` (survives `pipx reinstall`) |
| stdout / stderr | `~/.local/state/safekeep/out.log` / `err.log` (launchd does not expand `~` — the template's `__HOME__` does) |

`StartOnMount` (restart the job when a volume mounts) is documented in the
template and left commented out: the application-level backoff is the default
fallback.

## The unit (Linux)

`Type=simple`, `ExecStart` = the `safekeep` shim + `run`, `Restart=always`
with `RestartSec=2` (≈ `KeepAlive`), `WantedBy=default.target` (≈ `RunAtLoad`),
`PYTHONUNBUFFERED=1`, `Environment=PATH=…` (fswatch again).

`StartLimitIntervalSec=0` disables systemd's default rate limit (5 starts in
10s would park the unit in `failed` and stop restarting it) — the pacing stays
with the daemon, exactly like `ThrottleInterval 30` on macOS.

| Setting | Value |
|---|---|
| Template | `safekeep/data/safekeep.service.tmpl` |
| Rendered to | `~/.config/systemd/user/safekeep.service` |
| Logs | `journalctl --user -u safekeep -f` (the daemon log itself stays in `~/.local/state/safekeep/`) |
| Reload after editing the unit | `systemctl --user daemon-reload` |

### Injecting `s3_secret_key_env` into the agent

With `s3_secret_key_env:` in `~/.safekeep` the variable must be in the
**agent's** environment (a shell `export` does not reach a daemon started at
boot). Both templates ship the snippet **commented out** — uncomment and set
name and value:

```xml
<!-- plist -->
<key>SAFEKEEP_S3_SECRET</key>
<string>REPLACEME</string>
```

```ini
# systemd unit
Environment=SAFEKEEP_S3_SECRET=REPLACEME
```

Then reload (`safekeep daemon stop && safekeep daemon start` on macOS; edit +
`systemctl --user daemon-reload` + restart on Linux). Full reference:
[S3 destination](/s3#configuration).

## State

```bash
~/.local/state/safekeep/   # daemon log (both platforms)
```

## Uninstall

```bash
safekeep daemon uninstall            # removes the plist / unit only
safekeep daemon uninstall --purge    # ALSO removes ~/.safekeep and ~/.local/state/safekeep/
```

**Reversible by design**: `uninstall` without `--purge` keeps your config,
logs and state — reinstalling with `safekeep daemon install` restores the
previous setup exactly (the templates live in the package). `--purge` deletes
`~/.safekeep` (your destinations and rules) and the logs; do that only when
you want safekeep gone for good.
