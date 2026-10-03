#!/bin/bash
# safekeep — installazione idempotente dell'agent launchd.
# NON carica il job (launchctl bootstrap va fatto DOPO aver configurato ~/.safekeep).
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
PY="${SAFEKEEP_PYTHON:-/opt/homebrew/bin/python3}"
TPL="$REPO/launchd/com.safekeep.agent.plist"
PLIST="$HOME/Library/LaunchAgents/com.safekeep.agent.plist"

# --- preflight ---------------------------------------------------------------
if ! command -v fswatch >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
        echo "✗ fswatch mancante — installalo: brew install fswatch" >&2
    else
        echo "✗ fswatch mancante (e Homebrew: https://brew.sh) — poi: brew install fswatch" >&2
    fi
    exit 1
fi
if [ ! -x "$PY" ]; then
    PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    echo "✗ python3 non trovato (servono Python ≥ 3.9)" >&2
    exit 1
fi

# --- directory di stato + config globale -------------------------------------
mkdir -p "$HOME/.local/state/safekeep"
if [ ! -f "$HOME/.safekeep" ]; then
    cp "$REPO/examples/safekeep.example" "$HOME/.safekeep"
    echo "→ creata $HOME/.safekeep (da examples/safekeep.example)"
fi

# --- render del plist (placeholder → path reali) -----------------------------
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__REPO__|$REPO|g" \
    -e "s|__PYTHON__|$PY|g" \
    -e "s|__HOME__|$HOME|g" \
    "$TPL" > "$PLIST"
if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$PLIST" >/dev/null
fi
echo "→ plist installato: $PLIST"

cat <<EOF

installato. Prossimi passi:
  1. Configura source/dest reali in ~/.safekeep (ora è una copia dell'esempio)
  2. Diagnostica:   $REPO/bin/safekeep.py doctor
  3. Carica l'agent: launchctl bootstrap gui/\$(id -u) $PLIST
     (scarica con: launchctl bootout gui/\$(id -u)/com.safekeep.agent)
EOF
