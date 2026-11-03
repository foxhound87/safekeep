# Destination routing

Since 0.11.0 safekeep no longer sends every project to **every** destination.
A `route` line picks, per project, which destinations receive its files:
different projects can land on different targets — a NAS for active work, an
external disk for cold archives, an S3 prefix per client.

Routing lives **only** in `~/.safekeep`, never in a `.sync`: destinations
stay outside the project, so a cloned repo can never redirect your backup —
the same split described in [Configuration](/configuration).

## When it helps

- different clients backed up to different buckets;
- `~/code` on S3, everything else on the NAS;
- a default target (`route ~: nas`) with specific overrides on top.

## Configuration

Copy-paste ready — named destinations, the legacy anonymous one, the legacy
global S3 block and a set of routes:

```ini
# named filesystem destinations (routes can name them)
dest nas: /Volumes/NAS/backup
dest ext: /Volumes/EXFIT/safekeep-backup
# anonymous destination (legacy, still valid — routes cannot name it)
dest: /legacy/anonymous/path

# global S3 (legacy, still valid): default bucket for `s3:<prefix>` routes
s3_bucket: my-bucket
s3_access_key: AKIA...
s3_secret_key_env: SAFEKEEP_S3_SECRET

# source -> destinations (longest-prefix-wins, see Matching below)
route ~/projects: nas                 # one destination
route ~/Documents/notes: ext, nas     # several, comma-separated
route ~/code: s3:backup/code          # bucket = s3_bucket, prefix backup/code
route ~/archive: s3://other-bucket/old  # explicit, different bucket
route ~: nas                          # default for everything unmatched
```

| Line | Meaning |
|---|---|
| `dest <name>: <path>` | named filesystem destination; `<name>` is what routes, `status`, `doctor` and `restore --dest` refer to (letters, digits, `_`, `-`) |
| `dest: <path>` | anonymous destination (legacy, still valid) — a route cannot name it |
| `route <src>: <dest-list>` | sends the source `<src>` to every destination in the list (names and/or S3 targets, comma-separated) |
| `route <src>: s3:<prefix>` | S3 target: bucket = the global `s3_bucket`, prefix `<prefix>` |
| `route <src>: s3://<bucket>/<prefix>` | S3 target with an explicit bucket, different from `s3_bucket` |

## How a project is matched

Matching is **per project** and evaluates **two levels**: the *source root*
(the watched root containing the project, or `$HOME` in auto-discovery) and
the *project root* (the directory holding the project's `.sync`). A route
matches a project when its `<src>` is a prefix of either root; among all the
matches — across both levels — the route with the **longest** `<src>` wins
(*longest-prefix-wins*). The match is on the project's root, never on
individual files.

The two levels exist for auto-discovery: with no `source:` every project's
source root is `$HOME`, so a per-project route would never match on the
first level — the project root does. For example, with no `source:` line:

```ini
route ~/code: nas    # picks up the project ~/code/myapp, NOT ~/altro/myapp
route ~: nas         # default for everything else
```

`route ~: x` matches everything on `$HOME` — that is allowed and is how you
express a default: any more specific route (`~/code`, `~/projects/…`) wins
for its own projects, because the longest prefix beats the shortest.

- **Overlapping routes are fine** (one prefix of another) — that is exactly
  what longest-prefix-wins resolves. Only the **same** `<src>` declared twice
  is an error.
- **A source with no matching route goes to every destination** (anonymous +
  named + the global S3 one) — the exact 0.10.0 behaviour. Routing is opt-in.

## S3 per route

- `s3:<prefix>` uses the **global `s3_bucket`** and the prefix as written:
  the prefix is **literal and complete**, the global `s3_prefix` does **not**
  apply to it. No `s3_bucket:` in the file → a `ConfigError` with the line
  number; the bucket is never deduced from the path.
- `s3://<bucket>/<prefix>` picks an explicit bucket, different from
  `s3_bucket`.
- Credentials are the same global `s3_*` block for every route (same
  endpoint, same access key): a route never carries its own credentials.
  `s3_access_key` is required as soon as one S3 route exists.

See [S3 destination](/s3) for the `s3_*` block itself.

## Commands

```bash
safekeep status
safekeep doctor
safekeep restore --dest nas --force ~/Code/myapp/docs/note.md
```

- **`status`** — when the config has routes, each discovered project gets a
  `routing  <name> → [nas]` line (destination names, `s3://bucket/prefix`
  for S3 targets, `tutte` for a project with no matching route).
- **`doctor`** — reports the routes: a warning for a **route that matches no
  known root** yet (not fatal — a future rescan can attach it), and a warning
  for a **named destination cited by a route that is not mounted** (mount the
  volume and retry).
- **`restore --dest <name>`** (0.11.0) — restores from the named destination
  and **takes precedence over `--from local|s3|nas`**: the name identifies
  the origin unambiguously (it works for filesystem destinations and for
  S3 routes alike). An undeclared name is an error listing the available
  ones. Without `--dest`, `--from` and the auto-detect behave as in 0.8.0 —
  see [Commands](/commands#restore).

## Fail-fast errors

The config is parsed before anything runs, so a bad route stops the daemon,
`sync-once`, `status` and `restore` alike with a `ConfigError` and the line
number:

| Error | Cause |
|---|---|
| `route duplicata` | the same `<src>` declared twice — no implicit precedence, make it unique |
| `route verso destinazione inesistente` | a name not declared with `dest <name>: <path>` |
| `route s3:… senza s3_bucket` | short-form S3 route without the global `s3_bucket:` |
| `route S3 senza credenziali` | an S3 route with no `s3_*` block at all (`s3_access_key` missing) |

## Nothing to configure

A config without any `route` line behaves exactly like 0.10.0: every source
goes to every destination (anonymous, named and the global S3 one). You add
routes only when you need them.

## Limits

- Routing is **per project root**, not per file: a route cannot carve out a
  subdirectory of a project.
- The destination layout (`relative`/`full`) stays global — a route chooses
  *where* a project goes, not how its path is mapped inside the target.
- Every S3 route shares the global `s3_*` endpoint/region/credentials: a
  second S3 service on a different endpoint is not supported in v1.
