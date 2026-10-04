# Install

## Requirements

| | |
|---|---|
| OS | macOS, or Linux with systemd (POSIX) |
| Python | >= 3.9 |
| fswatch | [emcrisostomo/fswatch](https://github.com/emcrisostomo/fswatch) |

```bash
brew install fswatch      # macOS
sudo pacman -S fswatch    # Arch / Omarchy
sudo apt install fswatch  # Debian / Ubuntu
sudo dnf install fswatch  # Fedora
```

`safekeep doctor` prints the right command if `fswatch` is missing.

safekeep itself has **no runtime dependencies**: pure Python standard library.

## pipx (recommended)

```bash
pipx install safekeep
```

## pip

```bash
pip install safekeep
```

## Agent (launchd / systemd)

The agent that runs the daemon at boot is installed from a checkout of the
repository — not from the wheel — because the rendered plist / systemd unit
points at a concrete checkout:

```bash
git clone https://github.com/foxhound87/safekeep.git
cd safekeep
bash install.sh
```

`install.sh` is idempotent: it creates `~/.safekeep` from the example (if
missing), renders `~/Library/LaunchAgents/com.safekeep.agent.plist` on macOS
or `~/.config/systemd/user/safekeep.service` on Linux, and runs preflight
checks. It does **not** start the job — see [Agent](/agent).

## Verify

```bash
safekeep --help
safekeep doctor
```

Run `doctor` after granting Full Disk Access on macOS (see
[Troubleshooting](/troubleshooting)); on Linux the same command checks the
`inotify` watch limit and the systemd unit.
