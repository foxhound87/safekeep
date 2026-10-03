---
layout: home

hero:
  name: safekeep
  text: Selective always-on backups for macOS
  tagline: Watches your source folders with fswatch and copies only the files you allow-list. It never deletes anything.
  actions:
    - theme: brand
      text: Install
      link: /install
    - theme: alt
      text: GitHub
      link: https://github.com/foxhound87/safekeep

features:
  - title: Allow-list rules
    details: >
      A file is copied only if it matches at least one rule in .sync — gitignore-style
      bare lines, the inverse of gitignore: a line lists what to copy. No match means
      no copy.
  - title: Atomic copies
    details: >
      Temp file in the destination's own directory, then fsync + rename. A reader
      sees either the old file or the new one, never a partial copy.
  - title: Never deletes
    details: >
      A file removed or renamed at the source stays in the backup. The backup is
      copy-and-update only — history by construction.
  - title: fswatch + launchd
    details: >
      FSEvents-backed watching through fswatch, with a launchd agent (RunAtLoad +
      KeepAlive) that starts at boot and restarts the daemon if it dies.
  - title: Zero dependencies
    details: >
      Pure Python standard library. The only external pieces are fswatch and
      launchd, both already part of the macOS story.
---
