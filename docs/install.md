# Install

## Requirements

| | |
|---|---|
| OS | macOS, or Linux with systemd (POSIX). Windows: run it inside [WSL](/troubleshooting#windows-wsl-best-effort) (best-effort, systemd-on required for the agent) |
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
pip install --user safekeep     # make sure ~/.local/bin is on your PATH
```

### Optional: S3-compatible destination

The S3 client ([botocore](https://botocore.amazonaws.com/)) ships in an extra —
the core package stays pure standard library:

```bash
pipx install "safekeep[s3]"     # or: pip install "safekeep[s3]"
```

Without it a config with `s3_*` keys fails fast with the install hint.
Full reference: [S3 destination](/s3).

## Agent (launchd / systemd)

The agent that runs the daemon at boot is installed **from the installed
package** — the unit templates ship inside the wheel, no checkout needed:

```bash
safekeep daemon install          # preflight + render + write; does NOT start
safekeep daemon install --dry-run    # preview, nothing is written
safekeep daemon start            # after filling ~/.safekeep — loads and starts
```

The install step runs preflight checks first — `fswatch` present, `safekeep`
shim on `PATH`, python >= 3.9, and **WSL with systemd on** — and fails fast
(exit 1) if any is missing. It creates `~/.safekeep` from the embedded example
if missing (chmod `600`), renders `~/Library/LaunchAgents/com.safekeep.agent.plist`
on macOS or `~/.config/systemd/user/safekeep.service` on Linux, and does
**not** start the job — see [Agent](/agent).

On Linux, to start at boot without a login session, add `--enable-linger`
(runs `loginctl enable-linger $USER` — linger = user units active even
without a login).

The old `bash install.sh` wrapper still works but is **deprecated since
0.7.0** (removal still pending): it just delegates to `safekeep daemon install`.
On Linux **without** systemd (WSL1, WSL2 with systemd off, containers) the
preflight **fails with exit 1** and prints the fix (enable systemd in
`/etc/wsl.conf`) or the manual-start alternative (`safekeep run`) — see
[Windows: WSL](/troubleshooting#windows-wsl-best-effort).

## Verify

```bash
safekeep --help
safekeep doctor
```

Run `doctor` after granting Full Disk Access on macOS (see
[Troubleshooting](/troubleshooting)); on Linux the same command checks the
`inotify` watch limit and the systemd unit.
