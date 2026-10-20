#!/bin/bash
# CI smoke (safekeep 0.7.0, task 7 del piano): wheel + venv pulito + HOME
# temporaneo → `safekeep daemon install` → unit presente, NESSUN placeholder
# residuo, config creata con chmod 600, `doctor` best-effort.
#
# Portabilità:
#  - senza systemd (immagini slim/container) l'install NON-WSL esce 0 con
#    warning + unit scritta; con WSL_DISTRO_NAME settato si esercita il RAMO
#    ERRORE WSL (exit != 0 con le istruzioni di /etc/wsl.conf).
#  - con systemd (runner GH con systemd = PID 1) si esercita il ramo Linux
#    completo (daemon-reload best-effort, unit valida).
#
# NESSUNA operazione di servizio reale che avvii o fermi un agent:
#  - `daemon install` non carica la unit (niente bootstrap/enable);
#  - qui NON vengono mai eseguiti `daemon start|stop|uninstall` (sul launcher
#    dell'utente toccherebbero l'agent reale — i comandi sono coperti dai
#    test unit con `_run` mockkato);
#  - unica chiamata a systemctl: `daemon-reload` (best-effort, solo rileggere
#    le unit, non avvia/ferma nulla).
# Pensato per la CI: gira anche in locale, ma è CI-only.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== build wheel =="
# pip wheel: build backend + isolamento, senza pacchetti extra (`build`)
python3 -m pip wheel --no-deps --quiet -w "$TMP/dist" .
WHL="$(ls "$TMP"/dist/safekeep-*.whl)"
echo "wheel: $WHL"
# zipfile -l: stdlib, niente unzip nell'immagine slim
python3 -m zipfile -l "$WHL" | grep -q 'safekeep/data/safekeep.service.tmpl'
python3 -m zipfile -l "$WHL" | grep -q 'safekeep/data/com.safekeep.agent.plist.tmpl'
python3 -m zipfile -l "$WHL" | grep -q 'safekeep/data/safekeep.example'

echo "== venv pulito + install della wheel =="
python3 -m venv "$TMP/venv"
"$TMP/venv/bin/pip" install --quiet "$WHL"

export HOME="$TMP/home"
mkdir -p "$HOME"
export PATH="$TMP/venv/bin:$PATH"
UNIT_LINUX="$HOME/.config/systemd/user/safekeep.service"
UNIT_DARWIN="$HOME/Library/LaunchAgents/com.safekeep.agent.plist"

echo "== smoke: daemon --help =="
safekeep daemon --help >/dev/null
safekeep daemon install --help >/dev/null

echo "== safekeep daemon install (HOME temporaneo) =="
safekeep daemon install
if [ "$(uname -s)" = Darwin ]; then
    UNIT="$UNIT_DARWIN"
    grep -q '<string>run</string>' "$UNIT" || { echo 'FAIL: ExecStart != shim run'; exit 1; }
else
    UNIT="$UNIT_LINUX"
    grep -qE '^ExecStart=.+ run$' "$UNIT" || { echo 'FAIL: ExecStart != shim run'; exit 1; }
fi
test -f "$UNIT"
if grep -qE '__[A-Z_]+__' "$UNIT"; then
    echo 'FAIL: placeholder residuo nella unit:'; grep -E '__[A-Z_]+__' "$UNIT"; exit 1
fi
test -f "$HOME/.safekeep"
python3 - "$HOME/.safekeep" <<'EOF'
import os, sys
mode = os.stat(sys.argv[1]).st_mode & 0o777
assert mode == 0o600, f'permessi config {oct(mode)} != 0o600'
EOF

echo "== doctor (best-effort) =="
safekeep doctor || echo "doctor exit $? (best effort)"

echo "== ramo errore WSL (solo dove systemd manca davvero) =="
if [ ! -d /run/systemd/system ]; then
    if WSL_DISTRO_NAME=SafekeepCICheck safekeep daemon install 2>"$TMP/wsl.err"; then
        echo 'FAIL: WSL senza systemd deve uscire != 0'; exit 1
    fi
    grep -F 'wsl.conf' "$TMP/wsl.err"
    grep -F 'systemd=true' "$TMP/wsl.err"
    echo "OK: ramo errore WSL esercitato (exit != 0)"
else
    echo "systemd presente: ramo WSL non esercitabile qui (coperto dai test unit)"
fi

echo "== preflight senza fswatch → exit != 0 con install_hint =="
if PATH="$TMP/venv/bin" safekeep daemon install 2>"$TMP/nofs.err"; then
    echo 'FAIL: senza fswatch il preflight deve fallire'; exit 1
fi
grep -q 'fswatch' "$TMP/nofs.err"
grep -q 'install' "$TMP/nofs.err"

echo "== systemd-analyze verify (best-effort, solo dove disponibile) =="
if command -v systemd-analyze >/dev/null 2>&1 && [ -f "$UNIT_LINUX" ]; then
    systemd-analyze verify "$UNIT_LINUX" \
        || echo 'systemd-analyze verify: warning (best effort)'
fi

echo "ci_daemon_install.sh: OK"
