#!/bin/bash
# safekeep — rimuove l'agent launchd. Tocca solo il job e il plist:
# ~/.safekeep e i log restano intatti (usa -p per rimuoverli anche).
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.safekeep.agent.plist"

# errore ignorato se il job non è mai stato caricato
launchctl bootout "gui/$(id -u)/com.safekeep.agent" 2>/dev/null || true
rm -f "$PLIST"
echo "→ rimosso $PLIST"

if [ "${1:-}" = "-p" ]; then
    rm -f "$HOME/.safekeep"
    rm -rf "$HOME/.local/state/safekeep"
    echo "→ rimossi anche ~/.safekeep e i log in ~/.local/state/safekeep"
else
    echo "→ conservati ~/.safekeep e i log (usa -p per rimuoverli anche)"
fi
