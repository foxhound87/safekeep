#!/bin/bash
# safekeep — [DEPRECATO da 0.7.0, rimozione in 0.8.0] wrapper verso
# `safekeep daemon uninstall` (SPEC.md §14.3). Tocca solo il job/unit:
# ~/.safekeep e i log restano intatti (usa -p per rimuoverli anche).
# Il ramo `--trend` (SPEC.md §9.2) resta qui: fuori scope di
# `safekeep daemon` (0.7.0).
set -euo pipefail

# --- trend (SPEC §9.2): rimuove SOLO l'agent orario del trend. Legacy. --------
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

# --- percorso principale: delega al comando nuovo ------------------------------
echo "⚠ uninstall.sh è deprecato (safekeep 0.7.0 — verrà rimosso in 0.8.0)." >&2
echo "  Usa:  safekeep daemon uninstall [--purge]" >&2
if ! command -v safekeep >/dev/null 2>&1; then
    echo "✗ safekeep non trovato nel PATH — (ri)installa con: pipx install safekeep" >&2
    exit 1
fi
case "${1:-}" in
    '')         exec safekeep daemon uninstall ;;
    -p|--purge) exec safekeep daemon uninstall --purge ;;
    *) echo "uso: $0 [-p|--trend]" >&2; exit 2 ;;
esac
