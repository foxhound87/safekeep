# Quickstart

Three steps: one global config, one `.sync` per project, then dry-run.

## 1. `~/.safekeep`

The single place where destinations live:

```bash
# observed roots: only directories containing a .sync are followed
source: ~/Code
source: ~/Projects

# destination for ALL projects
dest: ~/Backup/safekeep

# relative (default): <dest>/<path relative to the source root>
#   ~/Code/myapp/docs/a.md → ~/Backup/safekeep/myapp/docs/a.md
layout: relative

log_level: info
```

`source:` is optional; without it safekeep switches to
[auto-discovery](/configuration#source-mode-vs-auto-discovery). `dest:` is
mandatory.

## 2. `<project>/.sync`

Drop a `.sync` in the root of every project you want to follow
(`~/Code/myapp/.sync`):

```bash
name: myapp

# allow-list semantics: without these lines NOT A SINGLE file would be copied
*.md
.env
!secrets/old.env      # ! = exclude
```

Bare lines are gitignore-style — **the inverse of gitignore**: a line lists
what to **copy**, and a leading `!` turns it into an exclude. A directory
without a `.sync` is not tracked.

## 3. Dry run, then start

```bash
safekeep sync-once --dry-run   # prints what would be copied, copies nothing
safekeep run                   # daemon: watch + copy (foreground)
```

Once `run` looks right, load it at boot with the per-OS agent — see
[Agent (launchd / systemd)](/agent).
