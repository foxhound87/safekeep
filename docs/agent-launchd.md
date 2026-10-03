# Agent (launchd)

The daemon runs as a per-user launchd agent: started at boot, restarted if it
dies.

## Install

```bash
git clone https://gitlab.com/foxhound87/safekeep.git
cd safekeep

# only external dependency, if not installed yet
brew install fswatch

bash install.sh
```

`install.sh` (idempotent) does three things:

1. creates `~/.safekeep` from `examples/safekeep.example` if missing;
2. renders `launchd/com.safekeep.agent.plist` — placeholders `__REPO__`,
   `__PYTHON__`, `__HOME__` become real paths — into
   `~/Library/LaunchAgents/com.safekeep.agent.plist`, then `plutil -lint`;
3. runs preflight checks (`fswatch`, `python3` >= 3.9).

It does **not** load the job: configure `~/.safekeep` first, then bootstrap.

## Load / unload / reload

```bash
# register and start (RunAtLoad)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.safekeep.agent.plist

# unload (removes the job from the bootstrap domain)
launchctl bootout gui/$(id -u)/com.safekeep.agent

# restart in place
launchctl kickstart -k gui/$(id -u)/com.safekeep.agent

# status
launchctl print gui/$(id -u)/com.safekeep.agent | head -40
```

## The plist

Label `com.safekeep.agent`, `ProgramArguments` = python + `bin/safekeep.py
run`, `RunAtLoad` + `KeepAlive`, `ThrottleInterval 30` (no frantic restart
loop), `PYTHONUNBUFFERED=1`.

| Setting | Value |
|---|---|
| Template | `launchd/com.safekeep.agent.plist` |
| Rendered to | `~/Library/LaunchAgents/com.safekeep.agent.plist` |
| stdout | `/tmp/safekeep.out.log` |
| stderr | `/tmp/safekeep.err.log` |

`StartOnMount` (restart the job when a volume mounts) is documented in the
template and left commented out: the application-level backoff is the default
fallback.

## State

```bash
~/.local/state/safekeep/   # daemon state
```

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.safekeep.agent
bash uninstall.sh
```
