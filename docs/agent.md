# Agent (launchd / systemd)

The daemon runs as a per-user agent: started at boot, restarted if it dies —
a launchd agent on macOS, a systemd *user unit* on Linux.

## Install

```bash
git clone https://github.com/foxhound87/safekeep.git
cd safekeep

# only external dependency, if not installed yet (see Install for other distros)
brew install fswatch      # macOS — Linux: pacman / apt / dnf

bash install.sh
```

`install.sh` (idempotent) picks the agent for your OS (`uname -s`) and does
three things:

1. creates `~/.safekeep` from `examples/safekeep.example` if missing;
2. renders the template — placeholders `__REPO__`, `__PYTHON__` (and `__HOME__`
   on the plist) become real paths:

   | | macOS | Linux |
   |---|---|---|
   | Template | `launchd/com.safekeep.agent.plist` | `systemd/safekeep.service` |
   | Rendered to | `~/Library/LaunchAgents/com.safekeep.agent.plist` | `~/.config/systemd/user/safekeep.service` |
   | Check | `plutil -lint` | `systemctl --user daemon-reload` |

3. runs preflight checks (`fswatch`, `python3` >= 3.9).

It does **not** start the job: configure `~/.safekeep` first, then load it.

## Load / unload / reload

```bash
# ---------- macOS (launchd) ----------
# register and start (RunAtLoad)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.safekeep.agent.plist

# unload (removes the job from the bootstrap domain)
launchctl bootout gui/$(id -u)/com.safekeep.agent

# restart in place
launchctl kickstart -k gui/$(id -u)/com.safekeep.agent

# status
launchctl print gui/$(id -u)/com.safekeep.agent | head -40
```

```bash
# ---------- Linux (systemd user unit) ----------
systemctl --user enable --now safekeep     # ≈ bootstrap: register + start

systemctl --user disable --now safekeep    # ≈ bootout
systemctl --user restart safekeep          # ≈ kickstart -k
systemctl --user status safekeep           # ≈ print

journalctl --user -u safekeep -f           # logs

# boot without a login session (otherwise the unit starts after the first login)
loginctl enable-linger $USER
```

## The plist (macOS)

Label `com.safekeep.agent`, `ProgramArguments` = python + `bin/safekeep.py
run`, `RunAtLoad` + `KeepAlive`, `ThrottleInterval 30` (no frantic restart
loop), `PYTHONUNBUFFERED=1`.

| Setting | Value |
|---|---|
| Template | `launchd/com.safekeep.agent.plist` |
| Rendered to | `~/Library/LaunchAgents/com.safekeep.agent.plist` |
| Interpreter | `__PYTHON__` → `/usr/bin/python3` (stable system shim, since 0.5.1: a `brew upgrade` can never delete it — see [Troubleshooting](/troubleshooting#daemon-runs-from-a-deleted-executable-homebrew-upgrade)) |
| stdout | `/tmp/safekeep.out.log` |
| stderr | `/tmp/safekeep.err.log` |

`StartOnMount` (restart the job when a volume mounts) is documented in the
template and left commented out: the application-level backoff is the default
fallback.

## The unit (Linux)

`Type=simple`, `ExecStart` = python + `bin/safekeep.py run`, `Restart=always`
with `RestartSec=2` (≈ `KeepAlive`), `WantedBy=default.target` (≈ `RunAtLoad`),
`PYTHONUNBUFFERED=1`.

`StartLimitIntervalSec=0` disables systemd's default rate limit (5 starts in
10s would park the unit in `failed` and stop restarting it) — the pacing stays
with the daemon, exactly like `ThrottleInterval 30` on macOS.

| Setting | Value |
|---|---|
| Template | `systemd/safekeep.service` |
| Rendered to | `~/.config/systemd/user/safekeep.service` |
| Logs | `journalctl --user -u safekeep -f` |
| Reload after editing | `systemctl --user daemon-reload` |

## State

```bash
~/.local/state/safekeep/   # daemon log (both platforms)
```

## Uninstall

```bash
# macOS
launchctl bootout gui/$(id -u)/com.safekeep.agent

# Linux
systemctl --user disable --now safekeep

bash uninstall.sh     # removes the plist / unit; -p also removes ~/.safekeep
```
