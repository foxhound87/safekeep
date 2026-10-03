# Configuration

Two files, two jobs. The split is deliberate: **destinations live only in
`~/.safekeep`, rules live only in `.sync`**.

## `~/.safekeep` (global)

A single file in `$HOME` — no `~/.config/safekeep/…`. Syntax is
`key: value`, one per line, `#` comments, blank lines ignored.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `source` | path, repeatable | optional | roots to watch; a directory without a `.sync` is not followed |
| `dest` | path, repeatable | **required** | destinations for every project |
| `layout` | `relative` \| `full` | `relative` | destination path layout |
| `defaults` | rule, repeatable | builtin | low-priority default rules |
| `log_level` | `debug`\|`info`\|`warn`\|`error` | `info` | log verbosity |

This file is **fail-fast**: an unknown key or an invalid line is a
`ConfigError` with a line number.

### Source mode vs auto-discovery

- **Source mode** — one or more `source:` lines. Those are exactly the watched
  roots.
- **Auto-discovery** — no `source:` line at all (not an error). safekeep scans
  `$HOME` for `.sync` files and watches `$HOME`. Skipped during the scan:
  hidden directories, `Library`, `.Trash`, `.cache`, `node_modules`, `.git`,
  `.venv`, `__pycache__`, `venv`, `Applications`, and the macOS privacy-protected
  folders (`Desktop`, `Documents`, `Downloads`, `Movies`, `Music`, `Pictures`,
  `Public`). A `.sync` created after startup is picked up automatically.

`dest:` is mandatory in both modes.

### Layout

- `relative` (default): `<dest>/<path relative to the source root>`.
  In auto-discovery mode the reference root is `$HOME`, so
  `~/Code/myapp/docs/a.md` → `<dest>/Code/myapp/docs/a.md`. The project segment
  stays in the path, so two projects never collide.
- `full`: `<dest>/<absolute path without leading slash>`.

## `.sync` (per project)

Rules only. Placed in the root of the project you want to follow.

```bash
name: myapp

*.md
docs/**
!docs/vendor/CHANGELOG.md
```

### Keys

| Key | Meaning |
|---|---|
| `name: <name>` | readable project name (logs, `status`) |
| `defaults: <rule>` | project-level defaults: above global rules, below this file's rules |

### Bare lines

A line that is not blank, not a comment and not a `key:` shape is an
**include**; a line starting with `!` is an **exclude** (the `!` is stripped):

```
*.md      # ≡ include: *.md
!old.md   # ≡ exclude: old.md
```

Bare lines share the **same ordered list** as the explicit `include:` /
`exclude:` keys.

> **It is the inverse of gitignore.** In a `.gitignore` a line lists what to
> *ignore*; here it lists what to *copy*. `*.md` excludes markdown in
> `.gitignore` and includes it in `.sync`. The `!` keeps the opposite sign:
> `!x` re-includes in gitignore, excludes here.

### No destinations in `.sync`

`dest:` (or `-dest:`) does not exist in a `.sync`: a line shaped like a key and
not a known key is **invalid** (warning with `log_level: info`, hard error with
`log_level: debug`). A `.sync` coming from a third-party clone therefore cannot
redirect your backup anywhere.

### Last-match-wins

1. Start from the defaults verdict: builtin < global `defaults:` < project
   `defaults:` (later wins between them).
2. Walk the `.sync` rules top to bottom.
3. The last rule matching the path (or a parent directory) overwrites the
   verdict.
4. Nothing matched → **allow-list default**: a *file* is not copied, a
   *directory* is still traversed.

Consequences:

- an `exclude:` beats an `include:` only if it sits **below** it;
- an `include:` further down **re-includes** what an `exclude:` removed;
- rules in `.sync` always win over defaults and global rules;
- a file must match at least one include or it is not copied;
- "copy everything except X" = `**` as the **first** rule, then re-list the
  builtins (`.git/`, `node_modules/`, `.venv/`, `__pycache__/`, `.DS_Store`,
  `*.swp`) as excludes — `**` sits above them and would re-include them.

### Directory pruning

A directory with an **excluded** verdict is pruned together with its whole
subtree (no walk, no child events). A directory that matches **no** rule is
traversed — pruning applies to explicit exclusions only, so `*.md` still
reaches `docs/note.md`.

### Pattern anchoring

| Pattern | Matches |
|---|---|
| `*.md` | basename at any depth: `README.md`, `docs/note.md`, `a/b/c.md` |
| `docs/a.md` | internal `/` → anchored to the project root |
| `/docs` | anchored to the project root |
| `docs/**` | the directory itself and everything under it |
| `foo/` | directory `foo` only (plus its subtree, thanks to pruning) |
| `?` | a single character (no `/`) |
| `*` | any sequence inside one path segment (no `/`) |
| `# …` | whole-line comment |

Builtins, always present at the lowest priority unless overridden:

```
.git/
node_modules/
.venv/
__pycache__/
.DS_Store
*.swp
```

`.sync` itself is always copied — an implicit lowest-layer `include: .sync`, so
the destination can reconstruct the config.

Full commented example: [`examples/sync.example`](https://gitlab.com/foxhound87/safekeep/blob/main/examples/sync.example).
