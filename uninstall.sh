#!/bin/bash
# safekeep — rimuove l'agent (launchd su macOS, systemd su Linux — SPEC.md §14.3).
# Tocca solo il job/unit: ~/.safekeep e i log restano intatti (usa -p per
# rimuoverli anche).
set -euo pipefail

OS="$(uname -s)"

case "$OS" in
    Darwin)
        PLIST="$HOME/Library/LaunchAgents/com.safekeep.agent.plist"
        # errore ignorato se il job non è mai stato caricato
        launchctl bootout "gui/$(id -u)/com.safekeep.agent" 2>/dev/null || true
        rm -f "$PLIST"
        echo "→ rimosso $PLIST"
        ;;
    Linux)
        UNIT="$HOME/.config/systemd/user/safekeep.service"
        systemctl --user disable --now safekeep 2>/dev/null || true
        rm -f "$UNIT"
        systemctl --user daemon-reload 2>/dev/null || true
        echo "→ rimossa $UNIT"
        ;;
    *) echo "✗ OS non supportato: $OS (servono macOS o Linux)" >&2; exit 1 ;;
esac

if [ "${1:-}" = "-p" ]; then
    rm -f "$HOME/.safekeep"
    rm -rf "$HOME/.local/state/safekeep"
    echo "→ rimossi anche ~/.safekeep e i log in ~/.local/state/safekeep"
else
    echo "→ conservati ~/.safekeep e i log (usa -p per rimuoverli anche)"
fi
