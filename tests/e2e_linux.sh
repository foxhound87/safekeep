#!/usr/bin/env bash
# E2E (end-to-end) live run on Linux — the gate the unittest suite can't give:
# every test drives a FAKE fswatch, this one drives the real `fswatch`
# (inotify_monitor) through the daemon and asserts a real copy.
#
# What it covers:
#   1. `doctor --config` exits 0 on a stock Linux box;
#   2. the daemon spawns the platform monitor and watches the source root;
#   3. a live event (`touch src/hello.md`) reaches dest as `hello.md`
#      (layout `relative`, path relative to the source root — SPEC §4.3);
#   4. the `.sync` rules hold under live events: `!draft.md` and a non-`*.md`
#      file are NOT copied (allow-list — SPEC §4.3);
#   5. clean SIGTERM shutdown (exit 0), no orphan `fswatch`, no
#      `*.safekeep.tmp.*` residue in dest.
#
# Sandbox: everything lives in a `mktemp -d` that is removed on exit, and
# HOME is pointed at it too, so the daemon log and every doctor probe stay
# inside the sandbox — no `~/.safekeep`, no launchd/systemd unit, no real
# backup destination is ever touched.
#
# Usage: bash tests/e2e_linux.sh   (exit 0 = pass)
set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX="$(mktemp -d)"
SRC="$SANDBOX/src"
DEST="$SANDBOX/dest"
CFG="$SANDBOX/safekeep.conf"
export HOME="$SANDBOX/home"          # log + probe di doctor dentro il sandbox

DAEMON_PID=""
FSWATCH_PID=""
STATUS=0

fail() { echo "FAIL: $*" >&2; STATUS=1; }
note() { echo "==> $*"; }

cleanup() {
  if [[ -n "$DAEMON_PID" ]] && kill -0 "$DAEMON_PID" 2>/dev/null; then
    kill -TERM "$DAEMON_PID" 2>/dev/null || true
    wait "$DAEMON_PID" 2>/dev/null || true
  fi
  rm -rf "$SANDBOX"
}
trap cleanup EXIT

# wait_for <secondi> <cmd...> — poll una volta al secondo, esce al primo successo
wait_for() {
  local deadline=$((SECONDS + $1))
  shift
  while ((SECONDS < deadline)); do
    if "$@"; then return 0; fi
    sleep 1
  done
  "$@"
}

# Solo il fswatch DI QUESTO run: figlio diretto del daemon. Un `pgrep -x`
# generico basterebbe a un altro safekeep vivo sulla stessa macchina.
fswatch_ours() { pgrep -P "$DAEMON_PID" -x fswatch >/dev/null 2>&1; }
fswatch_ours_dead() { ! kill -0 "$FSWATCH_PID" 2>/dev/null; }

mkdir -p "$SRC" "$DEST" "$HOME"
printf 'source: %s\ndest: %s\nlog_level: info\n' "$SRC" "$DEST" >"$CFG"
printf '*.md\n!draft.md\n' >"$SRC/.sync"

note "doctor on the sandbox config"
if ! python3 "$REPO/bin/safekeep.py" doctor --config "$CFG"; then
  fail "doctor exited != 0"
fi

note "daemon: python3 bin/safekeep.py run --config $CFG"
python3 "$REPO/bin/safekeep.py" run --config "$CFG" &
DAEMON_PID=$!

# fswatch vivo: figlio diretto del daemon (nome esatto, no -f)
if ! wait_for 10 fswatch_ours; then
  fail "fswatch never started (daemon alive: $(kill -0 "$DAEMON_PID" 2>/dev/null && echo yes || echo no))"
  exit "$STATUS"
fi
FSWATCH_PID="$(pgrep -P "$DAEMON_PID" -x fswatch | head -n1)" || true
sleep 2                               # lascia a fswatch il tempo di agganciare i watch

note "live event: touch src/hello.md -> dest/hello.md"
touch "$SRC/hello.md"
if ! wait_for 15 test -f "$DEST/hello.md"; then
  fail "hello.md did not reach dest within 15s"
fi

note "exclusions: draft.md (!draft.md) and skip.txt (non *.md) must stay out"
touch "$SRC/draft.md" "$SRC/skip.txt"
sleep 3                               # after grace: fswatch latency is -l 1.0
if [[ -e "$DEST/draft.md" ]]; then
  fail "draft.md was copied although .sync excludes it"
fi
if [[ -e "$DEST/skip.txt" ]]; then
  fail "skip.txt was copied although *.md does not match it"
fi

note "shutdown on SIGTERM"
kill -TERM "$DAEMON_PID"
rc=0
wait "$DAEMON_PID" || rc=$?
DAEMON_PID=""
if [[ $rc -ne 0 ]]; then
  fail "daemon exited with $rc (expected 0)"
fi

if [[ -n "$FSWATCH_PID" ]] && ! wait_for 5 fswatch_ours_dead; then
  fail "fswatch (pid $FSWATCH_PID) outlived the daemon"
fi

residue="$(find "$DEST" -name '*.safekeep.tmp.*' 2>/dev/null || true)"
if [[ -n "$residue" ]]; then
  fail "tmp residue in dest: $residue"
fi

if [[ $STATUS -eq 0 ]]; then
  note "E2E OK"
fi
exit "$STATUS"
