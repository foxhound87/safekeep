---
layout: home

hero:
  name: safekeep
  image:
    src: /safekeep.svg
    alt: safekeep
    width: 96
    height: 96
  text: Selective always-on backups for macOS and Linux
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
  - title: fswatch + launchd / systemd
    details: >
      Native watching through fswatch (FSEvents on macOS, inotify on Linux), with a
      launchd agent or a systemd user unit that starts at boot and restarts the
      daemon if it dies.
  - title: Zero dependencies
    details: >
      The core is pure Python standard library — the only external pieces are fswatch
      and the OS init system, launchd on macOS, systemd on Linux.
  - title: S3-compatible destination
    details: >
      One S3-compatible bucket (MinIO, Silo, Ceph, Wasabi, Backblaze B2, AWS) alongside
      the filesystem destinations, via an optional extra. Upload only, metadata-based
      idempotency, and it never deletes anything.
---

<style>
/* Home hero: logo above the name, responsive sizes.
   Targets the VitePress 1.x default theme hero classes
   (.VPHero.has-image > .container > .main | .image), verified in the built CSS. */

/* below 960px: slightly bigger than the 96px frontmatter default */
.VPHero.has-image .container .image .image-src {
  width: 112px;
  height: 112px;
}

@media (min-width: 960px) {
  /* stack the hero: image first, text below */
  .VPHero.has-image .container {
    flex-direction: column;
    align-items: center;
  }

  .VPHero.has-image .container .image {
    order: -1;
    flex-grow: 0;
    min-height: 0;
    margin: 0 0 24px;
  }

  .VPHero.has-image .container .image .image-container {
    width: 144px;
    height: 144px;
    transform: none;
  }

  .VPHero.has-image .container .image .image-src {
    width: 144px;
    height: 144px;
  }

  .VPHero.has-image .container .main {
    width: 100%;
    text-align: center;
  }

  /* 0,5,0: beats the theme's scoped .VPHero.has-image .name[data-v-x] { margin: 0 } */
  .VPHero.has-image .container .heading .name,
  .VPHero.has-image .container .heading .text,
  .VPHero.has-image .container .main .tagline {
    margin: 0 auto;
  }

  .VPHero.has-image .container .main .actions {
    justify-content: center;
  }
}
</style>
