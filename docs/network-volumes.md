# Network volumes

Since 0.9.0 Safekeep follows projects that live on **SMB shares** (Samba,
Time Machine, NAS or VM shares) as well as manually mounted CIFS shares on
Linux and WSL.

On these volumes event observation changes nature: SMB/CIFS clients do not
reliably surface local file events, and writes made from another host only
appear when the client re-reads the share. Safekeep therefore combines two
mechanisms inside the same daemon:

- **fswatch** for local and NFS roots;
- a **periodic poll** for network roots, comparing SHA-256 hashes against
  the previous snapshot.

No configuration is needed beyond mounting the share: the classification
(`classify_roots`) is automatic, driven by the filesystem type reported by
`statfs`/`statvfs`.

## Requirements

- **macOS**: mount the share in Finder; it appears under `/Volumes/...`
- **Linux**: mount with `mount.cifs` (package `cifs-utils`)
- **WSL**: mount under `/mnt/...` (or a drive letter visible to WSL) with
  `cifs-utils` installed

::: warning TCC on macOS
The daemon reads the share as the user that starts it. Grant Safekeep
**Files and Folders → Removable Volumes** and **Local Network** access in
**System Settings → Privacy & Security**, otherwise every read on the share
is denied and the project stays `blocked`.

Check with `safekeep doctor`: the `TCC …` rows must report `ok: …`.
:::

## Mounting

### macOS

1. `Finder → Go → Connect to Server…` (⌘K) and pick `smb://host/share`
2. The share appears in `/Volumes/<name>`
3. `safekeep daemon install --interval 15`

### Linux

```bash
sudo apt-get install -y cifs-utils
sudo mkdir -p /mnt/safekeep
sudo mount -t cifs //host/share /mnt/safekeep \
  -o username=user,uid=$UID,gid=$GID,vers=3.0
safekeep daemon install --interval 15
```

Make it persistent in `/etc/fstab`:

```
//host/share  /mnt/safekeep  cifs  credentials=/etc/safekeep/smbcreds,uid=1000,gid=1000,_netdev  0  0
```

### WSL

```powershell
wsl -u root apt-get install -y cifs-utils
sudo mount -t cifs //host/share /mnt/share -o username=user,vers=3.0
safekeep daemon install --interval 15
```

## Expected behaviour

- **Local changes**: the poll detects the SHA-256 difference within
  `poll_interval` (default 60s) and produces snapshots exactly like local
  events.
- **Remote changes**: writes made from another host are picked up on the
  next tick, delayed only by the client's SMB caching.
- **Unmount**: while the share is down, `safekeep status` and
  `safekeep doctor` report `network volume unreachable` and every project on
  that share is `blocked` with that note. Snapshots and pending state are
  preserved.
- **Remount**: the next tick reclassifies the roots and the poller resumes;
  changes made while the share was down are caught up on the first walk
  after remount.

## Diagnostics

```bash
safekeep doctor   # mountpoint, mounted state, reachability, TCC
safekeep status   # [network] rows / network volume unreachable
```

Example with the share unmounted:

```
  [network]  NAS /Volumes/backup  network volume unreachable
```

::: tip
`safekeep poll` is an internal module (`safekeep/poll.py`), not a CLI
command. Polling starts automatically when the daemon classifies at least
one root on a network volume.
:::
