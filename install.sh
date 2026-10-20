#!/bin/bash
# safekeep — [DEPRECATO da 0.7.0, rimozione in 0.8.0] wrapper verso
# `safekeep daemon install` (SPEC.md §14.3).
#
# I template unit non vivono più nel repo: sorgente unica nel package
# (safekeep/data/) e rendering da `safekeep daemon install` — questo script
# non renderizza più nulla. Il ramo `--trend` (SPEC.md §9.2) resta qui:
# fuori scope del comando `safekeep daemon` (0.7.0).
set -euo pipefail

# Interprete renderizzato nel plist trend (SPEC.md §8.4): lo shim di sistema
# /usr/bin/python3 non viene mai cancellato da un `brew upgrade` — un path
# Cellar sparisce sotto il processo vivo e TCC non lo identifica più (EPERM).
PY="${SAFEKEEP_PYTHON:-/usr/bin/python3}"

# ≥ 3.9 e realmente eseguibile: senza CLT (Command Line Tools) lo shim di
# sistema non risponde → cade su python3 nel PATH (comportamento pre-0.5.1)
py_ok() {
    [ -n "${1:-}" ] && [ -x "$1" ] &&
        "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
            >/dev/null 2>&1
}

# --- trend (SPEC §9.2): render + bootstrap SOLO dell'agent orario del trend --
# Legacy invariato, NON delegato: `safekeep daemon` gestisce solo l'agent
# principale. Non tocca, non ricarica la unit di com.safekeep.agent.
if [ "${1:-}" = "--trend" ]; then
    REPO="$(cd "$(dirname "$0")" && pwd)"
    OS="$(uname -s)"
    if [ "$OS" != Darwin ]; then
        echo "✗ --trend: agent launchd solo su macOS (su Linux usa cron o un systemd-timer)" >&2
        exit 1
    fi
    # preflight (come da sempre: trend gira doctor → serve fswatch)
    if ! command -v fswatch >/dev/null 2>&1; then
        echo "✗ fswatch mancante — installalo: brew install fswatch" >&2
        exit 1
    fi
    if ! py_ok "$PY"; then
        PY="$(command -v python3 || true)"
    fi
    if ! py_ok "$PY"; then
        echo "✗ python3 ≥ 3.9 non trovato (override: SAFEKEEP_PYTHON=/percorso/python3)" >&2
        exit 1
    fi
    TPL="$REPO/launchd/com.safekeep.trend.plist"
    AGENT="$HOME/Library/LaunchAgents/com.safekeep.trend.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    sed -e "s|__REPO__|$REPO|g" \
        -e "s|__PYTHON__|$PY|g" \
        -e "s|__SCRIPT__|$REPO/bin/safekeep-trend.sh|g" \
        -e "s|__HOME__|$HOME|g" \
        "$TPL" > "$AGENT"
    if command -v plutil >/dev/null 2>&1; then
        plutil -lint "$AGENT" >/dev/null
    fi
    echo "→ plist trend installato: $AGENT"
    # idempotente: se il job è già caricato, scarica SOLO quello (bootout del
    # singolo label) e lo ricarica — com.safekeep.agent non viene toccato
    if launchctl print "gui/$(id -u)/com.safekeep.trend" >/dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)/com.safekeep.trend" 2>/dev/null || true
    fi
    launchctl bootstrap "gui/$(id -u)" "$AGENT"
    echo "→ trend agent caricato (orario, StartInterval 3600)"
    echo "   prima riga tra 1h, oppure forzala:"
    echo "   launchctl kickstart gui/\$(id -u)/com.safekeep.trend"
    echo "   CSV: ${SAFEKEEP_TREND_CSV:-$HOME/.local/state/safekeep/permission-trend.csv}"
    echo "   log: $HOME/.local/state/safekeep/trend.log"
    exit 0
fi

# --- percorso principale: delega al comando nuovo ------------------------------
echo "⚠ install.sh è deprecato (safekeep 0.7.0 — verrà rimosso in 0.8.0)." >&2
echo "  Usa:  safekeep daemon install    (poi: safekeep daemon start)" >&2
echo "  pipx: pipx install safekeep      (o: pip install --user safekeep con" >&2
echo "        ~/.local/bin nel PATH di login E della shell)" >&2
if command -v safekeep >/dev/null 2>&1; then
    exec safekeep daemon install "$@"
fi
echo "✗ safekeep non trovato nel PATH — installalo prima (pipx install safekeep)" >&2
exit 1
