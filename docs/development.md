# Development

## Tests

```bash
python3 -m unittest discover -s tests
```

Standard-library `unittest`, zero dependencies: 156 tests covering the
matcher, config, atomic copy, volumes, daemon, auto-discovery and the CLI.
`fswatch` is **not** required — `tests/test_cli.py` tolerates its absence and
the daemon loop uses a fake fswatch (a Python script), not the real binary.

Do not install `fswatch` in a Linux CI container to "be complete": `doctor`
expects the macOS `fsevents_monitor` and the suite would fail.

CI (GitLab) runs exactly that command on `python:3.12-slim`.

## Constraints

- **Standard library only.** No runtime dependencies at all; `fswatch` is an
  external binary, not a package.
- No absolute `/Users/…` paths in tests: they use `tempfile.mkdtemp()` and a
  fake `$HOME`.
- macOS specifics (`plutil`, `/var` vs `/private/var`) are detected and
  tolerated at runtime.
- `safekeep/platform.py` is the **only** module reading `sys.platform`; tests
  patch it, so both OS branches are covered from either OS.

## Layout

```
safekeep/     Python package (cli, config, matcher, daemon, copy, …)
bin/          runnable entry point (launchd / systemd point here)
tests/        unittest suite
examples/     safekeep.example, sync.example (commented)
launchd/      launchd plist template (macOS)
systemd/      systemd user unit template (Linux)
docs/         this VitePress site
```

## Branches and commits

- `main` — releasable code, tagged `vX.Y.Z` (semver).
- `docs` — this documentation site, deployed to GitHub Pages by
  `.github/workflows/deploy-docs.yml` on every push touching `docs/**`.
- Commit messages in English, imperative, prefixed by type:
  `feat:`, `fix:`, `docs:`, `ci:`, `test:`, `refactor:`.

## Docs site

```bash
npm install
npm run docs:dev      # http://localhost:5173/safekeep/
npm run docs:build    # → docs/.vitepress/dist
npm run docs:preview
```
