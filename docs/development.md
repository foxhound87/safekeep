# Development

## Tests

```bash
python3 -m unittest discover -s tests
```

Standard-library `unittest`, zero dependencies: 206 tests covering the
matcher, config, atomic copy, volumes, daemon, auto-discovery, the CLI, the
permission-trend wrapper (`tests/test_trend.py`, SPEC §9.2) and the POSIX
portability layer.
`fswatch` is **not** required — `tests/test_cli.py` tolerates its absence and
the daemon loop uses a fake fswatch (a Python script), not the real binary.

`fswatch` **is** installed in the CI images: since 0.3.0 `doctor` expects the
monitor of the current platform (`inotify_monitor` on Linux, `fsevents_monitor`
on macOS), so the suite runs against the real binary there too.

CI runs that command on GitHub Actions and GitLab CI over a 3.9/3.12/3.14
matrix — **three legs, not one**: unit suite, then `tests/e2e_linux.sh` (live
daemon, real `fswatch`), then the WSL step — `WSL_DISTRO_NAME=SafekeepCITest
bash tests/e2e_linux.sh --doctor-only`, gated on the `WSL rilevato:
SafekeepCITest` marker **and** exit 0. That step is what exercises the WSL
branch of `doctor` (SPEC §17) for real instead of the env faked by the unit
tests; it needs nothing beyond `python3`, `bash`, `grep` and `mktemp`. Both CIs
declare the same three legs and the same steps (`.github/workflows/test.yml`,
`.gitlab-ci.yml`).

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
