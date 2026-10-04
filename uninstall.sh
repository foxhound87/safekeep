#!/bin/bash
# safekeep — rimuove l'agent (launchd su macOS, systemd su Linux — SPEC.md §14.3).
# Tocca solo il job/unit: ~/.safekeep e i log restano intatti (usa -p per
# rimuoverli anche).
set -euo pipefail

# --trend (SPEC.md §9.2): rimuove SOLO l'agent orario del trend.
if [ "${1:-}" = "--trend" ]; then
    if [ "$(uname -s)" != Darwin ]; then
        echo "✗ --trend: agent launchd solo su macOS" >&2
        exit 1
    fi
    PLIST="$HOME/Library/LaunchAgents/com.safekeep.trend.plist"
    launchctl bootout "gui/$(id -u)/com.safekeep.trend" 2>/dev/null || true
    rm -f "$PLIST"
    echo "→ rimosso $PLIST (daemon principale intatto)"
    echo "→ conservati CSV e log: ${SAFEKEEP_TREND_CSV:-$HOME/.local/state/safekeep/permission-trend.csv}"
    exit 0
fi

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
