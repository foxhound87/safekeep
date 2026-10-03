# Install

## Requirements

| | |
|---|---|
| OS | macOS |
| Python | >= 3.9 |
| fswatch | [emcrisostomo/fswatch](https://github.com/emcrisostomo/fswatch) |

```bash
brew install fswatch
```

safekeep itself has **no runtime dependencies**: pure Python standard library.

## pipx (recommended)

```bash
pipx install safekeep
```

## pip

```bash
pip install safekeep
```

## Launchd agent

The agent that runs the daemon at boot is installed from a checkout of the
repository — not from the wheel — because the launchd plist points at a
concrete checkout:

```bash
git clone https://gitlab.com/foxhound87/safekeep.git
cd safekeep
bash install.sh
```

`install.sh` is idempotent: it creates `~/.safekeep` from the example (if
missing), renders `~/Library/LaunchAgents/com.safekeep.agent.plist`, and runs
preflight checks. It does **not** load the job — see
[Agent (launchd)](/agent-launchd).

## Verify

```bash
safekeep --help
safekeep doctor
```

Run `doctor` after granting Full Disk Access (see
[Troubleshooting](/troubleshooting)).
